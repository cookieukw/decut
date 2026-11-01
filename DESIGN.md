# Design

## Context

A creator reviews an hour-long gameplay recording at a desktop between Kdenlive edits; the existing low-glare dark Qt surface must remain consistent across review and settings.

## Visual system

- Restrained neutral surfaces with green for waveform content and primary actions.
- Red translucent ranges identify proposed removals; labels and timecodes explain their meaning without relying solely on color.
- Amber identifies a sampling cursor or a warning, not a deletion.
- Use the existing IBM Plex Sans/system Qt font for controls and readable numeric timecodes.
- Put review controls in compact toolbars and preserve vertical space for tracks.
- Keep advanced settings collapsed by default; avoid decorative cards or redundant summaries.

## Interaction

Preview recalculation must not mutate the current timeline. Confirm edits with an explicit apply action. Preserve keyboard access, busy states and errors. Use native Qt widgets and simple separators.
