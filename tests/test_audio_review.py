import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import time
import unittest
import wave
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import opentimelineio as otio
from PySide6.QtCore import Qt, QTimer, QPoint, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from audio_review import AudioReviewDialog, SamplingWaveform, projected_duration
from audio_tools import NoiseSample, measure_noise, sample_window, threshold_from_peak, waveform_peaks
from otio_cutter import MainWindow, apply_group_cuts, collect_group_cuts, media_path
from qt_helpers import QtTestCase


class SampleMathTests(QtTestCase):
    def test_threshold_and_sample_boundaries(self):
        self.assertEqual(threshold_from_peak(-49.2), -46)
        self.assertEqual(threshold_from_peak(-91), -80)
        self.assertEqual(threshold_from_peak(-8.6), -5)
        self.assertEqual(threshold_from_peak(-1), 0)
        self.assertEqual(sample_window(10, 20, 10.01, .6), (10, .6))
        start, duration = sample_window(10, 20, 19.99, .6)
        self.assertAlmostEqual(start, 19.4)
        self.assertEqual(duration, .6)
        start, duration = sample_window(10, 20, 10.01, .01)
        self.assertAlmostEqual(start, 10.005)
        self.assertEqual(duration, .01)

    def test_needle_click_keeps_time_mapping(self):
        app = QApplication.instance() or QApplication([])
        widget = SamplingWaveform()
        widget.resize(1000, 220)
        widget.set_audio([], 600)
        captured = []
        widget.selected.connect(captured.append)
        widget.show()
        QTest.mouseClick(widget, Qt.LeftButton, pos=widget.rect().center())
        self.assertAlmostEqual(captured[-1], 300, delta=1)
        widget.close()

    def test_linear_waveform_keeps_quiet_audio_near_the_center(self):
        app = QApplication.instance() or QApplication([])
        widget = SamplingWaveform()
        widget.resize(400, 200)
        peaks = [.001] * 100 + [.1] * 100
        original = list(peaks)
        widget.set_audio([{"start": 0, "duration": 10, "peaks": peaks, "path": "voice.wav"}], 10)
        widget.cursor = 20  # Keep the sampling highlight outside the drawing.
        self.assertAlmostEqual(widget.amplitude(.001) / widget.amplitude(.1), .01)
        self.assertEqual(widget.amplitude(0), 0)
        widget.show()
        app.processEvents()
        image = widget.grab().toImage()
        ratio = image.devicePixelRatio()
        quiet = image.pixelColor(round(80 * ratio), round(90 * ratio))
        voice = image.pixelColor(round(300 * ratio), round(90 * ratio))
        self.assertEqual(quiet.name(), "#164b3d")
        self.assertEqual(voice.name(), "#3aa383")
        # A preceding envelope must not leak its fill brush into the next take.
        widget.set_audio([
            {"start": 0, "duration": 5, "peaks": [.1] * 100, "path": "voice.wav"},
            {"start": 5, "duration": 5, "peaks": [.001] * 100, "path": "quiet.wav"},
        ], 10)
        app.processEvents()
        image = widget.grab().toImage()
        self.assertEqual(image.pixelColor(round(300 * ratio), round(90 * ratio)).name(), "#164b3d")
        widget.set_display_gain(4)
        self.assertEqual(peaks, original)
        widget.close()


@unittest.skipUnless(shutil.which("ffmpeg") or os.environ.get("DECUT_FFMPEG"), "FFmpeg required")
class CalibrationTests(QtTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "reference.wav"
        # Two known quiet windows with different noise floors, between loud
        # synthetic tones. This tests amplitude detection, not speech recognition.
        rate = 16000
        amplitudes = [.1, .002, .2, .005, .1]
        with wave.open(str(self.path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(b"".join(struct.pack("<h", round(amplitudes[i // rate] * 32767 * math.sin(2 * math.pi * 440 * i / rate)))
                                     for i in range(rate * 5)))

    def tearDown(self):
        super().tearDown()
        self.directory.cleanup()

    def wait_for_job(self, dialog):
        deadline = time.monotonic() + 20
        while dialog._busy and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(10)
        self.assertFalse(dialog._busy, "Review worker did not finish")

    def test_native_measurement_and_streaming_peaks(self):
        sample = measure_noise(str(self.path), 1.2, .6)
        self.assertAlmostEqual(sample.peak_db, -54, delta=.3)
        self.assertEqual(sample.threshold, -50)
        peaks = waveform_peaks(str(self.path), 0, 5, 100)
        self.assertEqual(len(peaks), 100)
        self.assertLess(max(peaks[22:38]), .003)
        self.assertGreater(max(peaks[42:58]), .15)

    def test_repeated_calibration_requires_explicit_apply(self):
        timeline = otio.schema.Timeline()
        track = otio.schema.Track(name="mic", kind="Audio")
        track.append(otio.schema.Clip(
            media_reference=otio.schema.ExternalReference(target_url=str(self.path)),
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, 60), otio.opentime.RationalTime(300, 60))))
        timeline.tracks.append(track)
        assignments = [{"index": 0, "group": "A1", "analyze": True}]
        dialog = AudioReviewDialog(timeline, assignments, -50, .4, .1,
                                   collect_group_cuts, apply_group_cuts, media_path, "en")
        self.wait_for_job(dialog)
        self.assertEqual(len(dialog.cuts["A1"]), 1)
        before_cuts = list(dialog.cuts["A1"])
        before_threshold = dialog.threshold.value()
        dialog.visual_gain.setValue(4)
        self.assertEqual(dialog.cuts["A1"], before_cuts)
        self.assertEqual(dialog.threshold.value(), before_threshold)
        self.assertFalse(dialog._dirty)
        dialog.position.setValue(1.5)
        dialog.sample_here()
        self.wait_for_job(dialog)
        self.assertEqual(len(dialog.samples), 1)
        self.assertEqual(dialog.threshold.value(), -50)
        self.assertEqual(dialog.samples_table.rowCount(), 1)
        self.assertIn("Visible samples: 1", dialog.profile_count.text())
        self.assertEqual(dialog.wave.sample_markers[0]["number"], 1)
        self.assertEqual(dialog.review_tabs.currentIndex(), 1)
        self.assertTrue(dialog._dirty)
        dialog.remove_sample_btn.click()
        self.assertTrue(dialog.cuts["A1"])
        self.assertTrue(dialog.apply_btn.isEnabled())
        self.assertIn("Apply reviewed cuts", dialog.apply_btn.text())
        dialog.recalculate.click()
        self.wait_for_job(dialog)
        self.assertEqual(dialog.review_tabs.currentIndex(), 0)
        dialog.position.setValue(3.5)
        dialog.sample_here()
        self.wait_for_job(dialog)
        self.assertEqual(len(dialog.samples), 2)
        self.assertEqual(dialog.threshold.value(), -43)
        self.assertAlmostEqual(dialog.position.value(), 3.5)
        self.assertEqual(len(dialog.cuts["A1"]), 1, "Measuring must not recalculate cuts")
        dialog.recalculate.click()
        self.wait_for_job(dialog)
        self.assertEqual(len(dialog.cuts["A1"]), 2)
        self.assertAlmostEqual(timeline.duration().to_seconds(), 5)
        self.assertIsNone(dialog.output)
        # Loud samples must not silently raise the noise threshold into speech.
        dialog.position.setValue(2.5)
        dialog.sample_here()
        self.wait_for_job(dialog)
        self.assertEqual(len(dialog.samples), 2)
        self.assertEqual(dialog.threshold.value(), -43)
        self.assertIn("High-level sample", dialog.measurement.text())
        self.assertFalse(dialog.confirm_sample_btn.isHidden())
        # Disabling a proposed removal changes the plan without touching source.
        dialog.intervals.item(0, 0).setCheckState(Qt.Unchecked)
        self.assertEqual(len(dialog.cuts["A1"]), 1)
        dialog.recalculate.click()
        self.wait_for_job(dialog)
        self.assertEqual(len(dialog.cuts["A1"]), 1, "A protected cut must stay unchecked after recalculation")
        self.assertEqual(dialog.intervals.item(0, 0).checkState(), Qt.Unchecked)
        dialog.ripple_box.setChecked(True)
        dialog.apply_review()
        self.wait_for_job(dialog)
        self.assertIsNotNone(dialog.output)
        self.assertAlmostEqual(dialog.output.duration().to_seconds(), 4.2, places=2)
        self.assertAlmostEqual(timeline.duration().to_seconds(), 5)
        dialog.close()

    def test_saved_samples_are_visible_per_mode_and_can_be_located(self):
        timeline = otio.schema.Timeline()
        track = otio.schema.Track(name="mic", kind="Audio")
        track.append(otio.schema.Clip(
            media_reference=otio.schema.ExternalReference(target_url=str(self.path)),
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, 60), otio.opentime.RationalTime(300, 60))))
        timeline.tracks.append(track)
        dialog = AudioReviewDialog(timeline, [{"index": 0, "group": "A1", "analyze": True}],
                                   -50, .4, .1, collect_group_cuts, apply_group_cuts, media_path, "en")
        self.wait_for_job(dialog)
        dialog.needle_selected(1.5)
        dialog.sample_btn.click()
        self.wait_for_job(dialog)
        self.assertEqual(dialog.samples_table.rowCount(), 1)
        self.assertEqual(dialog.samples_table.item(0, 0).text(), "#1")
        self.assertEqual(dialog.samples_table.item(0, 5).text(), "Threshold suggestion")
        before = list(dialog.cuts["A1"])
        with patch("audio_review.measure_noise") as measure:
            dialog.locate_sample(0)
            self.assertAlmostEqual(dialog.position.value(), 1.5)
            self.assertEqual(measure.call_count, 0)
            self.assertEqual(dialog.cuts["A1"], before)
        dialog.wave.clear_selection()
        self.assertEqual(len(dialog.wave.sample_markers), 1, "Clearing the needle must keep stored markers")
        dialog.mode.setCurrentIndex(1)
        self.assertEqual(dialog.samples_table.rowCount(), 1)
        self.assertIn("0 incremental profile(s)", dialog.samples_hint.text())
        dialog.needle_selected(3.5)
        dialog.sample_btn.click()
        self.wait_for_job(dialog)
        self.assertEqual(dialog.samples_table.rowCount(), 2)
        self.assertEqual(dialog.samples_table.item(1, 5).text(), "Similarity profile")
        self.assertIn("Visible samples: 2", dialog.profile_count.text())
        dialog.reset_samples_btn.click()
        self.assertEqual(dialog.samples_table.rowCount(), 1)
        self.assertEqual(len(dialog.wave.sample_markers), 1, "Stored threshold samples remain visible")
        dialog.mode.setCurrentIndex(0)
        self.assertEqual(dialog.samples_table.rowCount(), 1, "Resetting profiles must not erase threshold history")
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_needle_is_passive_measurement_and_recalculation_are_explicit(self):
        timeline = otio.schema.Timeline()
        track = otio.schema.Track(name="mic", kind="Audio")
        track.append(otio.schema.Clip(
            media_reference=otio.schema.ExternalReference(target_url=str(self.path)),
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, 60), otio.opentime.RationalTime(300, 60))))
        timeline.tracks.append(track)
        assignments = [{"index": 0, "group": "A1", "analyze": True}]
        dialog = AudioReviewDialog(timeline, assignments, -50, .4, .1,
                                   collect_group_cuts, apply_group_cuts, media_path, "en")
        dialog.show()
        self.wait_for_job(dialog)
        self.assertFalse(dialog.wave.has_selection)
        before = list(dialog.cuts["A1"])
        threshold = dialog.threshold.value()
        with patch("audio_review.measure_noise", wraps=measure_noise) as measure, \
                patch.object(dialog, "collect_cuts", wraps=collect_group_cuts) as collect:
            QTest.mouseClick(dialog.wave, Qt.LeftButton,
                             pos=QPoint(round(dialog.wave.width() * .3), dialog.wave.height() // 2))
            QTest.qWait(50)
            self.assertTrue(dialog.wave.has_selection)
            self.assertEqual(measure.call_count, 0)
            self.assertEqual(collect.call_count, 0)
            self.assertEqual(dialog.cuts["A1"], before)
            self.assertEqual(dialog.threshold.value(), threshold)
            self.assertFalse(dialog._busy)
            self.assertFalse(dialog._dirty)
            dialog.preserve_btn.click()
            self.assertEqual(dialog.cuts["A1"], [])
            self.assertEqual(measure.call_count, 0)
            self.assertEqual(collect.call_count, 0)
            # Clear/Escape removes only the needle, not the cut choices.
            dialog.clear_needle_btn.click()
            self.assertFalse(dialog.wave.has_selection)
            self.assertFalse(dialog.sample_btn.isEnabled())
            self.assertEqual(dialog.cuts["A1"], [])
            dialog.needle_selected(3.5)
            dialog.sample_btn.click()
            self.wait_for_job(dialog)
            self.assertEqual(measure.call_count, 1)
            self.assertEqual(collect.call_count, 0)
            self.assertEqual(dialog.cuts["A1"], [])
            self.assertEqual(dialog.threshold.value(), -43)
            self.assertFalse(dialog.apply_btn.isEnabled())
            dialog.recalculate.click()
            self.wait_for_job(dialog)
            self.assertEqual(collect.call_count, 1)
            # First region remains protected; only the newly detected one is active.
            self.assertEqual(len(dialog.cuts["A1"]), 1)
            self.assertGreater(dialog.cuts["A1"][0][0], 3)
            self.assertIsNone(dialog.output)
            QTest.keyClick(dialog.wave, Qt.Key_Escape)
            self.assertFalse(dialog.wave.has_selection)
            self.assertEqual(len(dialog.cuts["A1"]), 1)
        dialog.close()

    def test_high_level_sample_is_usable_only_after_confirmation(self):
        timeline = otio.schema.Timeline()
        track = otio.schema.Track(name="mic", kind="Audio")
        track.append(otio.schema.Clip(
            media_reference=otio.schema.ExternalReference(target_url=str(self.path)),
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, 60), otio.opentime.RationalTime(300, 60))))
        timeline.tracks.append(track)
        assignments = [{"index": 0, "group": "A1", "analyze": True}]
        dialog = AudioReviewDialog(timeline, assignments, -50, .4, .1,
                                   collect_group_cuts, apply_group_cuts, media_path, "en")
        self.wait_for_job(dialog)
        sample = NoiseSample(-8.6, -20, threshold_from_peak(-8.6), 1.2, .6)
        dialog.sample_ready(sample, "A1", 1.5)
        self.assertEqual(dialog.threshold.value(), -50)
        self.assertFalse(dialog.confirm_sample_btn.isHidden())
        self.assertIn("-5 dB", dialog.measurement.text())
        self.assertEqual(len(dialog.samples), 0)
        self.assertIsNone(dialog.output)
        # Moving the needle dismisses a stale confirmation.
        dialog.position.setValue(2)
        self.assertTrue(dialog.confirm_sample_btn.isHidden())
        dialog.confirm_sample()
        self.assertEqual(dialog.threshold.value(), -50)
        # Explicit consent changes only the preview, not the source timeline.
        dialog.sample_ready(sample, "A1", 2)
        dialog.confirm_sample_btn.click()
        self.wait_for_job(dialog)
        self.assertEqual(dialog.threshold.value(), -5)
        self.assertEqual(len(dialog.samples), 1)
        self.assertIsNone(dialog.output)
        self.assertEqual(timeline.duration().to_seconds(), 5)
        self.assertIn("confirmed", dialog.measurement.text())
        dialog.close()


class LifetimeTests(QtTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_close_cancels_child_analysis_and_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            pid_file = folder / "pid"
            ffmpeg = folder / "slow-ffmpeg"
            ffmpeg.write_text(f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n")
            ffmpeg.chmod(0o755)
            media = folder / "reference.wav"
            media.touch()
            timeline = otio.schema.Timeline()
            track = otio.schema.Track(name="mic", kind="Audio")
            track.append(otio.schema.Clip(
                media_reference=otio.schema.ExternalReference(target_url=str(media)),
                source_range=otio.opentime.TimeRange(
                    otio.opentime.RationalTime(0, 60), otio.opentime.RationalTime(3600, 60))))
            timeline.tracks.append(track)
            main = MainWindow()
            main.timeline = timeline
            main.show()
            beats = []
            timer = QTimer()
            timer.setInterval(10)
            timer.timeout.connect(lambda: beats.append(time.monotonic()))
            timer.start()
            with patch.dict(os.environ, {"DECUT_FFMPEG": str(ffmpeg)}):
                dialog = AudioReviewDialog(timeline, [{"index": 0, "group": "A1", "analyze": True}],
                                           -50, 1, .18, collect_group_cuts, apply_group_cuts,
                                           media_path, parent=main)
                main.review_dialog = dialog
                dialog.finished.connect(main.review_closed)
                dialog.show()
                deadline = time.monotonic() + 3
                while (not pid_file.exists() or len(beats) < 5) and time.monotonic() < deadline:
                    self.app.processEvents()
                    QTest.qWait(10)
                self.assertTrue(pid_file.exists())
                self.assertGreaterEqual(len(beats), 5, "GUI timer did not run while FFmpeg was busy")
                self.assertTrue(dialog.cancel_btn.isEnabled())
                self.assertFalse(dialog.progress_bar.isHidden())
                main.close()
                deadline = time.monotonic() + 3
                while (dialog._busy or main.isVisible() or dialog.isVisible()) and time.monotonic() < deadline:
                    self.app.processEvents()
                    QTest.qWait(10)
                self.assertFalse(dialog._busy)
                self.assertFalse(dialog.isVisible())
                self.assertFalse(main.isVisible())
                self.assertIsNone(dialog.output)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), 0)
            timer.stop()

    def test_projected_statistics_match_output_without_cloning(self):
        timeline = otio.schema.Timeline()
        for name in ("video", "mic"):
            track = otio.schema.Track(name=name)
            track.append(otio.schema.Clip(source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, 60), otio.opentime.RationalTime(600, 60))))
            timeline.tracks.append(track)
        assignments = [{"index": i, "group": "A1", "analyze": False} for i in range(2)]
        cuts = {"A1": [(1, 3), (2, 4), (8, 12)]}
        expected = projected_duration(timeline, assignments, cuts)
        output, _ = apply_group_cuts(timeline, assignments, cuts)
        self.assertAlmostEqual(expected, output.duration().to_seconds())

    def test_main_close_also_closes_idle_waveform(self):
        main = MainWindow()
        main.show()
        dialog = AudioReviewDialog(otio.schema.Timeline(), [], -50, 1, .18,
                                   collect_group_cuts, apply_group_cuts, media_path, parent=main)
        main.review_dialog = dialog
        dialog.finished.connect(main.review_closed)
        dialog.show()
        self.app.processEvents()
        self.assertTrue(dialog.isVisible())
        main.close()
        self.app.processEvents()
        self.assertFalse(dialog.isVisible())
        self.assertFalse(main.isVisible())


if __name__ == "__main__":
    unittest.main()
