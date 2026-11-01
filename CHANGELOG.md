# Changelog

## 1.0.0 · 2026-11-01

### Added

- Python/PySide6 desktop workflow for Kdenlive OpenTimelineIO exports.
- Per-track groups with one reference audio controlling linked video/audio cuts.
- FFmpeg amplitude-based silence analysis and globally synchronized gap compaction with three reference modes.
- Nondestructive waveform review with filled linear peaks, take boundaries, zoom, pan and display-only gain.
- Manual sampling needle: measurement, proposal recalculation and final application are separate button actions.
- Optional incremental noise-profile matching, multiple stored samples, similarity tolerance and reset controls while retaining the existing threshold mode.
- Visible sample history, per-mode/group counters, numbered blue waveform references and click-to-locate sample rows.
- High-level sample warnings with explicit confirmation.
- Editable boundaries, per-cut enable/disable, persistent protection within a review and needle clearing.
- Timeline duration/reduction statistics and preview projections.
- Export filename suggestions and remembered output folder.
- English/Portuguese interface and cancellable background processing.
- AppImage assets, authoritative PyInstaller specification and documented build workflow.
- User/developer guides, English screenshots and automated Qt/FFmpeg pipeline tests.
- Version 1.0.0 in the GUI, `--version`, Qt metadata and AppImage metadata.

### Fixed

- Source-time to timeline-time mapping for split media references and mixed time rates.
- Protection margins expanding into speech instead of preserving its edges.
- Case-sensitive Video/Audio track recognition.
- Unsafe analysis of multiple references in one group and deletion of unrelated groups by an extra global compaction pass.
- Sample/waveform alignment, quiet-audio visibility and repeated-cut review behavior.
- Background process cancellation and orphan review windows on application close.
- Missing PyInstaller bundle files and AppImage desktop/icon/AppStream metadata.

### Known limitations

- Amplitude detection is not VAD or speech recognition.
- Audio audition, general undo, project-library autosave and persistent review sessions are not implemented.
- Complex Kdenlive/OTIO effects, nested compositions, transitions and alternate audio-stream selection are not fully supported.
- Source media stays externally referenced. Linux/AppImage compatibility depends on the build baseline and host libraries.

See the [user guide](docs/user-guide.md) for operational cautions and the [developer guide](docs/development.md) for packaging constraints.
