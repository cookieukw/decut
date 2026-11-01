"""Navigation time conversion uses the timeline rate, never native media FPS."""
import math


def timeline_frame_rate(timeline, fallback=30.0):
    candidates = []
    if timeline.global_start_time is not None:
        candidates.append(timeline.global_start_time.rate)
    if timeline.tracks.source_range is not None:
        candidates.append(timeline.tracks.source_range.duration.rate)
    try:
        candidates.append(timeline.duration().rate)
    except (ValueError, RuntimeError):
        pass
    for track in timeline.tracks:
        if track.source_range is not None:
            candidates.append(track.source_range.duration.rate)
        for child in track:
            if child.source_range is not None:
                candidates.append(child.source_range.duration.rate)
    return next((float(rate) for rate in candidates if math.isfinite(rate) and 1 <= rate <= 240), fallback)


def frame_from_seconds(seconds, fps):
    """Nearest frame, half upward. Seconds may still point between frames."""
    return max(0, math.floor(seconds * fps + .5))


def seconds_from_frame(frame, fps):
    return frame / fps


def last_frame_position(duration, fps):
    """Allow the end boundary when duration is an exact whole-frame count."""
    return max(0, math.floor(duration * fps + 1e-7))
