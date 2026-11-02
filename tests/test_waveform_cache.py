import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from waveform_cache import load_waveform, save_waveform


class WaveformCacheTests(unittest.TestCase):
    def test_roundtrip_and_file_change_invalidates_key(self):
        with tempfile.NamedTemporaryFile() as source:
            source.write(b"source-v1")
            source.flush()
            with tempfile.TemporaryDirectory() as cache:
                with patch("waveform_cache.CACHE_ROOT", Path(cache)):
                    values = [.1, .4, .0]
                    save_waveform(source.name, 1.0, 2.0, 100, values)
                    self.assertEqual(load_waveform(source.name, 1.0, 2.0, 100), values)
                    source.write(b"-changed")
                    source.flush()
                    os.fsync(source.fileno())
                    self.assertIsNone(load_waveform(source.name, 1.0, 2.0, 100))

    def test_cache_failure_is_non_fatal(self):
        with patch("waveform_cache.CACHE_ROOT", Path("/proc/decut-cache")):
            self.assertIsNone(load_waveform("/does/not/exist", 0, 1, 10))
            save_waveform("/does/not/exist", 0, 1, 10, [1])
