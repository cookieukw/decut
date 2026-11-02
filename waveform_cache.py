"""Persistent, versioned waveform cache keyed by source-file identity."""
from hashlib import sha256
import os
from pathlib import Path
import pickle
import tempfile
import zlib

CACHE_VERSION = 1
CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "decut" / "waveforms"


def cache_key(path, start, duration, bins):
    stat = os.stat(path)
    payload = repr((CACHE_VERSION, os.path.abspath(path), stat.st_size,
                    stat.st_mtime_ns, round(start, 6), round(duration, 6), int(bins))).encode()
    return sha256(payload).hexdigest()


def load_waveform(path, start, duration, bins):
    try:
        file = CACHE_ROOT / f"{cache_key(path, start, duration, bins)}.waveform"
        values = pickle.loads(zlib.decompress(file.read_bytes()))
        return values if isinstance(values, list) else None
    except (FileNotFoundError, OSError, ValueError, pickle.PickleError, zlib.error):
        return None


def save_waveform(path, start, duration, bins, values):
    try:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        target = CACHE_ROOT / f"{cache_key(path, start, duration, bins)}.waveform"
        payload = zlib.compress(pickle.dumps(list(values), protocol=pickle.HIGHEST_PROTOCOL), 3)
        with tempfile.NamedTemporaryFile(dir=CACHE_ROOT, prefix=".tmp-", delete=False) as temp:
            temp.write(payload)
            temp_path = Path(temp.name)
        temp_path.replace(target)
    except (OSError, ValueError):
        # Cache failure must never prevent a review or edit.
        return
