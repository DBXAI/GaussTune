import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_sysbench_online_ppt_five_stage import (
    _read_tps_window,
    _validate_trajectory,
    run,
)
from scripts.compare_sysbench_ap_latency import _find_execution_time


class OnlineSysbenchFiveStageTest(unittest.TestCase):
    def _trajectory(self):
        rows = []
        queries = {
            "S1": [18],
            "S2": [18, 21],
            "S3": [9, 13, 18, 21],
            "S4": [2, 9, 13, 18, 21],
            "S5": [9, 13, 18, 21],
        }
        after = {
            "S1": [[18, 832]],
            "S2": [[18, 832], [21, 2944]],
            "S3": [[9, 64], [13, 64], [18, 64], [21, 64]],
            "S4": [[2, 64], [9, 64], [13, 64], [18, 64], [21, 64]],
            "S5": [[9, 64], [13, 64], [18, 64], [21, 64]],
        }
        before = {
            "S1": 512,
            "S2": 5120,
            "S3": 4096,
            "S4": 4096,
            "S5": 4096,
        }
        sb_after = {
            "S1": 5120,
            "S2": 4096,
            "S3": 4096,
            "S4": 4096,
            "S5": 5120,
        }
        for stage in ("S1", "S2", "S3", "S4", "S5"):
            rows.append({
                "stage": stage,
                "shared_buffers_before_mb": before[stage],
                "shared_buffers_after_mb": sb_after[stage],
                "work_mem_before": [],
                "work_mem_after": after[stage],
                "candidate": {
                    "work_mem": [[query, 64] for query in queries[stage]],
                },
                "admitted_ap_clients": (
                    4 if stage == "S4" else
                    3 if stage == "S5" else len(queries[stage])
                ),
            })
        return {
            "schema": "huawei7.sysbench-ppt-dynamic-acceptance/v1",
            "transitions": rows,
        }

    def test_validates_exact_ppt_stage_order_and_queue_projection(self):
        rows, startup = _validate_trajectory(self._trajectory())
        self.assertEqual([row["stage"] for row in rows], ["S1", "S2", "S3", "S4", "S5"])
        self.assertEqual(startup, 5120)

    def test_rejects_missing_stage(self):
        trajectory = self._trajectory()
        trajectory["transitions"] = trajectory["transitions"][:-1]
        with self.assertRaises(ValueError):
            _validate_trajectory(trajectory)

    def test_reads_only_new_sysbench_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sysbench.log"
            path.write_text(
                "[ 1s ] thds: 1 tps: 10.0\n"
                "[ 2s ] thds: 1 tps: 20.0\n",
                encoding="utf-8",
            )
            offset = len(path.read_text(encoding="utf-8"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write("[ 3s ] thds: 1 tps: 30.0\n")
                handle.write("[ 4s ] thds: 1 tps: 50.0\n")
            self.assertEqual(_read_tps_window(path, offset), (40.0, 2))

    def test_reads_opengauss_total_runtime_from_explain_json(self):
        self.assertEqual(
            _find_execution_time([{"Plan": {}, "Total Runtime": 123.5}]),
            123.5,
        )

    def test_dry_run_does_not_touch_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime.json"
            trajectory = root / "trajectory.json"
            output = root / "out"
            runtime.write_text(json.dumps({"schema": "huawei7.stage-runtime/v1"}))
            trajectory.write_text(json.dumps(self._trajectory()))
            document = run(
                data_dir=root / "data",
                gausshome=root / "gausshome",
                database="postgres",
                runtime_config=runtime,
                trajectory_path=trajectory,
                out_dir=output,
                startup_max_mb=5120,
                initial_target_mb=512,
                stage_seconds=5,
                settle_seconds=0,
                resize_timeout_seconds=1,
                repeat=1,
                dry_run=True,
            )
            self.assertTrue(document["dry_run"])
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
