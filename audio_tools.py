"""Bounded-memory FFmpeg helpers for waveform review and noise sampling."""
from array import array
from dataclasses import dataclass
import math
import os
import re
import shutil
import subprocess
import sys
from media_process import managed_process


def ffmpeg_path():
    binary = os.environ.get("DECUT_FFMPEG") or shutil.which("ffmpeg")
    if not binary:
        raise RuntimeError("FFmpeg was not found. Install it or set DECUT_FFMPEG.")
    return binary


def waveform_peaks(path, start, duration, bins=4096, control=None):
    """Read PCM in small buckets, keeping only peaks, never the full recording."""
    if duration <= 0:
        return []
    rate = 8000
    frames_per_bin = max(1, math.ceil(duration * rate / bins))
    command = [ffmpeg_path(), "-hide_banner", "-nostats", "-nostdin",
               "-threads", "1", "-filter_threads", "1",
               "-ss", str(start), "-i", path, "-t", str(duration),
               "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1",
               "-ar", str(rate), "-f", "s16le", "-"]
    peaks = []
    if control:
        control.report("waveform")
    with managed_process(command, control, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        while True:
            if control:
                control.check()
            block = process.stdout.read(frames_per_bin * 2)
            if not block:
                break
            samples = array("h")
            samples.frombytes(block[:len(block) // 2 * 2])
            if sys.byteorder != "little":
                samples.byteswap()
            peaks.append(max(map(abs, samples), default=0) / 32768)
            if control and len(peaks) % 256 == 0:
                control.report("waveform", min(duration, len(peaks) * frames_per_bin / rate), duration)
        if control:
            control.check()
        if process.wait() != 0:
            raise RuntimeError(f"FFmpeg could not generate a waveform for {path}")
        # Pad short/truncated media without stretching it across the timeline.
        expected = math.ceil(duration * rate / frames_per_bin)
        return (peaks + [0.0] * max(0, expected - len(peaks)))[:expected]


@dataclass(frozen=True)
class NoiseSample:
    peak_db: float
    mean_db: float
    threshold: int
    start: float
    duration: float


def threshold_from_peak(peak_db, headroom=3):
    if not math.isfinite(peak_db):
        raise ValueError("Invalid sample level.")
    return max(-80, min(0, math.ceil(peak_db + headroom)))


def measure_noise(path, start, duration, control=None):
    """Measure a short native-rate sample; use peak rather than RMS for silencedetect."""
    if start < 0 or not 0.01 <= duration <= 2.0:
        raise ValueError("The noise sample must cover 0.01 to 2 seconds inside a clip.")
    command = [ffmpeg_path(), "-hide_banner", "-nostats", "-nostdin",
               "-threads", "1", "-filter_threads", "1",
               "-ss", str(start), "-i", path, "-t", str(duration),
               "-map", "0:a:0", "-vn", "-sn", "-dn",
               "-af", "volumedetect", "-f", "null", "-"]
    if control:
        control.report("sample")
    with managed_process(command, control, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True) as process:
        _, log = process.communicate(timeout=30)
        if control:
            control.check()
        if process.returncode:
            raise RuntimeError(f"FFmpeg could not sample audio: {log[-1200:]}")
    peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", log)
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", log)
    if not peak or not mean:
        raise ValueError("No audio at this position. Choose a point inside a clip.")
    peak_db, mean_db = float(peak[1]), float(mean[1])
    return NoiseSample(peak_db, mean_db, threshold_from_peak(peak_db), start, duration)


def sample_window(clip_start, clip_end, position, window):
    """Clamp a cursor sample to its clip, never crossing an edit or gap."""
    length = min(window, clip_end - clip_start)
    if length < 0.01:
        raise ValueError("This clip is too short for a noise sample.")
    start = min(max(position - length / 2, clip_start), clip_end - length)
    return start, length
