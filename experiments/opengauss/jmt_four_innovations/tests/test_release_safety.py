import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jmt.common.db import Gsql, readonly_query, extract_json
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_2_spill.labels import actual_label, score
from jmt.innovation_3_search.objective import select_for_target_tps
from jmt.innovation_3_search.search import QueryOption, solve_work_mem_dp
from jmt.innovation_4_performance.device import DeviceSurface, SurfacePoint, ServiceTimes
from jmt.innovation_4_performance.tps import TpLatencyCalibration, solve_capacity_tps
from jmt.common.resources import resources_root


class ReleaseSafetyTest(unittest.TestCase):
    def test_unknown_AP_latency_cannot_win_as_zero(self):
        row = dict(predicted_tps=8000, ap_execution_seconds=None,
                   ap_dynamic_peak_mb=10, shared_buffers_mb=512,
                   work_mem=((18, 64),))
        with self.assertRaisesRegex(ValueError, "unknown"):
            select_for_target_tps([row], 7000)

    def test_nan_objective_is_rejected(self):
        row = dict(predicted_tps=float("nan"), ap_execution_seconds=1,
                   ap_dynamic_peak_mb=10, shared_buffers_mb=512, work_mem=((18, 64),))
        with self.assertRaises(ValueError):
            select_for_target_tps([row], 7000)
        row["predicted_tps"] = 8000
        with self.assertRaises(ValueError):
            select_for_target_tps([row], float("inf"))

    def test_DP_rejects_invalid_runtime_and_memory(self):
        for runtime, peak in [(0, 1), (1, float("nan")), (float("inf"), 1), (1, -1)]:
            with self.assertRaises(ValueError):
                solve_work_mem_dp({1: (QueryOption(1, 32, peak, 1, 1, runtime, "unit"),)}, 100)

    def test_wm_not_physical_peak(self):
        # Synthetic unit inputs, not runtime evidence.
        option = QueryOption(1, 1024, 10, 1, 1, 1, "unit")
        self.assertEqual(len(solve_work_mem_dp({1: (option,)}, 11)), 1)

    def test_finite_fio_service_and_calibration(self):
        with self.assertRaises(ValueError):
            ServiceTimes(1, float("nan"), 1, 1)
        with self.assertRaises(ValueError):
            TpLatencyCalibration(1, 1, .1, .1, float("inf"), "unit")
        with self.assertRaises(ValueError):
            DeviceSurface([SurfacePoint(0, 0, float("nan"))], "unit")

    def test_nonconvergent_fixed_point_rejected(self):
        surface = DeviceSurface([SurfacePoint(x, y, 1) for x in (0., 4.) for y in (0., 4.)], "unit")
        # Alternating surface is a deliberate adversarial UNIT stub.
        with patch.object(surface, "latency_ms", side_effect=lambda *a: 1 if calls.pop() else 100):
            calls = [i % 2 for i in range(1000)]
            with self.assertRaisesRegex(RuntimeError, "converge"):
                solve_capacity_tps(
                    calibration=TpLatencyCalibration(1, 100, .1, .1, 1, "unit"),
                    p_sb=0, p_os=0, p_disk=1, tp_read_requests_per_tx=0,
                    tp_write_requests_per_tx=0, ap_read_iops=0, ap_write_iops=0,
                    service=ServiceTimes(1, 1, 1, 1), surface=surface,
                )

    def test_forecast_rejects_nan_before_evaluating_plan(self):
        from jmt.common.memory_units import MAX_WORK_MEM_MIB
        session = ForecastSession("select 1", {"relations": [], "columns": []})
        for wm in (float("nan"), float("inf"), 0, -1, MAX_WORK_MEM_MIB + 1):
            with self.assertRaises(ValueError):
                session.forecast([], wm)

    def test_missing_buffers_NOT_zero(self):
        with self.assertRaises(ValueError):
            actual_label([{"Plan": {"Node Type": "Aggregate"}}])
        label = actual_label([{"Plan": {"Node Type": "Limit", "Plans": [
            {"Node Type": "Sort", "Temp Read Blocks": 2, "Temp Written Blocks": 3}
        ]}}])
        self.assertEqual(label["write_bytes"], 3 * 8192)

    def test_missing_label_NOT_scored(self):
        report = score([{"valid_label": False}])
        self.assertEqual(report["complete_numeric_predictions"], 0)
        self.assertIsNone(report["combined_read_write_WAPE_pct"])

    def test_portable_resource_files_exist(self):
        self.assertTrue((resources_root() / "probes/block_rq_completion_total.bt").is_file())
        self.assertTrue((resources_root() / "scripts/run_gsql_with_password.py").is_file())

    def test_real_array_json_strings_normalised(self):
        class Fake:
            def json(self, *args):
                return {"columns": [{"mcv_freqs": [".2", ".3"]}],
                        "nonnull_unique_keys": [], "relations": [], "settings": []}
        self.assertEqual(collect_catalog(Fake(), "unit")["columns"][0]["mcv_freqs"], [.2, .3])

    def test_readonly_SQL_rejects_writes_and_multistatements(self):
        for sql in ("select 1;drop table x", "delete from x", "select 1 into x",
                    "select pg_sleep(1)", "select * from x for update",
                    "with x as (delete from y returning *) select * from x"):
            with self.assertRaises(ValueError):
                readonly_query(sql)
        self.assertTrue(readonly_query("select ';' as x;").startswith("select"))
        self.assertTrue(readonly_query("select 1 union select 2").startswith("select"))

    def test_json_extraction_skips_notices(self):
        self.assertEqual(extract_json('NOTICE: message\n [ {"Plan": {}} ]\n'), [{"Plan": {}}])

    def test_gsql_batch_mode_keeps_multiline_in_one_command(self):
        import subprocess
        fake = subprocess.CompletedProcess([], 0, '[{"Plan":{}}]', '')
        with patch("shutil.which", return_value="/bin/true"), \
                patch("subprocess.run", return_value=fake) as run:
            client = Gsql("gsql")
            client.execute("unit", "SELECT\n1")
            argv = run.call_args[0][0]
            self.assertIn("-c", argv)
            self.assertIn("START TRANSACTION READ ONLY;SELECT\n1;ROLLBACK;", argv[argv.index("-c") + 1])
            self.assertNotIn("BEGIN;", argv[argv.index("-c") + 1])

    def test_kernel_control_requires_explicit_opt_in(self):
        from jmt.innovation_1_cache.kernel_trace_control import set_guc
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "explicit"):
                set_guc("jmt_buffer_trace_epoch", 1)


if __name__ == "__main__":
    unittest.main()
