import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from huawei7.plan_switch import build_plan_switch_evidence


class PlanSwitchTest(unittest.TestCase):
    def test_complete_blind_grid_derives_family_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            query = root / "q18.sql"
            query.write_text("select 1;\n")
            query_sha = hashlib.sha256(query.read_bytes()).hexdigest()
            plans = []
            for memory, node in ((1, "Seq Scan"), (2, "Seq Scan"), (3, "Index Scan")):
                path = root / ("p%d.json" % memory)
                path.write_text(json.dumps([{"Plan": {
                    "Node Type": node, "Plan Rows": 1, "Plan Width": 8,
                }}]))
                collection = root / ("p%d.collection.json" % memory)
                collection.write_text(json.dumps({
                    "schema": "huawei7.blind-explain-collection/v1",
                    "machine_fingerprint": "m", "query_id": "18",
                    "work_mem_mb": memory, "query_sha256": query_sha,
                    "explain_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "executor": "row; enable_vector_engine=off",
                    "query_dop": 1,
                    "blind": True, "valid": True,
                }))
                plans.append({
                    "work_mem_mb": memory, "explain": path.name,
                    "collection": collection.name,
                })
            result = build_plan_switch_evidence({
                "schema": "huawei7.plan-switch-manifest/v1",
                "machine_fingerprint": "m", "query_id": 18,
                "minimum_mb": 1, "maximum_mb": 3, "grid_mb": 1,
                "plans": plans,
            }, root)
            self.assertEqual(result["plan_switch_points_mb"], [3])


if __name__ == "__main__":
    unittest.main()
