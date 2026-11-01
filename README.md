# decut

<p align="center">
  <img src="assets/decut.svg" alt="decut logo" width="128">
</p>

**Version 1.0.0** · Python desktop tool for reviewing silence cuts and compacting OpenTimelineIO edits from Kdenlive.

Synchronize your footage in Kdenlive, review linked cuts in decut, then return an editable `.otio` to Kdenlive. No intermediate video rendering or media re-encoding.

## Screenshots

### Tracks and groups

![decut with a Kdenlive timeline loaded, track groups and statistics](assets/screenshots/timeline.png)

### Waveform review

![Filled linear waveform, manual sampling needle and editable cut list](assets/screenshots/review.png)

The screenshots use a real **60-second excerpt** for readability. Waveforms, measurements and reduction values were produced by FFmpeg and the application, not invented UI data.

<details>
<summary>Before importing a timeline</summary>

![decut before importing an OTIO timeline](assets/screenshots/empty.png)

</details>

## Features

- Linked video, game audio and voice cuts through explicit track groups.
- FFmpeg silence detection using one reference audio track per group.
- Filled, linear waveforms with take boundaries, zoom, pan and display-only gain.
- Manual noise sampling: moving the needle never starts calibration or creates a cut.
- Separate **Sample here**, **Recalculate preview** and **Apply reviewed cuts** actions.
- Editable cut boundaries, individual checkboxes and **Keep this region** protection.
- Three gap-compaction modes, independent of silence analysis.
- Original/current duration, removed time, reduction percentage and interval counts.
- Suggested export filenames and a remembered export folder.
- Portuguese and English interface, cancellable background work and Linux AppImage packaging.

> Detection is amplitude-based, not speech recognition. Quiet speech can be mistaken for silence. Always review the proposed cuts and keep your original timeline.

## Quick start

### From source

Requirements: Python **3.10+**, a working Qt desktop environment and **FFmpeg** in `PATH`. Development has been tested with Python 3.12 on Linux.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
bash run_otio_tool.sh
```

To check the version without opening a window:

```bash
bash run_otio_tool.sh --version
# decut 1.0.0
```

For a custom FFmpeg installation:

```bash
DECUT_FFMPEG=/absolute/path/to/ffmpeg bash run_otio_tool.sh
```

### AppImage

If you have built or obtained `decut.AppImage`:

```bash
chmod +x decut.AppImage
./decut.AppImage
```

The AppImage bundles the Python application and Qt dependencies. FFmpeg is included when present on the build machine; otherwise install it on the target machine. See [build instructions](docs/development.md#appimage-build).

## Kdenlive → decut → Kdenlive

1. Import and synchronize your media in Kdenlive.
2. Export the timeline as **OpenTimelineIO (`.otio`)**.
3. Open it in decut and assign matching video/audio tracks to a group.
4. Mark **Analyze** on **one reference track per group**, usually the microphone. Leave game audio unchecked.
5. Click **Preview cuts** or **Remove silence**. Both open the same nondestructive review window.
6. Inspect the red regions. Optionally sample noise and recalculate, edit boundaries or uncheck cuts.
7. Click **Apply reviewed cuts**. Selected intervals are removed and closed across the group's tracks together.
8. Export the result and import the new `.otio` in Kdenlive. Render your final video there.

Example:

| Track | Group | Analyze |
|---|---|---|
| Gameplay video | A1 | No |
| Game audio | A1 | No |
| Microphone | A1 | Yes |
| Second camera | A2 | No |
| Second reference audio | A2 | Yes |

Groups are independent: cutting `A1` does not change `A2` or unassigned tracks. Put everything that must remain synchronized in the same group.

## Manual sampling and review

![Four explicit actions: position, measure, recalculate and apply](assets/screenshots/sampling-workflow.svg)

1. Place the amber needle in a quiet region without speech. Click/drag or enter a position in seconds. **This does not measure or modify cuts.**
2. Choose the sample window and click **Sample here**. Only the threshold setting is updated.
3. Click **Recalculate preview** to generate new proposals. Until then, Apply is disabled when the settings are stale.
4. Uncheck cuts, edit start/end times or choose **Keep this region** at the needle. Protected regions remain unchecked on subsequent recalculations in that review.
5. Click **Apply reviewed cuts** only when satisfied. **Close without applying** discards the review's pending edits.

**Clear needle**, `Esc` or `Delete` hides the needle and amber sample window without changing cuts. The amber area is **not** a deletion. Only checked red intervals are applied.

Loud samples show a warning and require **Use this sample anyway**. A sample overlapping speech can suggest a dangerously high threshold; prefer a window wholly inside the pause.

### What does sampling actually change?

It measures **only the amber window**, then proposes an amplitude threshold. It does **not** search for similar-sounding words or audio, and it does not automatically delete that window.

**Recalculate preview** analyzes the reference audio for its group using the new threshold. It **replaces** the previous automatic proposals rather than adding cuts endlessly. Previously protected regions stay protected during the review. Depending on the settings, proposals can appear, disappear or change boundaries.

### Visual example: measure first, then recalculate

**Needle positioned:** the original proposal is unchanged. No measurement runs just because the line moves.

![Needle placed in a quiet region, with no sample measured and the old proposal unchanged](assets/screenshots/needle.png)

**Sample here clicked:** the measured peak suggests **−47 dB**, but the existing cut list is still unchanged. Recalculation is a separate action.

![Measured peak and proposed threshold, with an instruction to explicitly recalculate](assets/screenshots/sampling.png)

**After Recalculate preview:** the review screenshot above shows **3 proposals** and **2.483 seconds** of projected savings. The prior automatic plan was replaced, not added on top.

### How the calculations work

![Measured peak plus headroom gives the threshold, and original minus final duration gives saved time and reduction](assets/screenshots/calculations.svg)

**After applying the reviewed plan**, the statistics panel shows the actual timeline reduction:

![Actual statistics: 60 seconds original, 57.52 seconds current, 2.48 seconds removed, 4.1 percent reduction and three intervals](assets/screenshots/statistics.png)

Read the [complete user guide](docs/user-guide.md) for settings, controls, statistics, saving behavior and troubleshooting.

## Compact existing gaps

Use **Compact timeline** for holes left after manual editing in Kdenlive. Open **Show advanced options** to select the rule:

| Mode | What defines a removable gap? |
|---|---|
| Video defines gaps | No video clip occupies the interval on any video track. Continuous audio in that interval is also removed. |
| Only gaps empty on every track | No track has a clip there. Continuous audio prevents removal. |
| Selected track defines gaps | The selected reference track is empty there, regardless of content on other tracks. |

Compaction applies the same gap intervals to **every track**, not just a group. It does not detect silence inside clips. See [compaction details and cautions](docs/user-guide.md#gap-compaction).

## Export and saved preferences

- Opening `ep3.otio` suggests **`ep3_exported.otio`**.
- The file dialog remembers the last successful export folder across restarts.
- You still confirm saving and any overwrite in the file dialog.
- Track group and Analyze assignments are stored in the exported OTIO metadata.
- Source media remains externally referenced, not copied into the OTIO.
- Calibration, review checkboxes/protection and the language choice are not a separate autosaved project. Save the applied timeline before closing.

## Development and release notes

```bash
.venv/bin/python -m unittest discover -s tests -v
bash build_appimage.sh
```

- [Development, architecture, tests and packaging](docs/development.md)
- [User guide and known limitations](docs/user-guide.md)
- [1.0.0 release notes](CHANGELOG.md)
- [MIT license](LICENSE)

Rebuild the AppImage after updating the source; an existing binary is not changed automatically.
