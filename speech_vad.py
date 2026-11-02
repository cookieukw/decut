"""Low-memory WebRTC voice activity detection over FFmpeg PCM streaming."""
from collections import deque
import re
import subprocess

import webrtcvad

from audio_tools import ffmpeg_path
from media_process import managed_process


def _merge(intervals, gap=.25):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def voice_segments(path, mode=2, control=None):
    """Return approximate voice intervals. WebRTC sees 30 ms, 16 kHz mono frames."""
    command = [ffmpeg_path(), "-hide_banner", "-nostats", "-nostdin", "-threads", "1",
               "-i", path, "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1",
               "-ar", "16000", "-f", "s16le", "-"]
    detector = webrtcvad.Vad(max(0, min(3, int(mode))))
    frame_bytes = 16000 * .03 * 2
    frame_bytes = int(frame_bytes)
    intervals, active_start, cursor = [], None, 0.0
    with managed_process(command, control, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        while True:
            if control:
                control.check()
            block = process.stdout.read(frame_bytes)
            if len(block) < frame_bytes:
                break
            speech = detector.is_speech(block, 16000)
            if speech and active_start is None:
                active_start = cursor
            elif not speech and active_start is not None:
                intervals.append((active_start, cursor + .03))
                active_start = None
            cursor += .03
        if active_start is not None:
            intervals.append((active_start, cursor))
        process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg could not stream audio for VAD: {path}")
    return _merge(intervals)


def remove_voice_from_silences(silences, voice):
    """Keep only silence portions that do not overlap detected speech."""
    remaining = []
    for start, end in silences:
        pieces = [(start, end)]
        for voice_start, voice_end in voice:
            next_pieces = []
            for piece_start, piece_end in pieces:
                if voice_end <= piece_start or voice_start >= piece_end:
                    next_pieces.append((piece_start, piece_end))
                    continue
                if piece_start < voice_start:
                    next_pieces.append((piece_start, voice_start))
                if voice_end < piece_end:
                    next_pieces.append((voice_end, piece_end))
            pieces = next_pieces
        remaining.extend((a, b) for a, b in pieces if b - a > .01)
    return remaining
