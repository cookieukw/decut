"""Navigation time conversion uses the timeline rate, never native media FPS."""
import math


def timeline_frame_rate(timeline, fallback=30.0):
    candidates = []
    if timeline.global_start_time is not None:
        candidates.append(timeline.global_start_time.rate)
    if timeline.tracks.source_range is not None:
        candidates.append(timeline.tracks.source_range.duration.rate)
    try:
        duration = timeline.duration()
        if duration.value > 0:
            candidates.append(duration.rate)
    except (ValueError, RuntimeError):
        pass
    for track in timeline.tracks:
        if track.source_range is not None:
            candidates.append(track.source_range.duration.rate)
        for child in track:
            source_range = getattr(child, "source_range", None)
            if source_range is not None:
                candidates.append(source_range.duration.rate)
    return next((float(rate) for rate in candidates if math.isfinite(rate) and 1 <= rate <= 240), fallback)


def frame_from_seconds(seconds, fps):
    """Nearest frame, half upward. Seconds may still point between frames."""
    return max(0, math.floor(seconds * fps + .5))


def seconds_from_frame(frame, fps):
    return frame / fps


def last_frame_position(duration, fps):
    """Allow the end boundary when duration is an exact whole-frame count."""
    return max(0, math.floor(duration * fps + 1e-7))
