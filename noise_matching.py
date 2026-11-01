"""Approximate noise-profile matching, not speech recognition or semantic audio search."""
from dataclasses import dataclass
import math
import subprocess

import numpy as np

from audio_tools import ffmpeg_path
from media_process import managed_process


@dataclass(frozen=True)
class NoiseProfile:
    rms_db: float
    peak_db: float
    crossings: float
    brightness: float


def pcm_profile(samples):
    values = np.asarray(samples, dtype=np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(values * values)))
    peak = float(np.max(np.abs(values)))
    if len(values) > 1:
        crossings = float(np.mean(np.signbit(values[:-1]) != np.signbit(values[1:])))
        brightness = float(np.sqrt(np.mean(np.diff(values) ** 2)) / max(rms, 1e-12))
    else:
        crossings = brightness = 0.0
    return NoiseProfile(20 * math.log10(max(rms, 1e-6)),
                        20 * math.log10(max(peak, 1e-6)), crossings, brightness)


def similar_noise(candidate, reference, tolerance=1.0):
    """Compare loudness and rough texture, never treat a loud word as a matched word."""
    if reference.peak_db <= -80 and candidate.peak_db <= -80:
        return True
    return (abs(candidate.rms_db - reference.rms_db) <= 6 * tolerance
            and abs(candidate.peak_db - reference.peak_db) <= 9 * tolerance
            and abs(candidate.crossings - reference.crossings) <= .12 * tolerance
            and abs(candidate.brightness - reference.brightness) <= .35 * tolerance)


def iter_profiles(path, control=None, start=0.0, duration=None):
    """Stream 100 ms mono frames. At most one small PCM block is retained."""
    rate, frames = 8000, 800
    command = [ffmpeg_path(), "-hide_banner", "-nostats", "-nostdin",
               "-threads", "1", "-filter_threads", "1", "-ss", str(start), "-i", path]
    if duration is not None:
        command.extend(["-t", str(duration)])
    command.extend(["-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1",
                    "-ar", str(rate), "-f", "s16le", "-"])
    cursor = start
    with managed_process(command, control, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        while True:
            if control:
                control.check()
            block = process.stdout.read(frames * 2)
            if not block:
                break
            samples = np.frombuffer(block[:len(block) // 2 * 2], dtype="<i2")
            if not len(samples):
                continue
            end = cursor + len(samples) / rate
            yield cursor, end, pcm_profile(samples)
            cursor = end
        if control:
            control.check()
        if process.wait() != 0:
            raise RuntimeError(f"FFmpeg could not analyze noise profiles: {path}")


def learn_profile(path, start, duration, control=None):
    if control:
        control.report("sample")
    profiles = [profile for _, _, profile in iter_profiles(path, control, start, duration)]
    if not profiles:
        raise ValueError("The selected window contains no audio.")
    medians = np.median([[p.rms_db, p.peak_db, p.crossings, p.brightness] for p in profiles], axis=0)
    return NoiseProfile(*map(float, medians))


def find_similar_pauses(path, profiles, minimum, tolerance=1.0, control=None):
    if not profiles:
        return []
    if control:
        control.report("matching")
    intervals = []
    active = None
    last_end = 0.0
    for start, end, candidate in iter_profiles(path, control):
        matches = any(similar_noise(candidate, reference, tolerance) for reference in profiles)
        if matches and active is None:
            active = start
        elif not matches and active is not None:
            if start - active + 1e-7 >= minimum:
                intervals.append((active, start))
            active = None
        last_end = end
    if active is not None and last_end - active + 1e-7 >= minimum:
        intervals.append((active, last_end))
    return intervals


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 1e-7:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
