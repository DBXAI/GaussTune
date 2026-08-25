import json
from pathlib import Path
import tempfile
import unittest

from huawei7.provenance import sha256
from huawei7.reproduction_audit import _validate_normalized_tpcc_episode
from huawei7.stability import assess_precondition_convergence


class StableHoldoutEvidenceTest(unittest.TestCase):
    def test_normalized_tpcc_chain_rehashes_every_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime.json"
            runtime.write_text("{}")
            dataset = root / "dataset.json"
            dataset.write_text("{}")
            checkpoint_command = root / "checkpoint-command.json"
            checkpoint_command.write_text("[]")
            reset_command = root / "reset-command.json"
            reset_command.write_text("[]")
            inputs = {
                name: {"path": str(path.resolve()), "sha256": sha256(path)}
                for name, path in (
                    ("runtime_config", runtime),
                    ("dataset_audit", dataset),
                    ("checkpoint_command", checkpoint_command),
                    ("dataset_reset_command", reset_command),
                )
            }
            quiescence = {
                "schema": "huawei7.storage-quiescence/v1",
                "valid": True,
                "checkpoint_completed": True,
                "device": "/dev/test",
                "required_consecutive_samples": 3,
                "accepted_consecutive_samples": 3,
                "samples": [{"sample": value} for value in range(1, 4)],
            }
            checkpoint_log = root / "checkpoint.log"
            checkpoint_log.write_text(json.dumps(quiescence) + "\n")
            reset_log = root / "reset.log"
            reset_log.write_text("seeded load completed\n")
            reset = root / "reset.json"
            exact_counts = {
                "warehouse": 1, "district": 10, "customer": 30000,
                "history": 30000, "oorder": 30000, "new_order": 9000,
                "stock": 100000, "item": 100000,
            }
            reset.write_text(json.dumps({
                "schema": "huawei7.tpcc-dataset-reset/v1",
                "valid": True,
                "machine_fingerprint": "machine",
                "dataset_fingerprint": "d" * 64,
                "connection_transport": "password-authenticated-dedicated-role",
                "database": "tpcc", "database_oid": 42,
                "warehouses": 1, "random_seed": 15721,
                "transaction_weights": [45, 43, 4, 4, 4],
                "runtime_config": inputs["runtime_config"],
                "dataset_audit": inputs["dataset_audit"],
                "table_row_counts": dict(exact_counts, order_line=150001),
                "expected_exact_row_counts": exact_counts,
                "district_next_order_id": {"minimum": 3001, "maximum": 3001},
                "available_bytes_after_reset": 200,
                "minimum_free_bytes": 100,
            }))
            throughputs = [100.0, 101.0, 99.0]
            samples = []
            for number, throughput in enumerate(throughputs, 1):
                driver = root / ("run-%02d.log" % number)
                summary = root / ("run-%02d.json" % number)
                sample_checkpoint = root / ("run-%02d.checkpoint.log" % number)
                driver.write_text("raw driver\n")
                summary.write_text("{}")
                sample_checkpoint.write_text(json.dumps(quiescence) + "\n")
                samples.append({
                    "run": number, "throughput_tps": throughput,
                    "driver_log": {
                        "path": str(driver), "sha256": sha256(driver),
                    },
                    "summary": {
                        "path": str(summary), "sha256": sha256(summary),
                    },
                    "checkpoint_log": {
                        "path": str(sample_checkpoint),
                        "sha256": sha256(sample_checkpoint),
                    },
                    "storage_quiescence": quiescence,
                })
            precondition = root / "precondition.json"
            precondition.write_text(json.dumps({
                "schema": "huawei7.tp-adaptive-precondition/v1",
                "valid": True, "converged": True,
                "benchmark": "benchbase-tpcc", "terminals": 128,
                "connection_transport": "password-authenticated-dedicated-role",
                "runtime_config": inputs["runtime_config"],
                "between_run_postcondition": {
                    "checkpoint_command": inputs["checkpoint_command"],
                },
                "samples": samples,
                "convergence": assess_precondition_convergence(
                    throughputs, required_tail_runs=3,
                    maximum_relative_range=.10,
                ),
            }))
            row = {
                "dataset_reset": {
                    "path": str(reset), "sha256": sha256(reset),
                    "log": str(reset_log), "log_sha256": sha256(reset_log),
                },
                "adaptive_precondition": {
                    "path": str(precondition), "sha256": sha256(precondition),
                },
                "checkpoint_log": str(checkpoint_log),
                "checkpoint_log_sha256": sha256(checkpoint_log),
                "storage_quiescence": quiescence,
            }
            contract = {
                "database": "tpcc", "database_oid": 42,
                "warehouses": 1, "random_seed": 15721,
            }
            state = _validate_normalized_tpcc_episode(
                row, machine="machine", dataset_fingerprint="d" * 64,
                terminals=128, inputs=inputs, reset_contract=contract,
            )
            self.assertEqual(state["district_next_order_id"]["minimum"], 3001)
            checkpoint_log.write_text("tampered\n")
            with self.assertRaisesRegex(ValueError, "artifacts changed"):
                _validate_normalized_tpcc_episode(
                    row, machine="machine", dataset_fingerprint="d" * 64,
                    terminals=128, inputs=inputs, reset_contract=contract,
                )


if __name__ == "__main__":
    unittest.main()
