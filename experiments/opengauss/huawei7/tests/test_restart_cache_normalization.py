import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "restart_with_shared_buffers.py"
)
SPEC = importlib.util.spec_from_file_location("restart_with_shared_buffers", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RestartCacheNormalizationTest(unittest.TestCase):
    def test_only_exact_database_oid_regular_files_are_advised(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            selected = data / "base" / "42"
            other = data / "base" / "43"
            selected.mkdir(parents=True)
            other.mkdir(parents=True)
            (selected / "100").write_bytes(b"selected")
            (selected / "100.1").write_bytes(b"segment")
            (other / "200").write_bytes(b"other")
            with mock.patch.object(MODULE.os, "posix_fadvise") as advise:
                report = MODULE._evict_database_cache(data, [42])
            self.assertTrue(report["valid"])
            self.assertEqual(report["database_oids"], [42])
            self.assertEqual(report["file_count"], 2)
            self.assertEqual(advise.call_count, 2)

    def test_missing_oid_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            selected = data / "base" / "42"
            selected.mkdir(parents=True)
            (selected / "100").write_bytes(b"selected")
            with self.assertRaisesRegex(ValueError, "missing or unsafe"):
                MODULE._database_files(data, [43])
            (selected / "link").symlink_to(selected / "100")
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE._database_files(data, [42])


if __name__ == "__main__":
    unittest.main()
