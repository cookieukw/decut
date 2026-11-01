"""Short, source-range-aware audio audition. No rendering or cut decisions."""
from dataclasses import dataclass
import math

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

from audio_tools import sample_window


@dataclass(frozen=True)
class AuditionRange:
    path: str
    source_start: float
    source_end: float
    timeline_start: float

    def timeline_position(self, source_seconds):
        return self.timeline_start + max(0, min(self.source_end - self.source_start,
                                               source_seconds - self.source_start))


def audition_range(segments, position, window=None, duration=3.0):
    if not math.isfinite(position) or duration <= 0:
        raise ValueError("Invalid audition position or duration.")
    segment = next((s for s in segments if s["start"] <= position < s["start"] + s["duration"]), None)
    if segment is None:
        raise ValueError("The needle is in a gap or at the end. Choose a point inside an audio take.")
    end = segment["start"] + segment["duration"]
    if window is not None:
        start, length = sample_window(segment["start"], end, position, window)
    else:
        start, length = position, min(duration, end - position)
    source_start = segment["source_start"] + start - segment["start"]
    return AuditionRange(segment["path"], source_start, source_start + length, start)


class AudioAudition(QObject):
    active_changed = Signal(bool)
    position_changed = Signal(float)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.output = QAudioOutput(self)
        self.output.setVolume(.75)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.output)
        self.player.mediaStatusChanged.connect(self.media_status_changed)
        self.player.seekableChanged.connect(lambda seekable: self.start_loaded() if seekable else None)
        self.player.playbackStateChanged.connect(self.state_changed)
        self.player.errorOccurred.connect(lambda error, message: self.fail(message or str(error)))
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(20)
        self.poll_timer.timeout.connect(self.poll_position)
        self.load_timer = QTimer(self)
        self.load_timer.setSingleShot(True)
        self.load_timer.timeout.connect(lambda: self.fail("Audio loading/seeking timed out."))
        self.target = None
        self.active = False
        self.pending = False

    def set_volume(self, percent):
        self.output.setVolume(max(0, min(100, percent)) / 100)

    def play(self, target):
        self.stop()
        if QMediaDevices.defaultAudioOutput().isNull():
            self.failed.emit("No audio output device is available.")
            return
        self.target = target
        self.pending = self.active = True
        self.active_changed.emit(True)
        self.load_timer.start(10000)
        source = QUrl.fromLocalFile(target.path)
        if self.player.source() != source:
            self.player.setSource(source)
        else:
            self.start_loaded()

    def start_loaded(self):
        if not self.pending or self.target is None:
            return
        if self.player.mediaStatus() not in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            return
        if self.target.source_start > 0 and not self.player.isSeekable():
            return
        self.pending = False
        self.load_timer.stop()
        self.player.setPosition(round(self.target.source_start * 1000))
        self.player.play()
        if self.active:
            self.poll_timer.start()

    def media_status_changed(self, status):
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self.start_loaded()
        elif status == QMediaPlayer.EndOfMedia:
            self.stop()
        elif status == QMediaPlayer.InvalidMedia and self.active:
            self.fail(self.player.errorString() or "The audio format cannot be played.")

    def state_changed(self, state):
        if state == QMediaPlayer.StoppedState and self.active and not self.pending:
            self.stop()

    def poll_position(self):
        if not self.active or self.pending or self.target is None:
            return
        seconds = self.player.position() / 1000
        if seconds >= self.target.source_end:
            self.stop()
        elif seconds >= self.target.source_start - .02:
            self.position_changed.emit(self.target.timeline_position(seconds))

    def stop(self):
        was_active = self.active
        self.active = self.pending = False
        self.target = None
        self.poll_timer.stop()
        self.load_timer.stop()
        self.player.stop()
        if was_active:
            self.active_changed.emit(False)

    def fail(self, message):
        self.stop()
        self.failed.emit(message)

    def shutdown(self):
        self.stop()
        self.player.setSource(QUrl())
