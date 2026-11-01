import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import opentimelineio as otio
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from otio_cutter import MainWindow, process_timeline
from qt_helpers import QtTestCase


def clip(start, duration, path, rate=60):
    return otio.schema.Clip(
        media_reference=otio.schema.ExternalReference(target_url=path),
        source_range=otio.opentime.TimeRange(
            otio.opentime.RationalTime.from_seconds(start, rate),
            otio.opentime.RationalTime.from_seconds(duration, rate),
        ),
    )


class PipelineTests(unittest.TestCase):
    def test_source_mapping_protection_ripple_and_roundtrip(self):
        # The same file is split into takes. Silences outside those takes must
        # never be translated onto them. Every member receives identical cuts.
        with tempfile.NamedTemporaryFile() as media:
            timeline = otio.schema.Timeline()
            for name, kind in [("video", "Video"), ("game", "Audio"), ("mic", "Audio")]:
                track = otio.schema.Track(name=name, kind=kind)
                track.extend([clip(10, 10, media.name), clip(40, 10, media.name)])
                timeline.tracks.append(track)
            original = copy.deepcopy(timeline)
            assignments = [{"index": i, "group": "A1", "analyze": i == 2} for i in range(3)]
            with patch("otio_cutter.detect_silence", return_value=[(0, 5), (12, 15), (42, 45), (90, 99)]) as detect:
                output, counts = process_timeline(timeline, assignments, -45, 1, .2)
            self.assertEqual(detect.call_count, 1)
            self.assertEqual(counts, {"A1": 2})
            restored = otio.adapters.read_from_string(otio.adapters.write_to_string(output))
            self.assertAlmostEqual(restored.duration().to_seconds(), 14.8)
            for track in restored.tracks:
                self.assertEqual(len(track), 4)
                self.assertFalse(any(isinstance(c, otio.schema.Gap) for c in track))
                self.assertEqual([round(c.source_range.start_time.to_seconds(), 3) for c in track], [10, 14.8, 40, 44.8])
                for child in track:
                    self.assertAlmostEqual(child.source_range.duration.value, round(child.source_range.duration.value))
            self.assertEqual(otio.adapters.write_to_string(timeline), otio.adapters.write_to_string(original))

    def test_reject_multiple_analysis_tracks(self):
        with self.assertRaisesRegex(ValueError, "ONE reference"):
            process_timeline(otio.schema.Timeline(), [
                {"index": 0, "group": "A1", "analyze": True},
                {"index": 1, "group": "A1", "analyze": True},
            ], -45, 1, .18)


class ExportTests(QtTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_export_name_and_remembered_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            window = MainWindow()
            window.preferences = QSettings(str(folder / "preferences.ini"), QSettings.IniFormat)
            window.path = str(folder / "ep3.otio")
            window.timeline = otio.schema.Timeline()
            track = otio.schema.Track(name="mic", kind="Audio")
            track.append(clip(0, 10, str(folder / "voice.wav")))
            window.timeline.tracks.append(track)
            window.populate()
            export_dir = folder / "exports"
            export_dir.mkdir()
            target = str(export_dir / "ep3_exported.otio")
            with patch("otio_cutter.QFileDialog.getSaveFileName", return_value=(target, "")) as picker:
                window.save()
            self.assertEqual(picker.call_args.args[2], str(folder / "ep3_exported.otio"))
            self.assertTrue(Path(target).exists())
            settings = QSettings(str(folder / "preferences.ini"), QSettings.IniFormat)
            self.assertEqual(settings.value("export/last_directory"), str(export_dir))
            with patch("otio_cutter.QFileDialog.getSaveFileName", return_value=("", "")) as picker:
                window.save()
            self.assertEqual(picker.call_args.args[2], target)
            window.close()


if __name__ == "__main__":
    unittest.main()
