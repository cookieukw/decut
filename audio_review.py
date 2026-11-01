"""Interactive OTIO review: sampling is nondestructive until Apply is accepted."""
import copy
import math
from pathlib import Path

import opentimelineio as otio
from PySide6.QtCore import Qt, QThread, Signal, QRectF, QTimer, QElapsedTimer, Slot
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QSplitter, QProgressBar,
)

from audio_tools import measure_noise, sample_window, waveform_peaks
from media_process import JobControl, AnalysisCancelled


def tc(value):
    hours, rest = divmod(max(0, value), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours):02}:{int(minutes):02}:{secs:06.3f}"


def projected_duration(timeline, assignments, cuts_by_group):
    """Compute the preview without cloning/splitting an entire OTIO on the GUI thread."""
    groups = {entry["index"]: entry["group"] for entry in assignments}
    durations = []
    for index, track in enumerate(timeline.tracks):
        duration = track.duration().to_seconds()
        cursor, removed = 0.0, 0.0
        for start, end in sorted(cuts_by_group.get(groups.get(index), [])):
            start, end = max(0.0, start, cursor), min(duration, end)
            if end > start:
                removed += end - start
                cursor = end
        durations.append(max(0.0, duration - removed))
    return max(durations, default=0.0)


class ReviewJob(QThread):
    progress = Signal(str, float, float)

    def __init__(self, task, callback, parent):
        super().__init__(parent)
        self.task = task
        self.callback = callback
        self.control = JobControl(self.progress.emit)
        self.data, self.error = None, None

    def run(self):
        try:
            self.data = self.task(self.control)
        except Exception as error:
            self.error = error


class SamplingWaveform(QWidget):
    selected = Signal(float)
    selection_cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.segments, self.cuts = [], []
        self.duration, self.cursor = 1.0, 0.0
        self.view_start, self.view_duration = 0.0, 1.0
        self.sample_width = .6
        self.display_gain = 2.0
        self.has_selection = False
        self._panning = False
        self.setMinimumHeight(200)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.CrossCursor)
        self.setAccessibleName("Waveform sampling cursor")

    def set_audio(self, segments, duration):
        self.segments = segments
        self.duration = max(duration, .001)
        self.has_selection = False
        self.fit()

    def fit(self):
        self.view_start, self.view_duration = 0.0, self.duration
        self.update()

    def at_x(self, x):
        return max(0, min(self.duration, self.view_start + x / max(1, self.width()) * self.view_duration))

    def x_at(self, time):
        return (time - self.view_start) / self.view_duration * self.width()

    def amplitude(self, peak):
        # Linear, absolute scale. Never normalize a quiet take to full height.
        return min(1.0, max(0.0, peak) * self.display_gain)

    def set_display_gain(self, gain):
        self.display_gain = gain
        self.update()

    def clear_selection(self):
        self.has_selection = False
        self.update()
        self.selection_cleared.emit()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#151b18"))
        bottom = max(40, self.height() - 28)
        top = 24
        mid = (top + bottom) / 2
        radius = (bottom - top) * .44
        for segment in self.segments:
            clip_left = max(0, self.x_at(segment["start"]))
            clip_right = min(self.width(), self.x_at(segment["start"] + segment["duration"]))
            if clip_right <= clip_left:
                continue
            clip_rect = QRectF(clip_left, 4, clip_right - clip_left, bottom - 4)
            painter.fillRect(clip_rect, QColor("#164b3d"))
            painter.setPen(QPen(QColor("#377b65"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(clip_rect)
            painter.setPen(QPen(QColor("#286451"), 1))
            painter.drawLine(int(clip_left), int(mid), int(clip_right), int(mid))
            painter.setPen(QColor("#d4e4dc"))
            name = segment.get("name") or Path(segment.get("path", "audio")).name
            label = painter.fontMetrics().elidedText(name, Qt.ElideRight, max(0, int(clip_right - clip_left - 12)))
            painter.drawText(QRectF(clip_left + 6, 5, max(0, clip_right - clip_left - 12), 18),
                             Qt.AlignLeft | Qt.AlignVCenter, label)
            peaks = segment["peaks"]
            step = segment["duration"] / max(1, len(peaks))
            if step <= 0:
                continue
            first = max(0, int((self.view_start - segment["start"]) / step))
            last = min(len(peaks), math.ceil((self.view_start + self.view_duration - segment["start"]) / step))
            stride = max(1, math.ceil(max(0, last - first) / max(1, self.width())))
            points = []
            for i in range(first, last, stride):
                peak = max(peaks[i:min(last, i + stride)], default=0)
                x = self.x_at(segment["start"] + (i + .5) * step)
                points.append((max(clip_left, min(clip_right, x)), self.amplitude(peak) * radius))
            if points:
                envelope = QPainterPath()
                envelope.moveTo(points[0][0], mid)
                for x, amplitude in points:
                    envelope.lineTo(x, mid - amplitude)
                for x, amplitude in reversed(points):
                    envelope.lineTo(x, mid + amplitude)
                envelope.closeSubpath()
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#3aa383"))
                painter.drawPath(envelope)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(196, 65, 59, 100))
        for start, end in self.cuts:
            x, right = max(0, self.x_at(start)), min(self.width(), self.x_at(end))
            if right > x:
                painter.drawRect(QRectF(x, 4, right - x, bottom - 4))
        painter.setBrush(QColor(227, 183, 108, 45))
        segment = next((s for s in self.segments if s["start"] <= self.cursor < s["start"] + s["duration"]), None)
        if self.has_selection and segment and segment["duration"] >= .05:
            start, duration = sample_window(segment["start"], segment["start"] + segment["duration"],
                                            self.cursor, self.sample_width)
            left, right = max(0, self.x_at(start)), min(self.width(), self.x_at(start + duration))
            if right > left:
                painter.drawRect(QRectF(left, 4, right - left, bottom - 4))
        if self.has_selection:
            painter.setPen(QPen(QColor("#e3b76c"), 2))
            x = int(self.x_at(self.cursor))
            painter.drawLine(x, 0, x, bottom)
        painter.setPen(QColor("#adb9b0"))
        for i in range(5):
            time = self.view_start + i * self.view_duration / 4
            painter.drawText(int(i * max(0, self.width() - 105) / 4), self.height() - 8, tc(time))

    def mousePressEvent(self, event):
        self.setFocus()
        if event.button() == Qt.MiddleButton:
            self._panning, self._pan_x = True, event.position().x()
        elif event.button() == Qt.LeftButton:
            self.has_selection = True
            self.cursor = self.at_x(event.position().x())
            self.update()

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = (event.position().x() - self._pan_x) / max(1, self.width()) * self.view_duration
            self.view_start = max(0, min(self.duration - self.view_duration, self.view_start - delta))
            self._pan_x = event.position().x()
        elif event.buttons() & Qt.LeftButton:
            self.has_selection = True
            self.cursor = self.at_x(event.position().x())
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._panning = False
        elif event.button() == Qt.LeftButton:
            self.has_selection = True
            self.cursor = self.at_x(event.position().x())
            self.selected.emit(self.cursor)
            self.update()

    def wheelEvent(self, event):
        anchor = self.at_x(event.position().x())
        fraction = event.position().x() / max(1, self.width())
        factor = .7 if event.angleDelta().y() > 0 else 1 / .7
        self.view_duration = max(min(.5, self.duration), min(self.duration, self.view_duration * factor))
        self.view_start = max(0, min(self.duration - self.view_duration, anchor - fraction * self.view_duration))
        self.update()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Left, Qt.Key_Right):
            self.has_selection = True
            delta = -.1 if event.key() == Qt.Key_Left else .1
            self.cursor = max(0, min(self.duration, self.cursor + delta))
            self.update()
            self.selected.emit(self.cursor)
        elif event.key() in (Qt.Key_Escape, Qt.Key_Delete):
            self.clear_selection()
        else:
            super().keyPressEvent(event)


class AudioReviewDialog(QDialog):
    def __init__(self, timeline, assignments, threshold, minimum, padding,
                 collect_cuts, apply_cuts, media_path, language="pt", parent=None):
        super().__init__(parent)
        # The parent is disabled while this dialog is open. Keep a read-only
        # reference; copying a large OTIO here would block the GUI on opening.
        self.timeline = timeline
        self.assignments = copy.deepcopy(assignments)
        self.collect_cuts, self.apply_cuts, self.media_path = collect_cuts, apply_cuts, media_path
        self.lang = language
        self.default_threshold = threshold
        self.thresholds = {}
        self.cuts, self.audio, self.cache, self.wave_cache = {}, {}, {}, {}
        self.review_rows = {}
        self.protected_ranges = {}
        self.output, self.counts, self.job = None, {}, None
        self._busy = False
        self._dirty = False
        self._displayed_group = None
        self._pending_sample = None
        self._closing = False
        self._stage = "analysis"
        self.resize(1100, 720)
        self.setWindowTitle(self.tr_text("Revisar e calibrar cortes", "Review and calibrate cuts"))
        layout = QVBoxLayout(self)
        self.hint = QLabel(self.tr_text(
            "Posicione a agulha, clique em Medir aqui e depois em Recalcular prévia. Mover a agulha não calcula nem cria cortes.",
            "Position the needle, click Sample here, then Recalculate preview. Moving the needle does not analyze or create cuts."))
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        top = QHBoxLayout()
        self.reference = QComboBox()
        for entry in assignments:
            if entry["analyze"] and entry["group"]:
                track = timeline.tracks[entry["index"]]
                self.reference.addItem(f"{entry['group']} · {track.name or entry['index'] + 1}", entry)
        top.addWidget(self.reference, 1)
        self.visual_gain = QDoubleSpinBox()
        self.visual_gain.setRange(.5, 16)
        self.visual_gain.setSingleStep(.5)
        self.visual_gain.setValue(2)
        self.visual_gain.setSuffix("×")
        self.visual_gain.setToolTip(self.tr_text(
            "Amplifica só o desenho da waveform. Não altera o áudio nem a detecção.",
            "Amplifies the waveform display only. Does not change audio or detection."))
        top.addWidget(QLabel(self.tr_text("Ganho visual", "Display gain")))
        top.addWidget(self.visual_gain)
        self.fit_btn = QPushButton(self.tr_text("Enquadrar", "Fit waveform"))
        top.addWidget(self.fit_btn)
        layout.addLayout(top)
        self.wave = SamplingWaveform()
        self.visual_gain.valueChanged.connect(self.wave.set_display_gain)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.wave)
        self.intervals = QTableWidget(0, 3)
        self.intervals.setHorizontalHeaderLabels([self.tr_text("Remover", "Remove"), self.tr_text("Início", "Start"), self.tr_text("Fim", "End")])
        self.intervals.horizontalHeader().setStretchLastSection(True)
        splitter.addWidget(self.intervals)
        splitter.setSizes([320, 180])
        layout.addWidget(splitter, 1)
        legend = QLabel(self.tr_text(
            "Verde: áudio · Vermelho: corte proposto · Amarelo: amostra, não é um corte",
            "Green: audio · Red: proposed cut · Amber: sample, not a cut"))
        legend.setWordWrap(True)
        layout.addWidget(legend)
        form = QFormLayout()
        controls = QHBoxLayout()
        self.threshold = QSpinBox()
        self.threshold.setRange(-80, 0)
        self.threshold.setValue(threshold)
        self.threshold.setSuffix(" dB")
        self.minimum = QDoubleSpinBox()
        self.minimum.setRange(.05, 30)
        self.minimum.setValue(minimum)
        self.minimum.setSuffix(" s")
        self.padding = QSpinBox()
        self.padding.setRange(0, 2000)
        self.padding.setValue(round(padding * 1000))
        self.padding.setSuffix(" ms")
        for label, widget in [(self.tr_text("Limiar", "Threshold"), self.threshold),
                              (self.tr_text("Pausa mínima", "Minimum pause"), self.minimum),
                              (self.tr_text("Proteção", "Protection"), self.padding)]:
            controls.addWidget(QLabel(label))
            controls.addWidget(widget)
        form.addRow(controls)
        sampling = QHBoxLayout()
        self.position = QDoubleSpinBox()
        self.position.setDecimals(3)
        self.position.setRange(0, max(.001, timeline.duration().to_seconds()))
        self.position.setSuffix(" s")
        self.window = QDoubleSpinBox()
        self.window.setRange(.1, 2)
        self.window.setSingleStep(.1)
        self.window.setValue(.6)
        self.window.setSuffix(" s")
        self.sample_btn = QPushButton(self.tr_text("Medir aqui", "Sample here"))
        self.clear_needle_btn = QPushButton(self.tr_text("Limpar agulha", "Clear needle"))
        self.preserve_btn = QPushButton(self.tr_text("Não cortar aqui", "Keep this region"))
        self.preserve_btn.setToolTip(self.tr_text(
            "Desmarca os cortes propostos que contêm a posição da agulha. Pode reativá-los na lista.",
            "Uncheck proposed cuts containing the needle position. You can re-enable them in the list."))
        sampling.addWidget(QLabel(self.tr_text("Agulha", "Needle")))
        sampling.addWidget(self.position)
        sampling.addWidget(QLabel(self.tr_text("Janela", "Window")))
        sampling.addWidget(self.window)
        sampling.addWidget(self.sample_btn)
        sampling.addWidget(self.clear_needle_btn)
        sampling.addWidget(self.preserve_btn)
        form.addRow(sampling)
        layout.addLayout(form)
        self.measurement = QLabel(self.tr_text("Nenhuma amostra ainda.", "No samples yet."))
        self.measurement.setWordWrap(True)
        layout.addWidget(self.measurement)
        self.confirm_sample_btn = QPushButton(self.tr_text(
            "Usar esta amostra mesmo assim", "Use this sample anyway"))
        self.confirm_sample_btn.setToolTip(self.tr_text(
            "Usa o nível medido como limiar. Recalcule a prévia para ver o resultado; não aplica cortes.",
            "Use the measured level as the threshold. Recalculate the preview to see the result; does not apply cuts."))
        self.confirm_sample_btn.hide()
        self.confirm_sample_btn.clicked.connect(self.confirm_sample)
        layout.addWidget(self.confirm_sample_btn)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.progress_text = QLabel()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        layout.addWidget(self.progress_text)
        layout.addWidget(self.progress_bar)
        self.progress_text.hide()
        self.progress_bar.hide()
        self.elapsed = QElapsedTimer()
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(500)
        self.progress_timer.timeout.connect(self.update_progress_text)
        bottom = QHBoxLayout()
        self.recalculate = QPushButton(self.tr_text("Recalcular prévia", "Recalculate preview"))
        self.apply_btn = QPushButton(self.tr_text("Aplicar cortes revisados", "Apply reviewed cuts"))
        self.cancel_btn = QPushButton(self.tr_text("Fechar sem aplicar", "Close without applying"))
        bottom.addWidget(self.recalculate)
        bottom.addStretch()
        bottom.addWidget(self.cancel_btn)
        bottom.addWidget(self.apply_btn)
        layout.addLayout(bottom)
        self.samples = []
        self.wave.selected.connect(self.needle_selected)
        self.wave.selection_cleared.connect(self.selection_cleared)
        self.position.valueChanged.connect(self.position_changed)
        self.window.valueChanged.connect(self.window_changed)
        self.reference.currentIndexChanged.connect(self.reference_changed)
        self.fit_btn.clicked.connect(self.wave.fit)
        self.sample_btn.clicked.connect(self.sample_here)
        self.clear_needle_btn.clicked.connect(self.wave.clear_selection)
        self.preserve_btn.clicked.connect(self.preserve_selection)
        self.recalculate.clicked.connect(self.refresh)
        self.cancel_btn.clicked.connect(self.reject)
        self.apply_btn.clicked.connect(self.apply_review)
        self.threshold.valueChanged.connect(self.invalidate)
        self.minimum.valueChanged.connect(self.invalidate)
        self.padding.valueChanged.connect(self.invalidate)
        self.intervals.itemChanged.connect(self.interval_changed)
        self.refresh()

    def tr_text(self, pt, en):
        return en if self.lang == "en" else pt

    def set_busy(self, busy):
        self._busy = busy
        for widget in (self.reference, self.recalculate, self.sample_btn,
                       self.threshold, self.minimum, self.padding, self.position, self.window,
                       self.intervals, self.wave):
            widget.setEnabled(not busy)
        self.cancel_btn.setEnabled(not self._closing)
        for button in (self.sample_btn, self.clear_needle_btn, self.preserve_btn):
            button.setEnabled(not busy and self.wave.has_selection)
        self.confirm_sample_btn.setEnabled(not busy)
        self.cancel_btn.setText(self.tr_text("Cancelar e fechar", "Cancel and close") if busy
                                else self.tr_text("Fechar sem aplicar", "Close without applying"))
        self.apply_btn.setEnabled(not busy and not self._dirty and any(self.cuts.values()))

    def start_job(self, task, callback):
        if self._busy:
            return
        self.set_busy(True)
        self._stage = "analysis"
        self.elapsed.start()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.progress_text.show()
        self.progress_timer.start()
        self.update_progress_text()
        self.job = ReviewJob(task, callback, self)
        self.job.progress.connect(self.job_progress, Qt.QueuedConnection)
        self.job.finished.connect(self.finish_job, Qt.QueuedConnection)
        self.job.start()

    @Slot(str, float, float)
    def job_progress(self, stage, done, total):
        if not self._busy:
            return
        self._stage = stage
        if total > 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(round(done / total * 100))
        else:
            self.progress_bar.setRange(0, 0)
        self.update_progress_text()

    def update_progress_text(self):
        stages = {
            "analysis": self.tr_text("Analisando áudio", "Analyzing audio"),
            "waveform": self.tr_text("Gerando waveform do clipe", "Generating clip waveform"),
            "sample": self.tr_text("Medindo amostra", "Measuring sample"),
            "apply": self.tr_text("Aplicando cortes", "Applying cuts"),
        }
        text = stages.get(self._stage, self._stage)
        self.progress_text.setText(f"{text} · {self.elapsed.elapsed() / 1000:.0f}s")

    @Slot()
    def finish_job(self):
        job = self.job
        data, error, callback = job.data, job.error, job.callback
        job.deleteLater()
        self.job = None
        self.progress_timer.stop()
        self.progress_bar.hide()
        self.progress_text.hide()
        self.set_busy(False)
        if self._closing:
            super().reject()
            return
        if error:
            self._dirty = True
            self.summary.setText(str(error))
            self.apply_btn.setEnabled(False)
            return
        callback(data)

    def group(self):
        entry = self.reference.currentData()
        return entry["group"] if entry else ""

    def invalidate(self):
        self.clear_pending_sample()
        self._dirty = True
        self.apply_btn.setEnabled(False)
        self.summary.setText(self.tr_text("Configurações alteradas. Recalcule a prévia.", "Settings changed. Recalculate the preview."))

    def refresh(self):
        if self._busy or not self.reference.count():
            return
        self.thresholds[self.group()] = self.threshold.value()
        thresholds = dict(self.thresholds)
        minimum, padding = self.minimum.value(), self.padding.value() / 1000
        self.summary.setText(self.tr_text("Analisando e gerando waveform…", "Analyzing and generating waveform…"))

        def prepare(control):
            cuts = self.collect_cuts(self.timeline, self.assignments, self.default_threshold,
                                     minimum, padding, thresholds, self.cache, control=control)
            audio = {}
            for entry in self.assignments:
                control.check()
                if not entry["analyze"] or not entry["group"]:
                    continue
                track = self.timeline.tracks[entry["index"]]
                segments, cursor = [], 0.0
                for child in track:
                    control.check()
                    duration = child.duration().to_seconds()
                    if isinstance(child, otio.schema.Clip):
                        source_start = child.trimmed_range().start_time.to_seconds()
                        path = self.media_path(child)
                        key = (path, source_start, duration)
                        if key not in self.wave_cache:
                            bins = max(4096, min(200000, math.ceil(duration * 20)))
                            self.wave_cache[key] = waveform_peaks(path, source_start, duration, bins, control)
                        segments.append({"start": cursor, "duration": duration, "source_start": source_start,
                                         "path": path, "peaks": self.wave_cache[key]})
                    cursor += duration
                audio[entry["group"]] = (segments, max(.001, cursor))
            return cuts, audio

        self.start_job(prepare, self.preview_ready)

    def preview_ready(self, data):
        self._dirty = False
        self.cuts, self.audio = data
        self.review_rows = {}
        for group, cuts in self.cuts.items():
            protected = self.protected_ranges.get(group, [])
            rows = [(start, end, not any(start < b and end > a for a, b in protected))
                    for start, end in cuts]
            self.review_rows[group] = rows
            self.cuts[group] = [(start, end) for start, end, enabled in rows if enabled]
        self.reference_changed()

    def reference_changed(self):
        self.clear_pending_sample()
        self.threshold.blockSignals(True)
        self.threshold.setValue(self.thresholds.get(self.group(), self.default_threshold))
        self.threshold.blockSignals(False)
        segments, duration = self.audio.get(self.group(), ([], 1))
        self.position.setMaximum(duration)
        if self._displayed_group != self.group():
            self.wave.cursor = 0
            self.position.setValue(0)
            self.wave.set_audio(segments, duration)
            self._displayed_group = self.group()
        else:
            # Keep the learned point and zoom in view when recalculating.
            self.wave.segments = segments
            self.wave.duration = duration
            self.wave.update()
        for button in (self.sample_btn, self.clear_needle_btn, self.preserve_btn):
            button.setEnabled(not self._busy and self.wave.has_selection)
        self.load_intervals()

    def load_intervals(self):
        self.intervals.blockSignals(True)
        rows = self.review_rows.get(self.group(), [])
        self.intervals.setRowCount(len(rows))
        for row, (start, end, enabled) in enumerate(rows):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Checked if enabled else Qt.Unchecked)
            self.intervals.setItem(row, 0, check)
            start_item = QTableWidgetItem(f"{start:.6f}")
            end_item = QTableWidgetItem(f"{end:.6f}")
            start_item.setData(Qt.UserRole, start)
            end_item.setData(Qt.UserRole, end)
            self.intervals.setItem(row, 1, start_item)
            self.intervals.setItem(row, 2, end_item)
        self.intervals.blockSignals(False)
        self.update_summary()

    def interval_changed(self, item):
        selected = []
        rows = []
        try:
            for row in range(self.intervals.rowCount()):
                values = []
                for col in (1, 2):
                    cell = self.intervals.item(row, col)
                    original = cell.data(Qt.UserRole)
                    value = original if original is not None and cell.text() == f"{original:.6f}" else float(cell.text())
                    values.append(value)
                start, end = values
                enabled = self.intervals.item(row, 0).checkState() == Qt.Checked
                if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= self.wave.duration):
                    raise ValueError("Invalid cut range.")
                rows.append((start, end, enabled))
                if enabled:
                    selected.append((start, end))
        except (ValueError, AttributeError):
            self.apply_btn.setEnabled(False)
            self.summary.setText(self.tr_text("Intervalo inválido. Use segundos com início menor que o fim.", "Invalid range. Use seconds with start before end."))
            return
        self.cuts[self.group()] = selected
        self.review_rows[self.group()] = rows
        previous = self.protected_ranges.get(self.group(), [])
        self.protected_ranges[self.group()] = sorted(set([
            (a, b) for a, b in previous if not any(a < end and b > start for start, end in selected)
        ] + [(start, end) for start, end, enabled in rows if not enabled]))
        self.update_summary()

    def update_summary(self):
        current_cuts = self.cuts.get(self.group(), [])
        self.wave.cuts = current_cuts
        self.wave.update()
        self.counts = {group: len(cuts) for group, cuts in self.cuts.items()}
        before = self.timeline.duration().to_seconds()
        after = projected_duration(self.timeline, self.assignments, self.cuts)
        removed = max(0, before - after)
        percent = removed / before * 100 if before else 0
        prefix = self.tr_text("Prévia", "Preview")
        cut_label = self.tr_text("cortes", "cuts")
        self.summary.setText(f"{prefix}: {sum(self.counts.values())} {cut_label} · {tc(before)} → {tc(after)} · −{tc(removed)} ({percent:.1f}%)")
        self.apply_btn.setEnabled(not self._busy and not self._dirty and any(self.cuts.values()))

    def position_changed(self, position):
        self.clear_pending_sample()
        self.wave.cursor = position
        self.wave.has_selection = True
        self.wave.update()
        for button in (self.sample_btn, self.clear_needle_btn, self.preserve_btn):
            button.setEnabled(not self._busy)

    def window_changed(self, value):
        self.clear_pending_sample()
        self.wave.sample_width = value
        self.wave.update()

    def needle_selected(self, position):
        self.position.setValue(position)
        self.position_changed(self.position.value())

    def selection_cleared(self):
        self.clear_pending_sample()
        for button in (self.sample_btn, self.clear_needle_btn, self.preserve_btn):
            button.setEnabled(False)
        self.measurement.setText(self.tr_text(
            "Agulha limpa. Os cortes não foram alterados.", "Needle cleared. Cuts were not changed."))

    def preserve_selection(self):
        if self._busy or not self.wave.has_selection:
            return
        position = self.wave.cursor
        found = False
        for row in range(self.intervals.rowCount()):
            try:
                start = float(self.intervals.item(row, 1).text())
                end = float(self.intervals.item(row, 2).text())
            except (ValueError, AttributeError):
                continue
            if start <= position < end:
                self.intervals.item(row, 0).setCheckState(Qt.Unchecked)
                found = True
        self.measurement.setText(self.tr_text(
            "Corte desmarcado. Esse trecho será mantido." if found else "Não há corte proposto nessa posição.",
            "Cut unchecked. This region will be kept." if found else "No proposed cut at this position."))

    def sample_here(self):
        if self._busy:
            return
        if not self.wave.has_selection:
            self.measurement.setText(self.tr_text("Posicione a agulha primeiro.", "Position the needle first."))
            return
        self.clear_pending_sample()
        position, group = self.position.value(), self.group()
        segments = self.audio.get(group, ([], 0))[0]
        segment = next((s for s in segments if s["start"] <= position < s["start"] + s["duration"]), None)
        if segment is None:
            self.measurement.setText(self.tr_text("A agulha está em um gap, não em áudio.", "The needle is in a gap, not in audio."))
            return
        try:
            start, duration = sample_window(segment["start"], segment["start"] + segment["duration"], position, self.window.value())
        except ValueError as error:
            self.measurement.setText(str(error))
            return
        source_start = segment["source_start"] + start - segment["start"]
        self.measurement.setText(self.tr_text("Medindo a amostra…", "Measuring sample…"))
        self.start_job(lambda control: measure_noise(segment["path"], source_start, duration, control),
                       lambda sample: self.sample_ready(sample, group, position))

    def sample_ready(self, sample, group, position):
        # Loud regions may be intentional noise references. Warn and request
        # explicit consent instead of either blocking them or learning silently.
        if sample.threshold > -25:
            self._pending_sample = (sample, group, position)
            self.measurement.setText(self.tr_text(
                f"Amostra alta em {tc(position)}: pico {sample.peak_db:.1f} dB → limiar sugerido {sample.threshold} dB. Pode marcar fala ou quase todo o áudio para remoção. Confirme para usar o limiar e depois clique em Recalcular prévia.",
                f"High-level sample at {tc(position)}: peak {sample.peak_db:.1f} dB → suggested threshold {sample.threshold} dB. May mark speech or almost all audio for removal. Confirm to use the threshold, then click Recalculate preview."))
            self.confirm_sample_btn.show()
            return
        self.accept_sample(sample, group, position)

    def clear_pending_sample(self):
        self._pending_sample = None
        self.confirm_sample_btn.hide()

    def confirm_sample(self):
        if self._busy or self._pending_sample is None:
            return
        sample, group, position = self._pending_sample
        self.accept_sample(sample, group, position, confirmed=True)

    def accept_sample(self, sample, group, position, confirmed=False):
        self.clear_pending_sample()
        self.samples.append((group, position, sample))
        self.thresholds[group] = sample.threshold
        self.threshold.setValue(sample.threshold)
        self.measurement.setText(self.tr_text(
            f"Amostra {len(self.samples)} em {tc(position)}: pico {sample.peak_db:.1f} dB, média {sample.mean_db:.1f} dB → limiar {sample.threshold} dB. Pausa e proteção mantidas.",
            f"Sample {len(self.samples)} at {tc(position)}: peak {sample.peak_db:.1f} dB, mean {sample.mean_db:.1f} dB → threshold {sample.threshold} dB. Pause and protection unchanged."))
        if confirmed:
            self.measurement.setText(self.measurement.text() + self.tr_text(
                " Amostra alta confirmada: revise os cortes antes de aplicar.",
                " High-level sample confirmed: review the cuts before applying."))
        self._dirty = True
        self.apply_btn.setEnabled(False)
        self.summary.setText(self.tr_text(
            "Amostra medida. Clique em Recalcular prévia para analisar com esse limiar.",
            "Sample measured. Click Recalculate preview to analyze with this threshold."))

    def apply_review(self):
        if self._busy or not self.apply_btn.isEnabled():
            return
        cuts = copy.deepcopy(self.cuts)
        self.start_job(lambda control: self.apply_cuts(self.timeline, self.assignments, cuts, control),
                       self.applied)

    def applied(self, result):
        self.output, self.counts = result
        self.accept()

    def reject(self):
        self.request_close()

    def request_close(self):
        """Close immediately when idle, or cancel FFmpeg and close after its worker exits."""
        self._closing = True
        if self._busy:
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText(self.tr_text("Cancelando…", "Cancelling…"))
            self.job.control.cancel()
        else:
            super().reject()

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
            self.request_close()
        else:
            super().closeEvent(event)
