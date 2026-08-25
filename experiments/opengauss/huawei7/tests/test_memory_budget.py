import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from huawei7.memory_budget import build_memory_budget
from scripts.collect_memory_snapshot import active_client_sessions


class MemoryBudgetTest(unittest.TestCase):
    def test_idle_gate_excludes_only_named_opengauss_background_jobs(self):
        with mock.patch(
            "scripts.collect_memory_snapshot.run_gsql", return_value="0",
        ) as query:
            self.assertEqual(active_client_sessions(object(), {}), 0)
        sql = query.call_args.args[1]
        self.assertIn("JobScheduler", sql)
        self.assertIn("pid <> pg_backend_pid()", sql)
        self.assertNotIn("application_name LIKE", sql)

    def test_three_sb_settings_separate_fixed_memory_and_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for sb in (1000, 2000, 3000):
                path = root / ("sb%d.json" % sb)
                path.write_text(json.dumps({
                    "schema": "huawei7.memory-snapshot/v1",
                    "machine_fingerprint": "m", "memory_bytes": 10000 * 1024 ** 2,
                    "shared_buffers_mb": sb, "valid": True,
                    "idle_checks": [{
                        "active_sessions_before": 0,
                        "active_sessions_after": 0,
                    } for _ in range(3)],
                    "samples": [{
                        "sysv_virtual_mb": sb * 1.02 + 500,
                        "private_rss_mb": 100,
                        "non_db_nonreclaimable_mb": 200,
                    } for _ in range(3)],
                }))
                paths.append(path.name)
            result = build_memory_budget({
                "schema": "huawei7.memory-budget-manifest/v1",
                "machine_fingerprint": "m", "safety_margin_mb": 50,
                "snapshots": paths,
            }, root)
            self.assertEqual(result["database_fixed_mb"], 600)
            self.assertEqual(result["system_other_reserve_mb"], 250)
            self.assertAlmostEqual(
                result["fit"]["sysv_mb_per_shared_buffer_mb"], 1.02,
            )


if __name__ == "__main__":
    unittest.main()
