"""Small Kdenlive MLT XML reader for timeline gaps and ordinary clips."""
import xml.etree.ElementTree as ET
from pathlib import Path
import opentimelineio as otio


def clock(value, fps=60.0):
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def read_kdenlive(path):
    root = ET.parse(path).getroot()
    profile = root.find("profile")
    fps = float(profile.get("frame_rate_num", 60)) / float(profile.get("frame_rate_den", 1)) if profile is not None else 60.0
    producers = {}
    producer_video = {}
    for producer in list(root.findall("producer")) + list(root.findall("chain")):
        resource = next((p.text for p in producer.findall("property") if p.get("name") == "resource"), None)
        if resource and resource not in ("black", "color"):
            producers[producer.get("id")] = resource
            video_index = next((p.text for p in producer.findall("property") if p.get("name") == "video_index"), "-1")
            producer_video[producer.get("id")] = video_index not in (None, "-1")
    playlists = {playlist.get("id"): playlist for playlist in root.findall("playlist")}
    timeline = otio.schema.Timeline(name=Path(path).stem)
    ordered_playlists = []
    for playlist_id, playlist in playlists.items():
        first_entry = next((item for item in playlist if item.tag == "entry"), None)
        if first_entry is None or first_entry.get("producer") not in producers:
            continue
        is_video = producer_video.get(first_entry.get("producer"), False)
        kind = "Video" if is_video else "Audio"
        label = "video" if is_video else f"audio {sum(1 for _, existing_kind, _ in ordered_playlists if existing_kind == 'Audio') + 1}"
        ordered_playlists.append((playlist_id, kind, label))
    for playlist_id, kind, label in ordered_playlists:
        playlist = playlists[playlist_id]
        track = otio.schema.Track(name=label, kind=kind)
        for item in playlist:
            if item.tag == "blank":
                duration = clock(item.get("length", "00:00:00.000"), fps)
                if duration > 0:
                    track.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(
                        duration=otio.opentime.RationalTime.from_seconds(duration, fps))))
            elif item.tag == "entry":
                resource = producers.get(item.get("producer"))
                if not resource:
                    continue
                start = clock(item.get("in", "00:00:00.000"), fps)
                end = clock(item.get("out", item.get("in", "00:00:00.000")), fps)
                duration = max(1 / fps, end - start + 1 / fps)
                track.append(otio.schema.Clip(
                    name=Path(resource).name,
                    media_reference=otio.schema.ExternalReference(target_url=resource),
                    source_range=otio.opentime.TimeRange(
                        start_time=otio.opentime.RationalTime.from_seconds(start, fps),
                        duration=otio.opentime.RationalTime.from_seconds(duration, fps))))
        if len(track):
            timeline.tracks.append(track)
    return timeline
