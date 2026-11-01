# Development and packaging

## Stack and supported workflow

- Python 3.10+; developed/tested on Linux with Python 3.12.
- PySide6 desktop widgets and background QThreads.
- OpenTimelineIO 0.18.1 for timeline interchange.
- FFmpeg for amplitude detection, short sample measurements and streamed waveform peaks.
- PyInstaller + linuxdeploy for Linux x86_64 AppImage builds.

`requirements.txt` is the current source-install dependency list. NumPy and SciPy remain listed but the current waveform/sampling implementation uses standard-library PCM buffers rather than requiring a full decoded NumPy audio array.

The old React/Vite editor is no longer part of the application.

## Repository layout

```text
app_version.py          Shared application version
otio_cutter.py          Main window, OTIO cut/compaction pipeline, export
audio_review.py         Review dialog, waveform drawing, manual calibration UI
audio_tools.py          Streamed PCM peaks and short noise measurements
media_process.py        Cancellation and FFmpeg subprocess ownership
run_otio_tool.sh        Source launcher
build_appimage.sh       AppImage build orchestration
decut.spec              Authoritative PyInstaller bundle recipe
requirements.txt       Runtime installation requirements
assets/                 Icon, desktop entry, AppStream metadata and AppRun
assets/screenshots/     English README screenshots
tests/                  Unit, Qt offscreen and FFmpeg integration tests
docs/                   User and developer guides
CHANGELOG.md            Release notes
```

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
bash run_otio_tool.sh
bash run_otio_tool.sh --version
```

The launcher uses `.venv/bin/python`. On systems with externally managed Python packages, use this virtual environment rather than overriding the system package manager.

## Pipeline

### Silence proposals

1. Validate one analyzed reference per group.
2. Detect silence once per media path/threshold/minimum-pause combination in the review cache.
3. Intersect file-time silence intervals with each clip's `source_range`.
4. Shrink silence by the speech-protection margin; never expand cuts into neighboring speech.
5. Map media time to timeline time and round detected boundaries inward to timeline frames.
6. Merge overlapping proposals within each group.
7. Display and edit that plan without changing the timeline.
8. Apply the confirmed plan with `apply_group_cuts`, which splits surviving source ranges and closes the removed intervals on each group member.

Do not add a separate global video compaction after group-based silence edits: it can delete valid content belonging to another group or a longer audio track.

### Compaction

`compact_timeline` computes the occupied-time union from the chosen reference tracks. The complement within their duration supplies the gap cuts. Those cuts are rippled out globally. This is independent of signal amplitude and does not depend on group assignments.

### Waveforms and calibration

FFmpeg emits mono 8 kHz, signed 16-bit PCM for the display. `waveform_peaks` retains maxima per bucket rather than a full recording. The review requests a capped number of display peaks per clip and draws a pixel-aggregated linear envelope. This is a visual summary, not sample-accurate waveform editing or separate stereo visualization.

Calibration uses a separate, short native-rate FFmpeg `volumedetect` job. Its peak plus 3 dB supplies the proposed threshold. Moving the needle never starts a process. **Sample here** changes the threshold; **Recalculate preview** changes proposals; **Apply reviewed cuts** changes the in-memory timeline.

The chosen sample window stays inside its clip. High-level suggestions require explicit confirmation. This does not constitute voice activity detection.

### Thread and process lifecycle

`ReviewJob` owns a cancellable `JobControl`. `managed_process` registers each subprocess, kills/waits as needed and closes its pipes. Cancellation must terminate FFmpeg before discarding the worker. Qt completion/progress callbacks are delivered to the GUI thread.

The main window retains its review dialog and requests cancellation/closure when exiting. Do not delete a running QThread or call a long synchronous wait from the GUI. Preview statistics use interval arithmetic instead of reconstructing OTIO on every UI edit.

Caches last for the review. They avoid repeated identical analysis/waveform work, but many unique clips or threshold choices still increase time and memory. Concurrently changing source media during a review is not supported.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
bash -n build_appimage.sh run_otio_tool.sh assets/AppRun
```

Qt tests run offscreen. Tests requiring FFmpeg are skipped if it is unavailable; install it for full coverage. Fixtures use generated audio and temporary directories, not private episode files.

Coverage includes source-range/rate mapping, grouped ripple cuts, protection margins, OTIO serialization, export naming/folder preferences, passive needle interactions, explicit measurement/recalculation/apply, loud-sample confirmation, linear waveform contrast, protected cuts and process/window cancellation. These tests do not replace a manual Kdenlive round-trip check on supported project structures.

## AppImage build

Build on Linux x86_64 with:

- A populated `.venv` and the runtime requirements installed.
- FFmpeg if it should be bundled.
- `curl` and network access for PyInstaller/linuxdeploy/runtime downloads on first use.
- ImageMagick (`convert` or `magick`) recommended to generate the PNG icon. Some appimagetool versions reject an SVG-only fallback.
- FUSE support, or a compatible AppImage extract-and-run setup, for build tools delivered as AppImages.

```bash
bash build_appimage.sh
chmod +x decut.AppImage
./decut.AppImage
./decut.AppImage --version
```

The script:

1. Reads/export the version from `app_version.py`.
2. Installs or upgrades PyInstaller in the build environment.
3. Recreates `build/`, `dist/` and `AppDir/`, and replaces `decut.AppImage`.
4. Uses `decut.spec` to generate an onedir bundle.
5. Copies the **whole bundle**, including `_internal`, into `AppDir/usr/lib/decut`.
6. Adds launchers and the local FFmpeg binary when available.
7. Copies desktop/AppStream/icon assets and generates a top-level PNG when possible.
8. Downloads linuxdeploy into `.cache/` when necessary and runs its AppImage output plugin.

The current output filename stays `decut.AppImage`; version information is carried by the application and packaging metadata. Updating source files does not update an existing AppImage: rebuild it.

The build is not hermetic: PyInstaller is upgraded and linuxdeploy/runtime downloads use upstream continuous releases. Build on an appropriate baseline Linux distribution; a binary built against newer glibc is not guaranteed to run on older systems. Other CPU architectures and Windows/macOS packaging are not provided by this script.

### Environment overrides

| Variable | Meaning |
|---|---|
| `DECUT_FFMPEG` | Runtime FFmpeg executable override; otherwise use `PATH`. |
| `PYTHON` | Interpreter for `build_appimage.sh`; defaults to `.venv/bin/python`. |
| `LINUXDEPLOY` | Existing linuxdeploy executable/AppImage; otherwise download to `.cache/`. |
| `QT_QPA_PLATFORM` | Qt platform selection, for example `offscreen` in automated tests. |

## Packaging troubleshooting

### Missing Python shared library or `_internal`

Do not copy only the PyInstaller executable. An onedir build needs the entire `dist/decut/` tree. The checked-in script stages this under `AppDir/usr/lib/decut/`.

### Icon or `.DirIcon` validation error

Keep `Icon=decut` in the desktop entry and include `assets/decut.svg`. Install ImageMagick so the build produces `AppDir/decut.png` for appimagetool versions that require it.

### AppStream validation error

The source metadata is `assets/decut.appdata.xml`, with component ID `com.decut.App`, desktop launchable `com.decut.App.desktop` and metadata license `CC0-1.0`. Validate it with `appstreamcli validate assets/decut.appdata.xml` when available. Missing homepage/developer/content-rating information may still produce warnings; do not invent project URLs to hide them.

### FUSE unavailable

Try the runtime's extract-and-run mode where supported:

```bash
APPIMAGE_EXTRACT_AND_RUN=1 ./decut.AppImage
```

Build-tool AppImages may also require this environment setting. If unavailable, extract the AppImage and run its `AppRun` manually in a separate directory.

### Qt platform plugin failure

Run the application in a terminal. The Qt X11/Wayland plugins may need host graphics libraries; bundling Qt does not replace the host display stack. `QT_DEBUG_PLUGINS=1 ./decut.AppImage` helps diagnose missing dependencies. An offscreen startup test proves initialization only, not normal desktop rendering.

### FFmpeg unavailable

Install FFmpeg or use `DECUT_FFMPEG=/absolute/path/to/ffmpeg`. The build copies FFmpeg from the build machine's `PATH`; the runtime override does not change what is bundled. Redistributing FFmpeg and Qt also requires complying with their applicable licenses, independently of decut's MIT license.

## Versioning and releases

`app_version.py` is the application version source. It is used by the GUI title, Qt application metadata, `--version` and the build environment's `VERSION`.

For a release, synchronize:

- `app_version.py`.
- `X-AppImage-Version` in `assets/com.decut.App.desktop`.
- The release entry in `assets/decut.appdata.xml`.
- `README.md`, these guides and `CHANGELOG.md`.

Tests verify the version metadata stays consistent. Run tests, validate the packaging assets, rebuild and manually check the AppImage before publishing. No release/tag/upload is performed automatically.
