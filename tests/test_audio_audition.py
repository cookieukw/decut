from unittest.mock import patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from audio_audition import audition_range
from test_navigation import navigation_dialog
from qt_helpers import QtTestCase


class SilentTestPlayer(QObject):
    """Test transport wiring without sending sound to a developer's speakers."""
    active_changed = Signal(bool)
    position_changed = Signal(float)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.target = None
        self.active = False
        self.closed = False
        self.volume = 75

    def set_volume(self, value):
        self.volume = value

    def play(self, target):
        self.target = target
        self.active = True
        self.active_changed.emit(True)

    def stop(self):
        self.active = False
        self.active_changed.emit(False)

    def shutdown(self):
        self.stop()
        self.closed = True


class AuditionTests(QtTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_source_mapping_window_boundaries_and_gaps(self):
        segments = [{"start": 10, "duration": 5, "source_start": 50, "path": "/media/voice.mp3"}]
        target = audition_range(segments, 12)
        self.assertEqual((target.source_start, target.source_end), (52, 55))
        self.assertEqual(target.timeline_position(53), 13)
        short = audition_range(segments, 14.8)
        self.assertAlmostEqual(short.source_end, 55)
        window = audition_range(segments, 10.05, window=.6)
        self.assertEqual(window.source_start, 50)
        self.assertAlmostEqual(window.source_end, 50.6)
        with self.assertRaises(ValueError):
            audition_range(segments, 5)
        with self.assertRaises(ValueError):
            audition_range(segments, 15)

    def test_listening_does_not_measure_or_cut_and_stops_on_navigation(self):
        dialog, collect, apply = navigation_dialog()
        dialog.position.setValue(2.5)
        with patch("audio_audition.AudioAudition", SilentTestPlayer), patch("audio_review.measure_noise") as measure:
            dialog.listen_btn.click()
            self.assertTrue(dialog.audition.active)
            self.assertEqual(dialog.audition.target.source_start, 22.5)
            self.assertTrue(dialog.stop_audio_btn.isEnabled())
            dialog.audition.position_changed.emit(3)
            self.assertEqual(dialog.wave.audition_cursor, 3)
            self.assertEqual(dialog.wave.cursor, 2.5)
            dialog.preview_volume.setValue(40)
            self.assertEqual(dialog.audition.volume, 40)
            dialog.position_frames.setValue(180)
            self.assertFalse(dialog.audition.active)
            self.assertIsNone(dialog.wave.audition_cursor)
            dialog.listen_window_btn.click()
            self.assertTrue(dialog.audition.active)
            self.assertAlmostEqual(dialog.audition.target.source_end - dialog.audition.target.source_start, .6)
            dialog.stop_audio_btn.click()
            self.assertFalse(dialog.audition.active)
            self.assertFalse(measure.called)
            self.assertFalse(collect.called)
            self.assertFalse(apply.called)
            self.assertFalse(dialog._dirty)
            dialog.listen_btn.click()
            dialog.close()
            self.assertTrue(dialog.audition.closed)
