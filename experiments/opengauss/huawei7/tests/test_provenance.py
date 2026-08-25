import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from huawei7.provenance import check_manifest, sha256, validate_json_evidence_tree


class ProvenanceTest(unittest.TestCase):
    def test_transitive_json_evidence_reaches_native_raw_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "native.log"
            raw.write_text("native benchmark output\n")
            child = root / "collection.json"
            child.write_text(json.dumps({
                "source_artifacts": [{
                    "path": str(raw), "sha256": sha256(raw),
                }],
            }))
            top = root / "model.json"
            top.write_text(json.dumps({
                "source_artifacts": [{
                    "path": str(child), "sha256": sha256(child),
                }],
            }))
            self.assertEqual(validate_json_evidence_tree(top), 2)
            raw.write_text("tampered\n")
            with self.assertRaisesRegex(ValueError, "changed"):
                validate_json_evidence_tree(top)

    def test_wrong_gaussdb_hash_is_a_hard_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.cpp"
            source.write_bytes(b"source")
            gaussdb = root / "gaussdb"
            gaussdb.write_bytes(b"wrong binary")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "source_commit": "locked-commit",
                "source_files": {
                    "source.cpp": hashlib.sha256(b"source").hexdigest(),
                },
                "reference_gaussdb_sha256": hashlib.sha256(
                    b"reference binary"
                ).hexdigest(),
                "required_symbols": ["ReadBuffer_common"],
            }), encoding="utf-8")
            with mock.patch("huawei7.provenance.subprocess.check_output") as run:
                run.side_effect = ["locked-commit\n", "ReadBuffer_common\n"]
                with self.assertRaisesRegex(RuntimeError, "gaussdb sha256"):
                    check_manifest(manifest, root, gaussdb)

    def test_current_operator_source_and_probe_symbols_match_manifest(self):
        root = Path("/root/openGauss-server-5.1.0")
        gaussdb = Path("/opt/openGauss/bin/gaussdb")
        if not root.is_dir() or not gaussdb.is_file():
            self.skipTest("live openGauss source/binary are unavailable")
        result = check_manifest(
            Path(__file__).resolve().parents[1] / "config" / "source_manifest.json",
            root, gaussdb,
        )
        self.assertTrue(result["valid"])


if __name__ == "__main__":
    unittest.main()
