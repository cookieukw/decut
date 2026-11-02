import unittest
from pathlib import Path
import opentimelineio as otio
from kdenlive_adapter import read_kdenlive


class KdenliveAdapterTests(unittest.TestCase):
    def test_real_project_imports_blanks_as_otio_gaps(self):
        path = Path("/home/cookie/Videos/simtale/ep3/ep3-modified.kdenlive")
        if not path.exists():
            self.skipTest("episode project is not available")
        timeline = read_kdenlive(path)
        self.assertGreaterEqual(len(timeline.tracks), 3)
        gaps = [child for track in timeline.tracks for child in track if isinstance(child, otio.schema.Gap)]
        self.assertGreater(len(gaps), 0)
        self.assertGreater(max(track.duration().to_seconds() for track in timeline.tracks), 1000)
