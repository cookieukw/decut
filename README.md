# decut OTIO silence cutter

<p align="center">
  <img src="assets/decut.svg" alt="decut logo" width="128">
</p>

A Python desktop tool for removing silence from Kdenlive timelines without rendering an intermediate video.

## Screenshots

### Timeline and track grouping

![Timeline with grouped video and audio tracks](assets/screenshots/timeline.png)

### Empty project state

![Empty project state](assets/screenshots/empty.png)

## How it works

1. Export your Kdenlive edit as **OpenTimelineIO (`.otio`)**.
2. Open the file in decut.
3. Assign a group to each track, such as `A1` or `A2`.
4. Put every item that must be cut together in the same group:
   - gameplay video: `A1`
   - game audio: `A1`
   - voice, music, or SFX reference: `A1`
5. Enable **Analyze** only on the track containing the reference audio.
6. Configure the threshold, minimum pause and protection padding.
7. Click **Remove silence**.
8. Export the new `.otio` and open it again in Kdenlive.

Silence is detected with FFmpeg. Detected intervals are converted to global timeline time and removed from every track in the same group. Media files remain referenced by the timeline and are not re-encoded.

## Group example

| Track | Group | Analyze |
|---|---|---|
| Gameplay.mp4 | A1 | no |
| Game audio.wav | A1 | no |
| Voice.wav | A1 | yes |
| Music.mp3 | A2 | yes |
| B-roll.mp4 | A2 | no |

Tracks assigned to different groups are processed independently.

## Compact timeline

If you already deleted unwanted sections in Kdenlive and left gaps behind, use **Compact timeline**. The tool uses video tracks as the edit decision: a 40-minute hole in a video is removed from the video and from every audio track, even if an audio track continues playing during that interval. It then joins the remaining takes without breaking their relative synchronization.

Groups and silence analysis are not required for this mode. If the OTIO contains no video tracks, the tool falls back to using all tracks to detect globally empty intervals.

## Installation

Requirements:

- Python 3.10 or newer
- FFmpeg available in `PATH`

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run_otio_tool.sh
```

On Linux, make the launcher executable if necessary:

```bash
chmod +x run_otio_tool.sh
```

## Build an AppImage

On a Linux build machine with FFmpeg installed:

```bash
chmod +x build_appimage.sh
./build_appimage.sh
```

The script creates `decut.AppImage`, bundles the Python application and OpenTimelineIO, and includes the local FFmpeg binary when available. `linuxdeploy` is downloaded into `.cache/` automatically on the first build.

## Interface language

Use the language selector in the top-right corner to switch between Portuguese and English.

## Important notes

- The tool edits the OTIO structure; it does not render video files.
- The original media files must remain at the paths referenced by the OTIO file.
- Always export to a new `.otio` file so the original Kdenlive timeline remains intact.
- Silence analysis requires an audio or video media reference that FFmpeg can open.
