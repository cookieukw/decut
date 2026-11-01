#!/usr/bin/env python3
"""decut OTIO cutter: synchronize silence removal across named track groups."""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import opentimelineio as otio
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox,
    QDoubleSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QComboBox, QToolButton,
)


def seconds(value) -> float:
    return value.to_seconds() if hasattr(value, "to_seconds") else float(value)


def timecode(value: float) -> str:
    value = max(0.0, value); hours, rest = divmod(value, 3600); minutes, secs = divmod(rest, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:05.2f}"


def media_path(clip: otio.schema.Clip) -> str | None:
    ref = clip.media_reference
    url = getattr(ref, "target_url", "") if ref else ""
    if not url:
        return None
    if url.startswith("file://"):
        # Do not strip a fixed number of characters: file://localhost/foo and
        # percent-escaped paths are both valid file URLs.
        parsed = urlparse(url)
        path = parsed.path
        if parsed.netloc and parsed.netloc != "localhost":
            path = f"//{parsed.netloc}{path}"
        url = unquote(path)
    else:
        url = unquote(url)
    return os.path.abspath(url)


def detect_silence(path: str, threshold: float, minimum: float) -> list[tuple[float, float]]:
    ffmpeg = os.environ.get("DECUT_FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg was not found. Install it or set DECUT_FFMPEG to its path.")
    command = [
        ffmpeg, "-hide_banner", "-nostats", "-i", path,
        "-af", f"silencedetect=noise={threshold}dB:d={minimum}",
        "-f", "null", "-",
    ]
    # FFmpeg can emit a lot of diagnostics for a damaged or long input. Read
    # stderr incrementally instead of retaining the complete process log.
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    starts = deque()
    intervals = []
    start_pattern = re.compile(r"silence_start:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))")
    end_pattern = re.compile(r"silence_end:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))")
    assert process.stderr is not None
    for line in process.stderr:
        start_match = start_pattern.search(line)
        if start_match:
            starts.append(float(start_match.group(1)))
        end_match = end_pattern.search(line)
        if end_match and starts:
            start = starts.popleft()
            end = float(end_match.group(1))
            if end > start:
                intervals.append((start, end))
    process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg could not analyze {path} (exit code {process.returncode}).")
    return intervals


def clip_segments(clip_start: float, clip_end: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    kept = [(clip_start, clip_end)]
    for cut_start, cut_end in cuts:
        next_kept = []
        for start, end in kept:
            if cut_end <= start or cut_start >= end:
                next_kept.append((start, end))
                continue
            if start < cut_start:
                next_kept.append((start, min(cut_start, end)))
            if cut_end < end:
                next_kept.append((max(cut_end, start), end))
        kept = next_kept
    return [(start, end) for start, end in kept if end - start > 0.001]


def rebuild_track(track: otio.schema.Track, cuts: list[tuple[float, float]]) -> otio.schema.Track:
    result = otio.schema.Track(name=track.name, kind=track.kind, metadata=copy.deepcopy(track.metadata))
    cursor = 0.0
    for child in track:
        duration = seconds(child.duration())
        segments = clip_segments(cursor, cursor + duration, cuts)
        for start, end in segments:
            part = copy.deepcopy(child)
            local_offset = start - cursor
            part_duration = end - start
            if isinstance(part, otio.schema.Clip) and part.source_range:
                source = part.source_range
                part.source_range = otio.opentime.TimeRange(
                    start_time=source.start_time + otio.opentime.RationalTime.from_seconds(
                        local_offset, source.start_time.rate
                    ),
                    duration=otio.opentime.RationalTime.from_seconds(
                        part_duration, source.duration.rate
                    ),
                )
            elif isinstance(part, otio.schema.Gap):
                part.source_range = otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, 1),
                    duration=otio.opentime.RationalTime.from_seconds(part_duration, 1),
                )
            result.append(part)
        cursor += duration
    return result


def process_timeline(timeline: otio.schema.Timeline, assignments: list[dict], threshold: float, minimum: float, padding: float) -> tuple[otio.schema.Timeline, dict]:
    padding = max(0.0, float(padding))
    cuts_by_group: dict[str, list[tuple[float, float]]] = {}
    tracks = list(timeline.tracks)
    for item in assignments:
        if not item["analyze"] or not item["group"]:
            continue
        index = item["index"]
        if index >= len(tracks):
            continue
        track = tracks[index]
        cursor = 0.0
        group_cuts = cuts_by_group.setdefault(item["group"], [])
        for child in track:
            if isinstance(child, otio.schema.Clip):
                path = media_path(child)
                if path and Path(path).exists():
                    source_start = seconds(child.source_range.start_time) if child.source_range else 0.0
                    for start, end in detect_silence(path, threshold, minimum):
                        clip_start = cursor + start - source_start
                        clip_end = cursor + end - source_start
                        group_cuts.append((clip_start - padding, clip_end + padding))
            cursor += seconds(child.duration())
    for group, cuts in cuts_by_group.items():
        cuts.sort()
        merged = []
        for start, end in cuts:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((max(0, start), end))
        cuts_by_group[group] = merged

    # Build a fresh timeline instead of mutating a copy of the original stack.
    # This prevents Kdenlive from retaining the old one-hour container range.
    output = otio.schema.Timeline(
        name=timeline.name,
        global_start_time=copy.deepcopy(timeline.global_start_time),
        metadata=copy.deepcopy(timeline.metadata),
    )
    for index, track in enumerate(tracks):
        assignment = next((x for x in assignments if x["index"] == index), None)
        cuts = cuts_by_group.get(assignment["group"], []) if assignment else []
        output.tracks.append(rebuild_track(track, cuts) if cuts else copy.deepcopy(track))
    return output, {group: len(cuts) for group, cuts in cuts_by_group.items()}


def compact_timeline(timeline: otio.schema.Timeline, mode: str = "video", selected_index: int = 0) -> tuple[otio.schema.Timeline, int]:
    """Use video occupancy as the edit decision and remove those intervals from every track."""
    occupied = []
    video_tracks = [track for track in timeline.tracks if track.kind == "video"]
    if mode == "all":
        source_tracks = list(timeline.tracks)
    elif mode == "selected" and 0 <= selected_index < len(timeline.tracks):
        source_tracks = [timeline.tracks[selected_index]]
    else:
        source_tracks = video_tracks or list(timeline.tracks)
    for track in source_tracks:
        cursor = 0.0
        for child in track:
            duration = seconds(child.duration())
            if duration > 0.001 and not isinstance(child, otio.schema.Gap):
                occupied.append((cursor, cursor + duration))
            cursor += duration
    if not occupied:
        return copy.deepcopy(timeline), 0
    occupied.sort(); merged = []
    for start, end in occupied:
        if merged and start <= merged[-1][1] + 0.001:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    total = max((seconds(track.duration()) for track in timeline.tracks), default=0.0)
    holes, cursor = [], 0.0
    for start, end in merged:
        if start > cursor + 0.001: holes.append((cursor, start))
        cursor = max(cursor, end)
    if total > cursor + 0.001: holes.append((cursor, total))
    output = otio.schema.Timeline(name=timeline.name, global_start_time=copy.deepcopy(timeline.global_start_time), metadata=copy.deepcopy(timeline.metadata))
    for track in timeline.tracks:
        output.tracks.append(rebuild_track(track, holes))
    return output, len(holes)


class RenderThread(QThread):
    done = Signal(object, object)

    def __init__(self, timeline, assignments, threshold, minimum, padding):
        super().__init__(); self.timeline = timeline; self.assignments = assignments; self.threshold = threshold; self.minimum = minimum; self.padding = padding

    def run(self):
        try: self.done.emit(*process_timeline(self.timeline, self.assignments, self.threshold, self.minimum, self.padding))
        except Exception as error: self.done.emit(None, error)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.timeline = None; self.path = None; self.worker = None; self.original_duration = 0.0; self.last_operation_count = 0
        self.lang = "pt"
        self.setWindowTitle("decut · OTIO silence cutter"); self.resize(1000, 680)
        root = QWidget(); layout = QVBoxLayout(root)
        top = QHBoxLayout(); self.open_btn = QPushButton(); self.save_btn = QPushButton(); self.save_btn.setEnabled(False); self.compact_btn = QPushButton(); self.compact_btn.setEnabled(False); self.analyze_btn = QPushButton(); self.analyze_btn.setEnabled(False); self.language = QComboBox(); self.language.addItem("Português", "pt"); self.language.addItem("English", "en"); top.addWidget(self.open_btn); top.addWidget(self.save_btn); top.addStretch(); top.addWidget(self.compact_btn); top.addWidget(self.analyze_btn); top.addWidget(self.language); layout.addLayout(top)
        self.title = QLabel("Abra uma timeline exportada pelo Kdenlive"); self.title.setObjectName("title"); layout.addWidget(self.title)
        self.table = QTableWidget(0, 4); self.table.horizontalHeader().setStretchLastSection(True); layout.addWidget(self.table)
        settings = QGroupBox(); form = QFormLayout(settings); self.settings = settings; self.threshold = QSpinBox(); self.threshold.setRange(-80, -10); self.threshold.setValue(-40); self.minimum = QDoubleSpinBox(); self.minimum.setRange(.05, 30); self.minimum.setSingleStep(.05); self.minimum.setValue(.6); self.padding = QSpinBox(); self.padding.setRange(0, 2000); self.padding.setValue(180); self.threshold_label = QLabel(); self.minimum_label = QLabel(); self.padding_label = QLabel(); form.addRow(self.threshold_label, self.threshold); form.addRow(self.minimum_label, self.minimum); form.addRow(self.padding_label, self.padding); layout.addWidget(settings)
        compact_box = QGroupBox(); compact_form = QFormLayout(compact_box); self.compact_box = compact_box; self.compact_mode = QComboBox(); self.compact_mode.addItem("Vídeo define os buracos", "video"); self.compact_mode.addItem("Só gaps vazios em todas as tracks", "all"); self.compact_mode.addItem("Track escolhida define os buracos", "selected"); self.compact_track = QComboBox(); self.compact_info = QToolButton(); self.compact_info.setText("ⓘ"); self.compact_info.setToolTip("Clique para ver a diferença entre os modos."); mode_wrap = QWidget(); mode_layout = QHBoxLayout(mode_wrap); mode_layout.setContentsMargins(0, 0, 0, 0); mode_layout.addWidget(self.compact_mode); mode_layout.addWidget(self.compact_info); self.compact_mode_label = QLabel(); self.compact_track_label = QLabel(); compact_form.addRow(self.compact_mode_label, mode_wrap); compact_form.addRow(self.compact_track_label, self.compact_track); self.compact_info.clicked.connect(self.show_compaction_help); layout.addWidget(compact_box)
        stats = QGroupBox(); stats_form = QFormLayout(stats); self.stats_box = stats; self.original_label = QLabel("—"); self.current_label = QLabel("—"); self.removed_label = QLabel("—"); self.percent_label = QLabel("—"); self.operation_label = QLabel("—"); self.original_text = QLabel(); self.current_text = QLabel(); self.removed_text = QLabel(); self.percent_text = QLabel(); self.operation_text = QLabel(); stats_form.addRow(self.original_text, self.original_label); stats_form.addRow(self.current_text, self.current_label); stats_form.addRow(self.removed_text, self.removed_label); stats_form.addRow(self.percent_text, self.percent_label); stats_form.addRow(self.operation_text, self.operation_label); layout.addWidget(stats)
        self.status = QLabel("Os vídeos e áudios permanecem referenciados pelo OTIO; a ferramenta não renderiza arquivos intermediários."); layout.addWidget(self.status); self.setCentralWidget(root)
        self.open_btn.clicked.connect(self.open); self.save_btn.clicked.connect(self.save); self.compact_btn.clicked.connect(self.compact); self.analyze_btn.clicked.connect(self.analyze); self.language.currentIndexChanged.connect(lambda index: self.set_language(self.language.itemData(index))); self.set_language("pt")

    def set_language(self, language):
        self.lang = language
        if language == "en":
            self.open_btn.setText("Open .otio"); self.save_btn.setText("Export .otio"); self.compact_btn.setText("Compact timeline"); self.analyze_btn.setText("Remove silence"); self.title.setText("Open a timeline exported by Kdenlive"); self.table.setHorizontalHeaderLabels(["Track", "Type", "Group", "Analyze"]); self.settings.setTitle("Detection"); self.threshold_label.setText("Threshold (dB)"); self.minimum_label.setText("Minimum pause (s)"); self.padding_label.setText("Protection (ms)"); self.compact_box.setTitle("Compaction"); self.compact_mode.setItemText(0, "Video defines gaps"); self.compact_mode.setItemText(1, "Only gaps empty on every track"); self.compact_mode.setItemText(2, "Selected track defines gaps"); self.compact_mode_label.setText("Compaction mode"); self.compact_track_label.setText("Reference track"); self.original_text.setText("Original duration"); self.current_text.setText("Current duration"); self.removed_text.setText("Time removed"); self.percent_text.setText("Reduction"); self.operation_text.setText("Intervals removed")
        else:
            self.open_btn.setText("Abrir .otio"); self.save_btn.setText("Exportar .otio"); self.compact_btn.setText("Compactar timeline"); self.analyze_btn.setText("Remover silêncios"); self.title.setText("Abra uma timeline exportada pelo Kdenlive"); self.table.setHorizontalHeaderLabels(["Track", "Tipo", "Grupo", "Analisar"]); self.settings.setTitle("Detecção"); self.threshold_label.setText("Limite (dB)"); self.minimum_label.setText("Pausa mínima (s)"); self.padding_label.setText("Proteção (ms)"); self.compact_box.setTitle("Compactação"); self.compact_mode.setItemText(0, "Vídeo define os buracos"); self.compact_mode.setItemText(1, "Só gaps vazios em todas as tracks"); self.compact_mode.setItemText(2, "Track escolhida define os buracos"); self.compact_mode_label.setText("Modo de compactação"); self.compact_track_label.setText("Track de referência"); self.original_text.setText("Duração original"); self.current_text.setText("Duração atual"); self.removed_text.setText("Tempo removido"); self.percent_text.setText("Redução"); self.operation_text.setText("Intervalos removidos")

    def show_compaction_help(self):
        if self.lang == "en":
            text = ("Video defines gaps: use video occupancy as the edit decision and remove those intervals from every track.\n\n"
                    "Only gaps empty on every track: preserve an interval if any track has content there.\n\n"
                    "Selected track defines gaps: use the chosen track as the edit decision and apply its gaps to every track.")
            QMessageBox.information(self, "Compaction modes", text)
        else:
            text = ("Vídeo define os buracos: usa a ocupação do vídeo como referência e remove esses intervalos de todas as tracks.\n\n"
                    "Só gaps vazios em todas as tracks: preserva o intervalo se qualquer track tiver conteúdo nele.\n\n"
                    "Track escolhida define os buracos: usa a track selecionada como referência e aplica os buracos em todas as tracks.")
            QMessageBox.information(self, "Modos de compactação", text)

    def open(self):
        path, _ = QFileDialog.getOpenFileName(self, "Abrir OpenTimelineIO", "", "OpenTimelineIO (*.otio *.json);;Todos (*.*)")
        if not path: return
        try:
            self.timeline = otio.adapters.read_from_file(path); self.path = path; self.title.setText(f"{self.timeline.name or Path(path).stem} · {len(self.timeline.tracks)} tracks"); self.populate(); self.save_btn.setEnabled(True); self.compact_btn.setEnabled(True); self.analyze_btn.setEnabled(self.table.rowCount() > 0); self.status.setText("Assign the same group to video and audio tracks that should be cut together." if self.lang == "en" else "Defina o mesmo grupo para o vídeo e o áudio que devem cortar juntos.")
            self.original_duration = seconds(self.timeline.duration()); self.last_operation_count = 0; self.update_stats()
        except Exception as error: QMessageBox.critical(self, "Falha ao abrir OTIO", str(error))

    def populate(self):
        self.table.setRowCount(0); self.compact_track.clear()
        for row, track in enumerate(self.timeline.tracks):
            name = track.name or f"Track {row + 1}"; self.table.insertRow(row); self.table.setItem(row, 0, QTableWidgetItem(name)); self.table.setItem(row, 1, QTableWidgetItem(track.kind or "unknown")); group = QLineEdit(track.metadata.get("decut", {}).get("group", "A1")); self.table.setCellWidget(row, 2, group); check = QCheckBox(); check.setChecked(track.kind == "audio"); self.table.setCellWidget(row, 3, check); self.compact_track.addItem(name, row)

    def assignments(self):
        result = []
        for row in range(self.table.rowCount()): result.append({"index": row, "group": self.table.cellWidget(row, 2).text().strip(), "analyze": self.table.cellWidget(row, 3).isChecked()})
        return result

    def analyze(self):
        if self.worker and self.worker.isRunning():
            return
        self.analyze_btn.setEnabled(False); self.status.setText("Analyzing audio with FFmpeg…" if self.lang == "en" else "Analisando áudio com FFmpeg…"); self.worker = RenderThread(self.timeline, self.assignments(), self.threshold.value(), self.minimum.value(), self.padding.value() / 1000); self.worker.done.connect(self.finished); self.worker.start()

    def compact(self):
        if not self.timeline: return
        self.timeline, holes = compact_timeline(self.timeline, self.compact_mode.currentData(), self.compact_track.currentData() or 0)
        self.last_operation_count = holes; self.update_stats(); self.status.setText((f"Removidos {holes} buracos globais da timeline." if self.lang == "pt" else f"Removed {holes} global timeline gaps."))

    def update_stats(self):
        current = seconds(self.timeline.duration()) if self.timeline else 0.0
        # Keep the statistics physically consistent: removed + current = original.
        original = max(0.0, self.original_duration)
        current = min(current, original) if original else current
        removed = max(0.0, original - current)
        percent = min(100.0, (removed / original * 100.0)) if original else 0.0
        self.original_label.setText(timecode(original)); self.current_label.setText(timecode(current)); self.removed_label.setText(timecode(removed)); self.percent_label.setText(f"{percent:.1f}%"); self.operation_label.setText(str(self.last_operation_count))

    def finished(self, timeline, result):
        self.analyze_btn.setEnabled(True)
        if timeline is None: QMessageBox.critical(self, "Analysis failed" if self.lang == "en" else "Falha na análise", str(result)); return
        self.timeline = timeline; self.last_operation_count = sum(result.values()); self.update_stats(); self.status.setText(("Cuts applied by group: " + (", ".join(f"{key}: {value}" for key, value in result.items()) or "no silence found")) if self.lang == "en" else ("Cortes aplicados em grupos: " + (", ".join(f"{key}: {value}" for key, value in result.items()) or "nenhum silêncio encontrado"))); self.save_btn.setEnabled(True)

    def save(self):
        if not self.timeline: return
        path, _ = QFileDialog.getSaveFileName(self, "Exportar timeline OTIO", "timeline_decut.otio", "OpenTimelineIO (*.otio)")
        if path:
            for row, track in enumerate(self.timeline.tracks): track.metadata.setdefault("decut", {})["group"] = self.table.cellWidget(row, 2).text().strip()
            otio.adapters.write_to_file(self.timeline, path); self.status.setText(f"Exported: {path}" if self.lang == "en" else f"Exportado: {path}")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait()
        event.accept()


def main():
    app = QApplication(sys.argv); app.setStyleSheet("QWidget{font-family:'IBM Plex Sans';font-size:12px;background:#151817;color:#e4ebe5} QMainWindow{background:#111313} QPushButton{background:#2b3c2e;border:1px solid #465948;border-radius:5px;padding:8px 12px} QPushButton:hover{background:#3b523d} QLineEdit,QSpinBox,QDoubleSpinBox,QTableWidget{background:#202522;border:1px solid #3a443d;border-radius:4px;padding:5px} QHeaderView::section{background:#252c28;color:#9fac9f;padding:7px;border:0} #title{font-size:18px;font-weight:600;padding:10px 0}"); window = MainWindow(); window.show(); sys.exit(app.exec())


if __name__ == "__main__": main()
