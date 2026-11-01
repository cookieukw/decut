import math
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import opentimelineio as otio
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import wave

from audio_review import AudioReviewDialog
from noise_matching import pcm_profile, similar_noise, learn_profile, find_similar_pauses, merge_intervals
from otio_cutter import collect_group_cuts, apply_group_cuts, media_path


class MatchingMathTests(unittest.TestCase):
    def test_similar_level_but_different_texture_is_not_the_same_profile(self):
        rng = np.random.default_rng(7)
        noise = rng.normal(0, 70, 800).astype(np.int16)
        tone = (100 * np.sin(np.arange(800) * 2 * math.pi * 300 / 8000)).astype(np.int16)
        reference = pcm_profile(noise)
        self.assertTrue(similar_noise(pcm_profile(noise), reference))
        self.assertFalse(similar_noise(pcm_profile(tone), reference))

    def test_incremental_merge_is_a_union_not_duplicate_cuts(self):
        self.assertEqual(merge_intervals([(1, 2), (1, 2), (1.5, 3), (5, 6)]), [(1, 3), (5, 6)])


@unittest.skipUnless(shutil.which("ffmpeg") or os.environ.get("DECUT_FFMPEG"), "FFmpeg required")
class IncrementalReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, dialog):
        deadline = time.monotonic() + 20
        while dialog._busy and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(10)
        self.assertFalse(dialog._busy)

    def test_sampling_adds_matching_regions_and_preserves_previous_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.wav"
            rate = 16000
            levels = np.repeat([.1, .002, .2, .005, .1], rate)
            data = (levels * 32767 * np.sin(np.arange(rate * 5) * 2 * math.pi * 440 / rate)).astype("<i2")
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(data.tobytes())
            first = learn_profile(str(path), 1.2, .6)
            matches = find_similar_pauses(str(path), [first], .4)
            self.assertEqual(len(matches), 1)
            self.assertAlmostEqual(matches[0][0], 1, places=2)
            timeline = otio.schema.Timeline()
            for name, kind in [("video", "Video"), ("mic", "Audio")]:
                track = otio.schema.Track(name=name, kind=kind)
                track.append(otio.schema.Clip(
                    media_reference=otio.schema.ExternalReference(target_url=str(path)),
                    source_range=otio.opentime.TimeRange(
                        otio.opentime.RationalTime(0, 60), otio.opentime.RationalTime(300, 60))))
                timeline.tracks.append(track)
            assignments = [{"index": i, "group": "A1", "analyze": i == 1} for i in range(2)]
            dialog = AudioReviewDialog(timeline, assignments, -50, .4, .1,
                                       collect_group_cuts, apply_group_cuts, media_path, "en")
            self.wait(dialog)
            dialog.mode.setCurrentIndex(1)
            self.assertTrue(dialog._dirty)
            dialog.needle_selected(1.5)
            dialog.sample_btn.click()
            self.wait(dialog)
            self.assertEqual(len(dialog.noise_profiles["A1"]), 1)
            dialog.recalculate.click()
            self.wait(dialog)
            self.assertEqual(len(dialog.cuts["A1"]), 1)
            first_cuts = list(dialog.cuts["A1"])
            dialog.needle_selected(3.5)
            dialog.sample_btn.click()
            self.wait(dialog)
            self.assertEqual(dialog.cuts["A1"], first_cuts, "Measurement must not start matching")
            dialog.recalculate.click()
            self.wait(dialog)
            self.assertEqual(len(dialog.cuts["A1"]), 2)
            self.assertEqual(dialog.cuts["A1"][0], first_cuts[0])
            dialog.intervals.item(0, 0).setCheckState(Qt.Unchecked)
            dialog.similarity.setCurrentIndex(0)
            dialog.recalculate.click()
            self.wait(dialog)
            self.assertEqual(len(dialog.cuts["A1"]), 1)
            self.assertEqual(dialog.intervals.item(0, 0).checkState(), Qt.Unchecked)
            dialog.apply_review()
            self.wait(dialog)
            self.assertAlmostEqual(dialog.output.duration().to_seconds(), 4.2, places=2)
            self.assertTrue(all(not isinstance(c, otio.schema.Gap) for t in dialog.output.tracks for c in t))
            self.assertEqual(timeline.duration().to_seconds(), 5)
            dialog.close()
