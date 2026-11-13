import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "runtime", ROOT / "vendor", ROOT / "tools"):
    sys.path.insert(0, str(directory))

from build_sample_index import build


class SampleBuilderTests(unittest.TestCase):
    def fixture(self, root):
        data = root / "input"
        data.mkdir()
        raw = gzip.compress(json.dumps([{"test.key": 1}, {"test.key": 2}]).encode())
        (data / "fixture.json.gz").write_bytes(raw)
        (data / "catalog.json").write_text(json.dumps({"database": "test", "columns": []}))
        metadata = []
        bundle = {
            "database": "test", "metadata": metadata,
            "metadata_sha256": hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest(),
            "expires_unix": time.time() + 60,
            "profiles": {
                "fixture": {"database": "test", "rows": 2, "p": .5,
                            "gzip_sha256": hashlib.sha256(raw).hexdigest()},
            },
        }
        path = data / "BUNDLE.json"
        path.write_text(json.dumps(bundle))
        return path, bundle

    def test_builds_frames_without_models_and_keeps_original_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, bundle = self.fixture(root)
            summary = build(path, root / "out")
            index = json.loads((root / "out/models/INDEX.json").read_text())
            self.assertEqual(index["expires_unix"], bundle["expires_unix"])
            self.assertEqual(index["models"], {})
            self.assertEqual(summary["learned_models_trained"], 0)
            self.assertEqual(summary["learned_model_files_written"], 0)
            self.assertFalse(summary["TTL_extended"])
            self.assertEqual(len(list((root / "out/models").glob("*.pkl"))), 1)

    def test_expired_bundle_cannot_be_reissued(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, bundle = self.fixture(root)
            bundle["expires_unix"] = time.time() - 1
            path.write_text(json.dumps(bundle))
            with self.assertRaisesRegex(ValueError, "no TTL renewal"):
                build(path, root / "out")
            self.assertFalse((root / "out").exists())

    def test_metadata_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, bundle = self.fixture(root)
            bundle["metadata_sha256"] = "wrong"
            path.write_text(json.dumps(bundle))
            with self.assertRaisesRegex(ValueError, "watermark"):
                build(path, root / "out")
            self.assertFalse((root / "out").exists())

    def test_changed_raw_frame_rejected_no_successful_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _ = self.fixture(root)
            (path.parent / "fixture.json.gz").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "changed offline"):
                build(path, root / "out")
            self.assertFalse((root / "out/models/INDEX.json").exists())


if __name__ == "__main__":
    unittest.main()
