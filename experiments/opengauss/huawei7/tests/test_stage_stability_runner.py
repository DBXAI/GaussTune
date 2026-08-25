import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "run_stage_stability_aa.py"
)
SPEC = importlib.util.spec_from_file_location("run_stage_stability_aa", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StageStabilityRunnerTest(unittest.TestCase):
    def test_cache_record_must_cover_exact_audited_oids(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "restart.log"
            record = {
                "schema": "huawei7.workload-cache-normalization/v1",
                "method": "POSIX_FADV_DONTNEED while openGauss is stopped",
                "database_oids": [1, 2, 3],
                "file_count": 10,
                "logical_bytes_advised": 100,
                "server_stopped_during_eviction": True,
                "valid": True,
            }
            log.write_text("restart output\n" + json.dumps(record) + "\n")
            self.assertEqual(
                MODULE._cache_normalization_from_log(log, [1, 2, 3]), record,
            )
            with self.assertRaisesRegex(RuntimeError, "differs"):
                MODULE._cache_normalization_from_log(log, [1, 2])

    def test_missing_or_duplicate_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "restart.log"
            log.write_text("no record\n")
            with self.assertRaisesRegex(RuntimeError, "one cache"):
                MODULE._cache_normalization_from_log(log, [1])

    def test_tpcc_reset_state_is_canonical_and_detects_row_drift(self):
        report = {
            "database": "tpcc",
            "database_oid": 42,
            "warehouses": 100,
            "random_seed": 15721,
            "transaction_weights": [45, 43, 4, 4, 4],
            "table_row_counts": {"warehouse": 100, "order_line": 30_001_892},
            "expected_exact_row_counts": {"warehouse": 100},
            "district_next_order_id": {"minimum": 3001, "maximum": 3001},
        }
        first = MODULE.tpcc_reset_logical_state(report)
        reordered = dict(report)
        reordered["table_row_counts"] = {
            "order_line": 30_001_892, "warehouse": 100,
        }
        self.assertEqual(first, MODULE.tpcc_reset_logical_state(reordered))
        drifted = dict(report)
        drifted["table_row_counts"] = {
            "warehouse": 100, "order_line": 30_001_893,
        }
        self.assertNotEqual(first, MODULE.tpcc_reset_logical_state(drifted))


if __name__ == "__main__":
    unittest.main()
