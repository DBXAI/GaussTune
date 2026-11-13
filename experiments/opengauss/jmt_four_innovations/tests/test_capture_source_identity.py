import ast
import hashlib
from pathlib import Path
import tempfile
import unittest

from jmt.common.provenance import source_identity_matches
from jmt.common.resources import resources_root
from jmt.innovation_4_performance import native_stats


class CaptureSourceIdentityTests(unittest.TestCase):
    def evidence(self, path):
        return {"path": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def test_relocated_byte_identical_source_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            recorded, supported = root / "old_probe.py", root / "new_probe.py"
            recorded.write_text("print('capture')\n")
            supported.write_bytes(recorded.read_bytes())
            self.assertTrue(source_identity_matches(self.evidence(recorded), (supported,)))

    def test_same_filename_different_code_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            recorded, supported = root / "old", root / "new"
            recorded.mkdir()
            supported.mkdir()
            recorded, supported = recorded / "probe.py", supported / "probe.py"
            recorded.write_text("print('unreviewed')\n")
            supported.write_text("print('supported')\n")
            self.assertFalse(source_identity_matches(self.evidence(recorded), (supported,)))

    def test_changed_recorded_file_is_rejected_even_if_declared_hash_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            recorded, supported = Path(temp) / "old", Path(temp) / "new"
            recorded.write_text("original\n")
            supported.write_bytes(recorded.read_bytes())
            row = self.evidence(recorded)
            recorded.write_text("modified\n")
            self.assertFalse(source_identity_matches(row, (supported,)))

    def test_missing_recorded_source_is_not_proven_by_matching_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            supported = Path(temp) / "new"
            supported.write_text("original\n")
            row = self.evidence(supported)
            row["path"] = str(Path(temp) / "missing")
            self.assertFalse(source_identity_matches(row, (supported,)))

    def test_self_declared_unreviewed_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            recorded, supported = Path(temp) / "old", Path(temp) / "new"
            recorded.write_text("unreviewed\n")
            supported.write_text("supported\n")
            self.assertFalse(source_identity_matches(self.evidence(recorded), (supported,)))

    def test_missing_or_relative_evidence_is_rejected(self):
        self.assertFalse(source_identity_matches(None, ()))
        self.assertFalse(source_identity_matches({"path": "relative", "sha256": "0"*64}, ()))

    def test_reviewed_legacy_capture_bytes_are_preserved(self):
        legacy = resources_root() / "scripts" / "native_stats_capture_v1_legacy.py"
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(),
                         "958a7ae8df4a974583fcadb74ea9cb5dea526863bd2b1a006348f170b0256ee5")
        self.assertTrue(source_identity_matches(self.evidence(legacy),
                                               (Path(native_stats.__file__), legacy)))

    def test_legacy_counter_names_and_delta_semantics_match_current(self):
        legacy = resources_root() / "scripts" / "native_stats_capture_v1_legacy.py"
        old, new = ast.parse(legacy.read_text()), ast.parse(Path(native_stats.__file__).read_text())
        def symbols(tree):
            result = {}
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name == "database_stats_delta":
                    result[node.name] = ast.dump(node, include_attributes=False)
                if isinstance(node, ast.Assign):
                    if any(isinstance(t, ast.Name) and t.id == "COUNTERS" for t in node.targets):
                        result["COUNTERS"] = ast.dump(node, include_attributes=False)
            return result
        self.assertEqual(symbols(old), symbols(new))


if __name__ == "__main__":
    unittest.main()
