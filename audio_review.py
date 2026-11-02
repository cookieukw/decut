"""Interactive OTIO review: sampling is nondestructive until Apply is accepted."""
import copy
import math
from pathlib import Path

import opentimelineio as otio
from PySide6.QtCore import Qt, QThread, Signal, QRectF, QTimer, QElapsedTimer, Slot, QSignalBlocker
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QSplitter, QProgressBar, QTabWidget, QAbstractItemView, QSlider, QLayout,
)

from audio_tools import measure_noise, sample_window, waveform_peaks
from media_process import JobControl, AnalysisCancelled
from noise_matching import learn_profile, find_similar_pauses, merge_intervals
from timeline_time import timeline_frame_rate, frame_from_seconds, seconds_from_frame, last_frame_position


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
        self.sample_markers = []
        self.audition_cursor = None
        self.duration, self.cursor = 1.0, 0.0
        self.view_start, self.view_duration = 0.0, 1.0
        self.sample_width = .6
        self.display_gain = 2.0
        self.has_selection = False
        self.frame_rate = 30.0
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
        # Stored references are distinct from the live amber needle and red cuts.
        for index, sample in enumerate(self.sample_markers):
            left = max(0, self.x_at(sample["start"]))
            right = min(self.width(), self.x_at(sample["end"]))
            if right <= left:
                continue
            painter.setPen(QPen(QColor("#81b9df"), 1))
            painter.setBrush(QColor(104, 170, 215, 40))
            painter.drawRect(QRectF(left, top, max(1, right - left), bottom - top))
            tag_y = top + 3 + index % 3 * 19
            tag_x = max(0, min(self.width() - 46, self.x_at(sample["position"]) - 18))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#274b63"))
            painter.drawRoundedRect(QRectF(tag_x, tag_y, 44, 17), 3, 3)
            painter.setPen(QColor("#e0ebf2"))
            painter.drawText(QRectF(tag_x, tag_y, 44, 17), Qt.AlignCenter, f"#{sample['number']}")
        painter.setPen(Qt.NoPen)
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
        if self.audition_cursor is not None:
            painter.setPen(QPen(QColor("#e1ebe5"), 1, Qt.DashLine))
            x = int(self.x_at(self.audition_cursor))
            painter.drawLine(x, top, x, bottom)
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
            direction = -1 if event.key() == Qt.Key_Left else 1
            if event.modifiers() & Qt.ControlModifier:
                frame = frame_from_seconds(self.cursor, self.frame_rate) + direction
                self.cursor = seconds_from_frame(max(0, min(last_frame_position(self.duration, self.frame_rate), frame)), self.frame_rate)
            else:
                self.cursor = max(0, min(self.duration, self.cursor + direction * .1))
            self.update()
            self.selected.emit(self.cursor)
        elif event.key() in (Qt.Key_Escape, Qt.Key_Delete):
            self.clear_selection()
        else:
            super().keyPressEvent(event)


class AudioReviewDialog(QDialog):
    def __init__(self, timeline, assignments, threshold, minimum, padding,
                 collect_cuts, apply_cuts, media_path, language="pt", parent=None,
                 protect_speech=False):
        super().__init__(parent)
        # The parent is disabled while this dialog is open. Keep a read-only
        # reference; copying a large OTIO here would block the GUI on opening.
        self.timeline = timeline
        self.assignments = copy.deepcopy(assignments)
        self.collect_cuts, self.apply_cuts, self.media_path = collect_cuts, apply_cuts, media_path
        self.lang = language
        self.default_threshold = threshold
        self.protect_speech = protect_speech
        self.speech_cache = {}
        self.thresholds = {}
        self.cuts, self.audio, self.cache, self.wave_cache = {}, {}, {}, {}
        self.review_rows = {}
        self.protected_ranges = {}
        self.noise_profiles = {}
        self.sample_records = []
        self.incremental_cuts = {}
        self.match_cache = {}
        self.output, self.counts, self.job = None, {}, None
        self._busy = False
        self._dirty = False
        self._displayed_group = None
        self._pending_sample = None
        self._closing = False
        self._stage = "analysis"
        self.audition = None
        self.resize(1100, 700)
        self.setMinimumSize(980, 640)
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
        modes = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem(self.tr_text("Limiar de volume (atual)", "Volume threshold (existing)"), "threshold")
        self.mode.addItem(self.tr_text("Amostras semelhantes (incremental)", "Similar samples (incremental)"), "samples")
        self.mode.setToolTip(self.tr_text(
            "Limiar substitui propostas. Amostras acumula regiões com nível e textura semelhantes ao ruído medido. Não reconhece palavras.",
            "Threshold replaces proposals. Samples accumulates regions with similar measured noise level and texture. Does not recognize words."))
        self.similarity = QComboBox()
        self.similarity.addItem(self.tr_text("Mais precisa", "More precise"), .7)
        self.similarity.addItem(self.tr_text("Equilibrada", "Balanced"), 1.0)
        self.similarity.addItem(self.tr_text("Mais ampla", "Broader"), 1.3)
        self.similarity.setCurrentIndex(1)
        self.similarity.setEnabled(False)
        self.profile_count = QPushButton()
        self.reset_samples_btn = QPushButton(self.tr_text("Limpar amostras e propostas", "Clear samples and proposals"))
        self.reset_samples_btn.setEnabled(False)
        modes.addWidget(QLabel(self.tr_text("Modo", "Mode")))
        modes.addWidget(self.mode, 1)
        modes.addWidget(self.similarity)
        modes.addWidget(self.profile_count)
        modes.addWidget(self.reset_samples_btn)
        layout.addLayout(modes)
        self.wave = SamplingWaveform()
        self.wave.frame_rate = timeline_frame_rate(timeline)
        self.visual_gain.valueChanged.connect(self.wave.set_display_gain)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.wave)
        self.intervals = QTableWidget(0, 3)
        self.intervals.setHorizontalHeaderLabels([self.tr_text("Remover", "Remove"), self.tr_text("Início", "Start"), self.tr_text("Fim", "End")])
        self.intervals.horizontalHeader().setStretchLastSection(True)
        self.review_tabs = QTabWidget()
        self.review_tabs.addTab(self.intervals, self.tr_text("Cortes sugeridos", "Proposed cuts"))
        samples_panel = QWidget()
        samples_layout = QVBoxLayout(samples_panel)
        samples_layout.setContentsMargins(4, 4, 4, 4)
        self.samples_hint = QLabel()
        self.samples_hint.setWordWrap(True)
        samples_layout.addWidget(self.samples_hint)
        self.samples_table = QTableWidget(0, 6)
        self.samples_table.setHorizontalHeaderLabels([
            "#", self.tr_text("Posição", "Position"), self.tr_text("Duração", "Duration"),
            self.tr_text("Pico", "Peak"), self.tr_text("Média", "Mean"), self.tr_text("Uso", "Use")])
        self.samples_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.samples_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.samples_table.setMinimumHeight(76)
        self.samples_table.setMaximumHeight(150)
        self.samples_table.horizontalHeader().setStretchLastSection(True)
        self.samples_table.cellClicked.connect(self.locate_sample)
        samples_layout.addWidget(self.samples_table)
        self.review_tabs.addTab(samples_panel, self.tr_text("Amostras salvas", "Saved samples"))
        self.profile_count.clicked.connect(lambda: self.review_tabs.setCurrentIndex(1))
        splitter.addWidget(self.review_tabs)
        splitter.setMinimumHeight(250)
        splitter.setMaximumHeight(330)
        splitter.setSizes([220, 100])
        layout.addWidget(splitter, 1)
        legend = QLabel(self.tr_text(
            "Verde: áudio · Vermelho: corte · Amarelo: agulha · Azul #N: amostra salva (não é corte)",
            "Green: audio · Red: cut · Amber: needle · Blue #N: saved sample (not a cut)"))
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
        self.protect_speech_box = QCheckBox(self.tr_text("Proteger fala (VAD)", "Protect speech (VAD)"))
        self.protect_speech_box.setChecked(protect_speech)
        self.protect_speech_box.setToolTip(self.tr_text(
            "WebRTC VAD impede cortar regiões onde pode haver voz. Usa CPU, mas pouca RAM.",
            "WebRTC VAD prevents cuts where voice may be present. It uses CPU but little RAM."))
        self.padding = QSpinBox()
        self.padding.setRange(0, 2000)
        self.padding.setValue(round(padding * 1000))
        self.padding.setSuffix(" ms")
        for label, widget in [(self.tr_text("Limiar", "Threshold"), self.threshold),
                              (self.tr_text("Pausa mínima", "Minimum pause"), self.minimum),
                              (self.tr_text("Proteção", "Protection"), self.padding)]:
            controls.addWidget(QLabel(label))
            controls.addWidget(widget)
        controls.addWidget(self.protect_speech_box)
        form.addRow(controls)
        navigation = QHBoxLayout()
        self.position = QDoubleSpinBox()
        self.position.setDecimals(6)
        self.position.setRange(0, max(.001, timeline.duration().to_seconds()))
        self.position.setSuffix(" s")
        self.position.setKeyboardTracking(False)
        self.position_frames = QSpinBox()
        self.position_frames.setRange(0, min(2147483647, last_frame_position(self.position.maximum(), self.wave.frame_rate)))
        self.position_frames.setKeyboardTracking(False)
        self.position_frames.setToolTip(self.tr_text(
            "Índice a partir de zero no tempo da timeline. Digitar um frame posiciona a agulha no início desse quadro.",
            "Zero-based index in timeline time. Enter a frame to seek to its start."))
        self.navigation_fps = QDoubleSpinBox()
        self.navigation_fps.setRange(1, 240)
        self.navigation_fps.setDecimals(8)
        self.navigation_fps.setValue(self.wave.frame_rate)
        self.navigation_fps.setKeyboardTracking(False)
        self.wave.frame_rate = self.navigation_fps.value()
        self.navigation_fps.setToolTip(self.tr_text(
            "FPS detectado da timeline, não do arquivo de vídeo. Corrija se necessário. Afeta apenas a conversão/navegação, não reencoda nem altera os cortes.",
            "Detected timeline FPS, not source-video FPS. Correct if needed. Affects navigation/conversion only; does not re-encode or change cuts."))
        self.previous_frame_btn = QPushButton("−1 frame")
        self.next_frame_btn = QPushButton("+1 frame")
        navigation.addWidget(QLabel(self.tr_text("Agulha (segundos)", "Needle (seconds)")))
        navigation.addWidget(self.position, 1)
        navigation.addWidget(QLabel("Frame"))
        navigation.addWidget(self.position_frames, 1)
        navigation.addWidget(QLabel(self.tr_text("FPS de navegação", "Navigation FPS")))
        navigation.addWidget(self.navigation_fps)
        navigation.addWidget(self.previous_frame_btn)
        navigation.addWidget(self.next_frame_btn)
        form.addRow(navigation)
        sampling = QHBoxLayout()
        self.window = QDoubleSpinBox()
        self.window.setRange(.1, 2)
        self.window.setSingleStep(.1)
        self.window.setValue(.6)
        self.window.setSuffix(" s")
        self.sample_btn = QPushButton(self.tr_text("Medir aqui", "Sample here"))
        self.remove_sample_btn = QPushButton(self.tr_text("Remover esta amostra", "Remove this sample"))
        self.remove_sample_btn.setToolTip(self.tr_text(
            "Adiciona somente a janela amarela aos cortes revisados. Ainda exige Aplicar cortes revisados.",
            "Adds only the amber window to reviewed cuts. Apply reviewed cuts is still required."))
        self.remove_sample_btn.setEnabled(False)
        self.clear_needle_btn = QPushButton(self.tr_text("Limpar agulha", "Clear needle"))
        self.preserve_btn = QPushButton(self.tr_text("Não cortar aqui", "Keep this region"))
        self.preserve_btn.setToolTip(self.tr_text(
            "Desmarca os cortes propostos que contêm a posição da agulha. Pode reativá-los na lista.",
            "Uncheck proposed cuts containing the needle position. You can re-enable them in the list."))
        sampling.addWidget(QLabel(self.tr_text("Janela", "Window")))
        sampling.addWidget(self.window)
        sampling.addWidget(self.sample_btn)
        sampling.addWidget(self.remove_sample_btn)
        sampling.addWidget(self.clear_needle_btn)
        sampling.addWidget(self.preserve_btn)
        form.addRow(sampling)
        layout.addLayout(form)
        playback = QHBoxLayout()
        self.listen_btn = QPushButton(self.tr_text("Ouvir daqui (3 s)", "Listen from here (3 s)"))
        self.listen_window_btn = QPushButton(self.tr_text("Ouvir janela", "Listen to window"))
        self.stop_audio_btn = QPushButton(self.tr_text("Parar", "Stop"))
        self.stop_audio_btn.setEnabled(False)
        self.preview_volume = QSlider(Qt.Horizontal)
        self.preview_volume.setRange(0, 100)
        self.preview_volume.setValue(75)
        self.preview_volume.setMaximumWidth(110)
        self.preview_volume.setToolTip(self.tr_text("Volume só da prévia; não altera o áudio exportado.",
                                                  "Preview volume only; does not change exported audio."))
        self.audition_status = QLabel()
        playback.addWidget(self.listen_btn)
        playback.addWidget(self.listen_window_btn)
        playback.addWidget(self.stop_audio_btn)
        playback.addWidget(QLabel(self.tr_text("Volume da prévia", "Preview volume")))
        playback.addWidget(self.preview_volume)
        playback.addWidget(self.audition_status, 1)
        layout.addLayout(playback)
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
        self.apply_btn.setMinimumHeight(38)
        self.cancel_btn = QPushButton(self.tr_text("Fechar sem aplicar", "Close without applying"))
        self.cancel_btn.setMinimumHeight(38)
        bottom.addWidget(self.recalculate)
        bottom.addStretch()
        bottom.addWidget(self.cancel_btn)
        bottom.addWidget(self.apply_btn)
        layout.addLayout(bottom)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.samples = []
        self.wave.selected.connect(self.needle_selected)
        self.wave.selection_cleared.connect(self.selection_cleared)
        self.position.valueChanged.connect(self.position_changed)
        self.position_frames.valueChanged.connect(self.frame_position_changed)
        self.navigation_fps.valueChanged.connect(self.navigation_rate_changed)
        self.previous_frame_btn.clicked.connect(lambda: self.step_frame(-1))
        self.next_frame_btn.clicked.connect(lambda: self.step_frame(1))
        self.listen_btn.clicked.connect(lambda: self.listen_audio(False))
        self.listen_window_btn.clicked.connect(lambda: self.listen_audio(True))
        self.stop_audio_btn.clicked.connect(self.stop_audio)
        self.preview_volume.valueChanged.connect(self.preview_volume_changed)
        self.window.valueChanged.connect(self.window_changed)
        self.reference.currentIndexChanged.connect(self.reference_changed)
        self.fit_btn.clicked.connect(self.wave.fit)
        self.sample_btn.clicked.connect(self.sample_here)
        self.remove_sample_btn.clicked.connect(self.remove_current_sample)
        self.clear_needle_btn.clicked.connect(self.wave.clear_selection)
        self.preserve_btn.clicked.connect(self.preserve_selection)
        self.recalculate.clicked.connect(self.refresh)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.similarity.currentIndexChanged.connect(self.invalidate)
        self.reset_samples_btn.clicked.connect(self.reset_samples)
        self.cancel_btn.clicked.connect(self.reject)
        self.apply_btn.clicked.connect(self.apply_review)
        self.threshold.valueChanged.connect(self.invalidate)
        self.minimum.valueChanged.connect(self.invalidate)
        self.padding.valueChanged.connect(self.invalidate)
        self.protect_speech_box.toggled.connect(self.invalidate)
        self.intervals.itemChanged.connect(self.interval_changed)
        self.update_sample_indicators()
        self.refresh()

    def tr_text(self, pt, en):
        return en if self.lang == "en" else pt

    def set_busy(self, busy):
        if busy:
            self.stop_audio()
        self._busy = busy
        for widget in (self.reference, self.recalculate, self.sample_btn, self.remove_sample_btn, self.mode, self.profile_count,
                        self.threshold, self.minimum, self.padding, self.position, self.window,
                        self.intervals, self.wave, self.position_frames, self.navigation_fps,
                        self.previous_frame_btn, self.next_frame_btn):
            widget.setEnabled(not busy)
        samples_mode = self.mode.currentData() == "samples"
        self.threshold.setEnabled(not busy and not samples_mode)
        self.similarity.setEnabled(not busy and samples_mode)
        self.reset_samples_btn.setEnabled(not busy and samples_mode)
        self.cancel_btn.setEnabled(not self._closing)
        for button in (self.sample_btn, self.remove_sample_btn, self.clear_needle_btn, self.preserve_btn,
                       self.listen_btn, self.listen_window_btn):
            button.setEnabled(not busy and self.wave.has_selection)
        self.confirm_sample_btn.setEnabled(not busy)
        self.cancel_btn.setText(self.tr_text("Cancelar e fechar", "Cancel and close") if busy
                                else self.tr_text("Fechar sem aplicar", "Close without applying"))
        self.apply_btn.setEnabled(not busy and not self._dirty and any(self.cuts.values()))
        self.update_apply_label()

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
            "matching": self.tr_text("Buscando ruídos semelhantes", "Finding similar noise"),
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

    def mode_changed(self):
        self.invalidate()
        self.set_busy(False)
        if self.mode.currentData() == "samples":
            self.hint.setText(self.tr_text(
                "Meça um ruído sem fala e clique em Recalcular prévia para acumular trechos parecidos. Nada é removido antes de aplicar.",
                "Sample noise without speech, then click Recalculate preview to accumulate similar regions. Nothing is removed before Apply."))
        else:
            self.hint.setText(self.tr_text(
                "Posicione a agulha, clique em Medir aqui e depois em Recalcular prévia. Mover a agulha não calcula nem cria cortes.",
                "Position the needle, click Sample here, then Recalculate preview. Moving the needle does not analyze or create cuts."))
        self.update_profile_count()

    def update_profile_count(self):
        self.update_sample_indicators()

    def visible_samples(self):
        return [record for record in self.sample_records
                if record["group"] == self.group() and record["mode"] == self.mode.currentData()]

    def update_sample_indicators(self):
        rows = self.visible_samples()
        count = len(rows)
        incremental = self.mode.currentData() == "samples"
        if incremental:
            self.profile_count.setText(self.tr_text(f"Amostras: {count} · Ver lista", f"Samples: {count} · View list"))
            hint = self.tr_text("Perfis usados na busca incremental. Clique numa linha para localizar na onda.",
                                "Profiles used by incremental matching. Click a row to locate it on the waveform.")
        else:
            self.profile_count.setText(self.tr_text(f"Medições: {count} · Ver lista", f"Measurements: {count} · View list"))
            hint = self.tr_text("Histórico de medições do modo limiar; não são perfis de similaridade. A última medição sugere o limiar.",
                                "Threshold measurement history, not similarity profiles. The latest measurement suggests a threshold.")
        if not rows:
            hint += self.tr_text(" Nenhuma amostra neste modo/grupo. Posicione a agulha e clique em Medir aqui.",
                                  " No samples in this mode/group. Position the needle and click Sample here.")
        self.samples_hint.setText(hint)
        self.review_tabs.setTabText(1, self.tr_text(f"Amostras salvas ({count})", f"Saved samples ({count})"))
        self.samples_table.setRowCount(count)
        for row, record in enumerate(rows):
            usage = (self.tr_text("Perfil de similaridade", "Similarity profile") if incremental
                     else self.tr_text("Sugestão de limiar", "Threshold suggestion"))
            values = [f"#{record['number']}", tc(record["position"]),
                      f"{record['end'] - record['start']:.3f} s", f"{record['peak']:.1f} dB",
                      f"{record['mean']:.1f} dB", usage]
            for column, value in enumerate(values):
                self.samples_table.setItem(row, column, QTableWidgetItem(value))
        self.wave.sample_markers = rows
        self.wave.update()

    def locate_sample(self, row, column=0):
        rows = self.visible_samples()
        if not 0 <= row < len(rows) or self._busy:
            return
        record = rows[row]
        self.needle_selected(record["position"])
        self.window.setValue(record["end"] - record["start"])
        self.wave.view_duration = min(self.wave.duration, max(5, (record["end"] - record["start"]) * 12))
        self.wave.view_start = max(0, min(self.wave.duration - self.wave.view_duration,
                                        record["position"] - self.wave.view_duration / 2))
        self.wave.update()

    def reset_samples(self):
        if self._busy:
            return
        group = self.group()
        self.noise_profiles[group] = []
        self.sample_records = [record for record in self.sample_records
                               if not (record["group"] == group and record["mode"] == "samples")]
        self.incremental_cuts[group] = []
        self.cuts[group] = []
        self.review_rows[group] = []
        self.match_cache.clear()
        self.clear_pending_sample()
        self.load_intervals()
        self.update_profile_count()
        self.invalidate()

    def refresh(self):
        if self._busy or not self.reference.count():
            return
        mode = self.mode.currentData()
        if mode == "samples" and not any(self.noise_profiles.values()):
            self.summary.setText(self.tr_text(
                "Adicione uma amostra com Medir aqui antes de buscar trechos semelhantes.",
                "Add a sample with Sample here before searching for similar regions."))
            self.apply_btn.setEnabled(False)
            return
        self.thresholds[self.group()] = self.threshold.value()
        thresholds = dict(self.thresholds)
        profiles = copy.deepcopy(self.noise_profiles)
        accumulated = copy.deepcopy(self.incremental_cuts)
        tolerance = self.similarity.currentData()
        minimum, padding = self.minimum.value(), self.padding.value() / 1000
        self.summary.setText(self.tr_text("Analisando e gerando waveform…", "Analyzing and generating waveform…"))

        def prepare(control):
            source_windows = {}
            if mode == "samples":
                for entry in self.assignments:
                    if not entry["analyze"] or not entry["group"]:
                        continue
                    for child in self.timeline.tracks[entry["index"]]:
                        if isinstance(child, otio.schema.Clip):
                            start = child.trimmed_range().start_time.to_seconds()
                            key = (entry["group"], self.media_path(child))
                            source_windows.setdefault(key, []).append((start, start + child.duration().to_seconds()))
                source_windows = {key: merge_intervals(ranges) for key, ranges in source_windows.items()}

            def provider(group, path, minimum, control):
                refs = tuple(profiles.get(group, []))
                if not refs:
                    return []
                ranges = tuple(source_windows.get((group, path), []))
                key = (group, path, refs, minimum, tolerance, ranges)
                if key not in self.match_cache:
                    self.match_cache[key] = find_similar_pauses(path, refs, minimum, tolerance, control, ranges)
                return self.match_cache[key]

            kwargs = {"interval_provider": provider} if mode == "samples" else {}
            cuts = self.collect_cuts(self.timeline, self.assignments, self.default_threshold,
                                     minimum, padding, thresholds, self.cache, control=control,
                                     protect_speech=self.protect_speech_box.isChecked(),
                                     speech_cache=self.speech_cache, **kwargs)
            if mode == "samples":
                cuts = {group: merge_intervals(accumulated.get(group, []) + ranges)
                        for group, ranges in cuts.items()}
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
        if self.mode.currentData() == "samples":
            self.incremental_cuts = copy.deepcopy(self.cuts)
        self.review_rows = {}
        for group, cuts in self.cuts.items():
            protected = self.protected_ranges.get(group, [])
            rows = [(start, end, not any(start < b and end > a for a, b in protected))
                    for start, end in cuts]
            self.review_rows[group] = rows
            self.cuts[group] = [(start, end) for start, end, enabled in rows if enabled]
        self.reference_changed()
        self.review_tabs.setCurrentIndex(0)

    def reference_changed(self):
        self.stop_audio()
        self.clear_pending_sample()
        self.threshold.blockSignals(True)
        self.threshold.setValue(self.thresholds.get(self.group(), self.default_threshold))
        self.threshold.blockSignals(False)
        segments, duration = self.audio.get(self.group(), ([], 1))
        with QSignalBlocker(self.position), QSignalBlocker(self.position_frames):
            self.position.setMaximum(duration)
            self.position_frames.setMaximum(min(2147483647, last_frame_position(duration, self.navigation_fps.value())))
        if self._displayed_group != self.group():
            self.wave.cursor = 0
            self.sync_position_controls(0)
            self.wave.set_audio(segments, duration)
            self._displayed_group = self.group()
        else:
            # Keep the learned point and zoom in view when recalculating.
            self.wave.segments = segments
            self.wave.duration = duration
            self.wave.update()
        for button in (self.sample_btn, self.remove_sample_btn, self.clear_needle_btn, self.preserve_btn,
                       self.listen_btn, self.listen_window_btn):
            button.setEnabled(not self._busy and self.wave.has_selection)
        self.load_intervals()
        self.update_profile_count()

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
        self.update_apply_label()

    def update_apply_label(self):
        if self._busy:
            self.apply_btn.setText(self.tr_text("Aplicando…", "Applying…"))
        elif any(self.cuts.values()) and not self._dirty:
            self.apply_btn.setText(self.tr_text("Aplicar cortes revisados", "Apply reviewed cuts"))
        else:
            self.apply_btn.setText(self.tr_text("Nenhum corte pronto", "No cuts ready"))

    def position_changed(self, position):
        self.set_needle_position(position)

    def sync_position_controls(self, seconds):
        with QSignalBlocker(self.position), QSignalBlocker(self.position_frames):
            self.position.setValue(seconds)
            self.position_frames.setValue(min(self.position_frames.maximum(), frame_from_seconds(seconds, self.navigation_fps.value())))

    def set_needle_position(self, position):
        self.stop_audio()
        self.clear_pending_sample()
        position = max(0, min(self.wave.duration, position))
        self.sync_position_controls(position)
        self.wave.cursor = position
        self.wave.has_selection = True
        if not self.wave.view_start <= position <= self.wave.view_start + self.wave.view_duration:
            self.wave.view_start = max(0, min(self.wave.duration - self.wave.view_duration,
                                            position - self.wave.view_duration / 2))
        self.wave.update()
        for button in (self.sample_btn, self.remove_sample_btn, self.clear_needle_btn, self.preserve_btn,
                       self.listen_btn, self.listen_window_btn):
            button.setEnabled(not self._busy)

    def frame_position_changed(self, frame):
        self.set_needle_position(seconds_from_frame(frame, self.navigation_fps.value()))

    def step_frame(self, direction):
        frame = max(0, min(self.position_frames.maximum(), self.position_frames.value() + direction))
        self.frame_position_changed(frame)

    def navigation_rate_changed(self, fps):
        # FPS is a display/navigation conversion. Preserve the selected time and plan.
        self.wave.frame_rate = fps
        with QSignalBlocker(self.position_frames):
            self.position_frames.setMaximum(min(2147483647, last_frame_position(self.wave.duration, fps)))
        self.sync_position_controls(self.wave.cursor)

    def window_changed(self, value):
        self.stop_audio()
        self.clear_pending_sample()
        self.wave.sample_width = value
        self.wave.update()

    def needle_selected(self, position):
        self.set_needle_position(position)

    def selection_cleared(self):
        self.stop_audio()
        self.clear_pending_sample()
        for button in (self.sample_btn, self.remove_sample_btn, self.clear_needle_btn, self.preserve_btn,
                       self.listen_btn, self.listen_window_btn):
            button.setEnabled(False)
        self.measurement.setText(self.tr_text(
            "Agulha limpa. Os cortes não foram alterados.", "Needle cleared. Cuts were not changed."))

    def listen_audio(self, window_only=False):
        if self._busy or not self.wave.has_selection:
            return
        try:
            from audio_audition import AudioAudition, audition_range
            target = audition_range(self.audio.get(self.group(), ([], 0))[0], self.wave.cursor,
                                    window=self.window.value() if window_only else None)
            if self.audition is None:
                self.audition = AudioAudition(self)
                self.audition.active_changed.connect(self.audition_state_changed)
                self.audition.position_changed.connect(self.audition_position_changed)
                self.audition.failed.connect(self.audition_failed)
            self.audition.set_volume(self.preview_volume.value())
            self.audition.play(target)
        except Exception as error:
            self.audition_failed(str(error))

    def preview_volume_changed(self, value):
        if self.audition is not None:
            self.audition.set_volume(value)

    def audition_state_changed(self, active):
        self.stop_audio_btn.setEnabled(active)
        if active:
            self.audition_status.setText(self.tr_text("Carregando áudio…", "Loading audio…"))
        else:
            self.wave.audition_cursor = None
            self.wave.update()
            self.audition_status.clear()

    def audition_position_changed(self, position):
        self.wave.audition_cursor = position
        self.wave.update()
        frame = frame_from_seconds(position, self.navigation_fps.value())
        self.audition_status.setText(f"{tc(position)} · frame {frame}")

    def audition_failed(self, message):
        self.audition_state_changed(False)
        self.audition_status.setText(self.tr_text(f"Falha na prévia: {message}", f"Preview failed: {message}"))

    def stop_audio(self):
        if self.audition is not None:
            self.audition.stop()
        self.audition_state_changed(False)

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

    def remove_current_sample(self):
        if self._busy or not self.wave.has_selection:
            return
        group = self.group()
        segment = next((s for s in self.audio.get(group, ([], 0))[0]
                        if s["start"] <= self.wave.cursor < s["start"] + s["duration"]), None)
        if segment is None:
            self.measurement.setText(self.tr_text("A agulha está em um gap, não em áudio.", "The needle is in a gap, not in audio."))
            return
        start, duration = sample_window(segment["start"], segment["start"] + segment["duration"],
                                        self.wave.cursor, self.window.value())
        interval = (start, start + duration)
        self.cuts[group] = merge_intervals(self.cuts.get(group, []) + [interval])
        self.review_rows[group] = [(a, b, True) for a, b in self.cuts[group]]
        if self.mode.currentData() == "samples":
            self.incremental_cuts[group] = merge_intervals(self.incremental_cuts.get(group, []) + [interval])
        self.protected_ranges[group] = [
            (a, b) for a, b in self.protected_ranges.get(group, [])
            if not (a < interval[1] and b > interval[0])]
        self._dirty = False
        self.load_intervals()
        self.measurement.setText(self.tr_text(
            f"Amostra adicionada aos cortes: {tc(start)} → {tc(start + duration)}. Clique em Aplicar cortes revisados.",
            f"Sample added to cuts: {tc(start)} → {tc(start + duration)}. Click Apply reviewed cuts."))

    def sample_here(self):
        if self._busy:
            return
        if not self.wave.has_selection:
            self.measurement.setText(self.tr_text("Posicione a agulha primeiro.", "Position the needle first."))
            return
        self.clear_pending_sample()
        position, group = self.wave.cursor, self.group()
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
        mode = self.mode.currentData()

        def measure(control):
            sample = measure_noise(segment["path"], source_start, duration, control)
            profile = learn_profile(segment["path"], source_start, duration, control) if mode == "samples" else None
            return sample, profile

        self.start_job(measure, lambda result: self.sample_ready(result[0], group, position, result[1]))

    def sample_ready(self, sample, group, position, profile=None):
        # Loud regions may be intentional noise references. Warn and request
        # explicit consent instead of either blocking them or learning silently.
        if sample.threshold > -25:
            self._pending_sample = (sample, group, position, profile)
            if self.mode.currentData() == "samples":
                self.measurement.setText(self.tr_text(
                    f"Amostra alta em {tc(position)}: pico {sample.peak_db:.1f} dB. Esse perfil pode corresponder a fala. Confirme apenas se quer buscá-lo e revisar os resultados.",
                    f"High-level sample at {tc(position)}: peak {sample.peak_db:.1f} dB. This profile may match speech. Confirm only if you want to search for it and review the results."))
            else:
                self.measurement.setText(self.tr_text(
                    f"Amostra alta em {tc(position)}: pico {sample.peak_db:.1f} dB → limiar sugerido {sample.threshold} dB. Pode marcar fala ou quase todo o áudio para remoção. Confirme para usar o limiar e depois clique em Recalcular prévia.",
                    f"High-level sample at {tc(position)}: peak {sample.peak_db:.1f} dB → suggested threshold {sample.threshold} dB. May mark speech or almost all audio for removal. Confirm to use the threshold, then click Recalculate preview."))
            self.confirm_sample_btn.show()
            return
        self.accept_sample(sample, group, position, profile=profile)

    def clear_pending_sample(self):
        self._pending_sample = None
        self.confirm_sample_btn.hide()

    def confirm_sample(self):
        if self._busy or self._pending_sample is None:
            return
        sample, group, position, profile = self._pending_sample
        self.accept_sample(sample, group, position, confirmed=True, profile=profile)

    def accept_sample(self, sample, group, position, confirmed=False, profile=None):
        self.clear_pending_sample()
        if self.mode.currentData() == "samples" and profile is None:
            self.measurement.setText(self.tr_text("Meça novamente para obter um perfil acústico.", "Sample again to obtain an acoustic profile."))
            return
        self.samples.append((group, position, sample))
        if self.mode.currentData() == "samples":
            self.noise_profiles.setdefault(group, []).append(profile)
        else:
            self.thresholds[group] = sample.threshold
            self.threshold.setValue(sample.threshold)
        segment = next((s for s in self.audio.get(group, ([], 0))[0]
                        if s["start"] <= position < s["start"] + s["duration"]), None)
        start = (segment["start"] + sample.start - segment["source_start"]
                 if segment else max(0, position - sample.duration / 2))
        number = max((record["number"] for record in self.sample_records
                      if record["group"] == group and record["mode"] == self.mode.currentData()), default=0) + 1
        self.sample_records.append({"group": group, "mode": self.mode.currentData(), "number": number,
                                    "position": position, "start": start, "end": start + sample.duration,
                                    "peak": sample.peak_db, "mean": sample.mean_db})
        self.update_sample_indicators()
        self.review_tabs.setCurrentIndex(1)
        self.measurement.setText(self.tr_text(
            f"Amostra {len(self.samples)} em {tc(position)}: pico {sample.peak_db:.1f} dB, média {sample.mean_db:.1f} dB → limiar {sample.threshold} dB. Pausa e proteção mantidas.",
            f"Sample {len(self.samples)} at {tc(position)}: peak {sample.peak_db:.1f} dB, mean {sample.mean_db:.1f} dB → threshold {sample.threshold} dB. Pause and protection unchanged."))
        if confirmed:
            self.measurement.setText(self.measurement.text() + self.tr_text(
                " Amostra alta confirmada: revise os cortes antes de aplicar.",
                " High-level sample confirmed: review the cuts before applying."))
        if self.mode.currentData() == "samples":
            self.measurement.setText(self.tr_text(
                f"Amostra {len(self.noise_profiles[group])} adicionada em {tc(position)}: pico {sample.peak_db:.1f} dB. Recalcule para buscar trechos com nível e textura parecidos.",
                f"Sample {len(self.noise_profiles[group])} added at {tc(position)}: peak {sample.peak_db:.1f} dB. Recalculate to find regions with similar level and texture."))
        self._dirty = True
        self.apply_btn.setEnabled(False)
        self.summary.setText(self.tr_text(
            "Amostra medida. Clique em Recalcular prévia para atualizar as propostas.",
            "Sample measured. Click Recalculate preview to update proposals."))

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
        if self.audition is not None:
            self.audition.shutdown()
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
            if self.audition is not None:
                self.audition.shutdown()
            super().closeEvent(event)
