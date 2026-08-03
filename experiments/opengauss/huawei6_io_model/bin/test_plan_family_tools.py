#!/usr/bin/env python3

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import joint_bidirectional_replay as replay
import plan_family_anchor_manifest as manifest
import validate_plan_family_spill as validation


class PlanFamilyToolsTest(unittest.TestCase):
    def test_representative_prefers_low_spill_observable_point(self) -> None:
        self.assertEqual(manifest.representative([1024, 1174]), 1024)
        self.assertEqual(manifest.representative([128, 512, 2048]), 128)

    def test_anchor_is_never_selected_across_plan_families(self) -> None:
        anchor = replay.TraceAnchor(256, Path("q5"), "q5_p1", [])
        anchors = {(5, "q5_p1"): [anchor]}
        self.assertIsNone(replay.choose_anchor(anchors, 5, "q5_p2", 512))

    def test_normalized_plan_sha_ignores_gsql_status_lines(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            raw = Path(root) / "plan.txt"
            clean = Path(root) / "clean.txt"
            raw.write_text("SET\nSET\nHash Join\n  -> Seq Scan\n\n", encoding="utf-8")
            clean.write_text("Hash Join\n  -> Seq Scan\n", encoding="utf-8")
            self.assertEqual(
                validation.normalized_plan_sha(raw),
                validation.normalized_plan_sha(clean),
            )

    def test_completed_anchor_is_keyed_by_actual_family(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            query = Path(root) / "q5"
            query.mkdir()
            (query / ".complete").write_text(
                "query_id=5\nwork_mem_mb=512\n", encoding="utf-8"
            )
            anchors = manifest.completed_anchors(
                [Path(root)], {(5, 512): "q5_p2"}
            )
            self.assertIn((5, "q5_p2"), anchors)
            self.assertNotIn((5, "q5_p1"), anchors)


if __name__ == "__main__":
    unittest.main()
