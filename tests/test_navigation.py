from unittest.mock import Mock, patch

import opentimelineio as otio
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from audio_review import AudioReviewDialog
from timeline_time import timeline_frame_rate, frame_from_seconds, seconds_from_frame
from qt_helpers import QtTestCase


def navigation_dialog(rate=60):
    timeline = otio.schema.Timeline()
    track = otio.schema.Track(name="mic", kind="Audio")
    track.append(otio.schema.Clip(
        media_reference=otio.schema.ExternalReference(
            target_url="/example/reference.wav",
            available_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, 50), otio.opentime.RationalTime(5000, 50))),
        source_range=otio.opentime.TimeRange(
            otio.opentime.RationalTime(0, rate), otio.opentime.RationalTime.from_seconds(100, rate))))
    timeline.tracks.append(track)
    collect, apply = Mock(), Mock()
    with patch.object(AudioReviewDialog, "refresh"):
        dialog = AudioReviewDialog(timeline, [{"index": 0, "group": "A1", "analyze": True}],
                                   -50, 1, .18, collect, apply, lambda child: "/example/reference.wav", "en")
    dialog.preview_ready(({"A1": []}, {"A1": ([{
        "start": 0, "duration": 100, "source_start": 20,
        "path": "/example/reference.wav", "peaks": [],
    }], 100)}))
    return dialog, collect, apply


class FrameNavigationTests(QtTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_timeline_rate_not_media_rate_and_two_way_conversion(self):
        dialog, collect, apply = navigation_dialog()
        self.assertEqual(timeline_frame_rate(dialog.timeline), 60)
        dialog.position_frames.setValue(3086)
        self.assertEqual(dialog.position_frames.value(), 3086)
        self.assertAlmostEqual(dialog.position.value(), 3086 / 60, places=6)
        self.assertEqual(dialog.wave.cursor, 3086 / 60)
        dialog.position.setValue(2.5)
        self.assertEqual(dialog.position_frames.value(), 150)
        self.assertEqual(dialog.wave.cursor, 2.5)
        self.assertFalse(collect.called)
        self.assertFalse(apply.called)
        self.assertFalse(dialog._dirty)

    def test_fractional_rate_and_frame_steps_do_not_accumulate_drift(self):
        dialog, _, _ = navigation_dialog(30000 / 1001)
        dialog.position_frames.setValue(1000)
        for _ in range(20):
            dialog.next_frame_btn.click()
        self.assertEqual(dialog.position_frames.value(), 1020)
        self.assertEqual(dialog.wave.cursor, 1020 / dialog.navigation_fps.value())
        for _ in range(20):
            dialog.previous_frame_btn.click()
        self.assertEqual(dialog.position_frames.value(), 1000)
        self.assertEqual(dialog.wave.cursor, 1000 / dialog.navigation_fps.value())
        seconds = dialog.wave.cursor
        dialog.navigation_fps.setValue(60)
        self.assertEqual(dialog.wave.cursor, seconds)
        self.assertEqual(dialog.position_frames.value(), frame_from_seconds(seconds, 60))

    def test_keyboard_jump_bounds_and_no_automatic_analysis(self):
        dialog, collect, _ = navigation_dialog()
        dialog.wave.view_duration = 2
        dialog.wave.view_start = 0
        dialog.position_frames.setValue(3086)
        self.assertLessEqual(dialog.wave.view_start, dialog.wave.cursor)
        self.assertGreaterEqual(dialog.wave.view_start + dialog.wave.view_duration, dialog.wave.cursor)
        QTest.keyClick(dialog.wave, Qt.Key_Right, Qt.ControlModifier)
        self.assertEqual(dialog.position_frames.value(), 3087)
        QTest.keyClick(dialog.wave, Qt.Key_Left, Qt.ControlModifier)
        self.assertEqual(dialog.position_frames.value(), 3086)
        dialog.position_frames.setValue(999999)
        self.assertEqual(dialog.wave.cursor, 100)
        dialog.position_frames.setValue(-1)
        self.assertEqual(dialog.wave.cursor, 0)
        self.assertFalse(collect.called)
        self.assertAlmostEqual(seconds_from_frame(3086, 60), 51.4333333333)
