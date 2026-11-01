from pathlib import Path
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET

from app_version import __version__


ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_release_metadata_matches_application(self):
        self.assertEqual(__version__, "1.0.0")
        release = ET.parse(ROOT / "assets/decut.appdata.xml").find("./releases/release")
        self.assertEqual(release.attrib["version"], __version__)
        desktop = (ROOT / "assets/com.decut.App.desktop").read_text()
        self.assertIn(f"X-AppImage-Version={__version__}", desktop.splitlines())

    def test_version_cli_does_not_start_gui(self):
        result = subprocess.run([sys.executable, str(ROOT / "otio_cutter.py"), "--version"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"decut {__version__}")


if __name__ == "__main__":
    unittest.main()
