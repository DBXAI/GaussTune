import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from huawei7.transaction_evidence import (
    build_combined_transaction_evidence, build_transaction_evidence,
    read_transaction_evidence, tp_command_contract_id, tp_driver_topology,
    validate_tp_command_evidence,
)


class TransactionEvidenceTest(unittest.TestCase):
    def test_v2_contract_binds_exact_sysbench_argv(self):
        command = {
            "schema": "huawei7.tp-command/v2",
            "machine_fingerprint": "m", "benchmark": "sysbench",
            "terminals": 128, "baseline_terminals": 128,
            "surge_terminals": 0, "warmup_seconds": 10,
            "measure_seconds": 30, "drivers": [{
                "role": "baseline", "terminals": 128,
                "start_phase": "warmup",
                "argv": ["sysbench", "/x/oltp_read_only.lua", "run"],
            }],
        }
        first = tp_command_contract_id(command)
        command["drivers"][0]["argv"].insert(-1, "--rand-type=uniform")
        self.assertNotEqual(first, tp_command_contract_id(command))

    def test_v2_command_binds_measurement_phase_surge_topology(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "command.json"
            machine_path = Path(directory) / "machine.json"
            contract_path = Path(directory) / "contract.json"
            machine_path.write_text("{}")
            contract_path.write_text("{}")
            dataset_audit = Path(directory) / "dataset-audit.json"
            dataset_fingerprint = "d" * 64
            dataset_audit.write_text(json.dumps({
                "schema": "huawei7.dataset-contract-audit/v3",
                "profile": "test", "machine_fingerprint": "m",
                "valid": True, "failures": [],
                "dataset_fingerprint": dataset_fingerprint,
                "databases": {
                    "ap": "ap", "sysbench": "sb",
                    "benchbase_tpcc": "tpcc",
                },
                "database_oids": {
                    "ap": 1, "sysbench": 2, "benchbase_tpcc": 3,
                },
                "database_sizes_bytes": {
                    "ap": 10, "sysbench": 20, "benchbase_tpcc": 30,
                },
                "sysbench_table_count": 16,
                "sysbench_min_estimated_rows": 999000,
                "sysbench_max_estimated_rows": 1001000,
                "tpcc_warehouse_count": 100,
                "machine_artifact": {
                    "path": str(machine_path),
                    "sha256": hashlib.sha256(machine_path.read_bytes()).hexdigest(),
                },
                "contract_artifact": {
                    "path": str(contract_path),
                    "sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
                },
            }))
            command = {
                "schema": "huawei7.tp-command/v2",
                "machine_fingerprint": "m", "benchmark": "sysbench",
                "terminals": 144, "baseline_terminals": 128,
                "surge_terminals": 16, "warmup_seconds": 30,
                "measure_seconds": 60,
                "dataset": {
                    "schema": "huawei7.dataset-identity/v1",
                    "profile": "test", "database": "sb", "database_oid": 2,
                    "database_size_bytes": 20,
                    "dataset_fingerprint": dataset_fingerprint,
                    "tables": 16, "rows_per_table": 1_000_000,
                    "minimum_estimated_rows": 999000,
                    "maximum_estimated_rows": 1001000,
                    "audit_artifact": {
                        "path": str(dataset_audit),
                        "sha256": hashlib.sha256(
                            dataset_audit.read_bytes()
                        ).hexdigest(),
                    },
                },
                "drivers": [
                    {"role": "baseline", "terminals": 128,
                     "start_phase": "warmup", "argv": ["sysbench", "128"]},
                    {"role": "surge", "terminals": 16,
                     "start_phase": "measurement", "argv": ["sysbench", "16"]},
                ],
            }
            command["command_contract_id"] = tp_command_contract_id(command)
            path.write_text(json.dumps(command))
            raw_path = Path(directory) / "raw.log"
            raw_path.write_text("raw evidence\n")
            raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            raw_artifacts = [{
                "kind": kind, "path": str(raw_path), "sha256": raw_sha,
            } for kind in (
                "buffer_probe_raw", "buffer_probe_stderr", "block_probe_raw",
                "block_probe_stderr", "attribution_snapshots",
                "attribution_observer_log", "normalized_buffer_trace",
                "transaction_evidence",
            )] + [{
                "kind": "tp_driver_log", "role": role,
                "path": str(raw_path), "sha256": raw_sha,
            } for role in ("baseline", "surge")]
            collection = {
                "terminals": 144,
                "baseline_terminals": 128, "surge_terminals": 16,
                "trace_csv": str(raw_path),
                "transaction_evidence": str(raw_path),
                "tp_command_contract_id": command["command_contract_id"],
                "tp_command_artifact": {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                },
                "raw_artifacts": raw_artifacts,
            }
            validated = validate_tp_command_evidence(
                collection, machine_fingerprint="m", benchmark="sysbench",
            )
            topology = tp_driver_topology(validated)
            self.assertEqual(
                [(row["role"], row["terminals"], row["start_phase"])
                 for row in topology],
                [("baseline", 128, "warmup"),
                 ("surge", 16, "measurement")],
            )
            invalid = dict(command)
            invalid["surge_terminals"] = 15
            with self.assertRaisesRegex(ValueError, "surge"):
                tp_driver_topology(invalid)

    def test_tp_command_binds_exact_dataset_and_is_rehashed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "command.json"
            command = {
                "schema": "huawei7.tp-command/v1",
                "machine_fingerprint": "m", "benchmark": "sysbench",
                "terminals": 128, "warmup_seconds": 30,
                "measure_seconds": 60,
                "dataset": {"tables": 16, "rows_per_table": 4_000_000},
                "argv": ["sysbench"],
            }
            command["command_contract_id"] = tp_command_contract_id(command)
            path.write_text(json.dumps(command))
            import hashlib
            collection = {
                "terminals": 128,
                "tp_command_contract_id": command["command_contract_id"],
                "tp_command_artifact": {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                },
            }
            validate_tp_command_evidence(
                collection, machine_fingerprint="m", benchmark="sysbench",
            )
            path.write_text("{}")
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                validate_tp_command_evidence(
                    collection, machine_fingerprint="m", benchmark="sysbench",
                )

    def test_sysbench_source_is_parsed_and_reverified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "sysbench.log"
            log.write_text("".join(
                "[ %ds ] thds: 1 tps: %.1f\n" % (second, 10 + second)
                for second in range(1, 6)
            ))
            artifact = root / "transactions.json"
            artifact.write_text(json.dumps(build_transaction_evidence(
                benchmark="sysbench", source=log, machine_fingerprint="m",
                trace_id="t1", warmup_seconds=2, measure_seconds=3,
            )))
            transactions, seconds, digest = read_transaction_evidence(
                artifact, machine_fingerprint="m", trace_id="t1",
                benchmark="sysbench",
            )
            self.assertEqual((transactions, seconds), (42.0, 3.0))
            self.assertEqual(len(digest), 64)
            log.write_text("changed")
            with self.assertRaises(ValueError):
                read_transaction_evidence(
                    artifact, machine_fingerprint="m", trace_id="t1",
                    benchmark="sysbench",
                )

    def test_benchbase_measured_requests_are_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / "run.summary.json"
            summary.write_text(json.dumps({
                "Measured Requests": 1200,
                "Throughput (requests/second)": 20,
            }))
            result = build_transaction_evidence(
                benchmark="benchbase-tpcc", source=summary,
                machine_fingerprint="m", trace_id="t2",
                warmup_seconds=30, measure_seconds=60,
            )
            self.assertEqual(result["transactions"], 1200)
            self.assertEqual(result["scored_seconds"], 60)

    def test_combined_driver_transactions_are_reparsed_and_summed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline.log"
            surge = root / "surge.log"
            baseline.write_text("".join(
                "[ %ds ] thds: 128 tps: 10.0\n" % second
                for second in range(1, 6)
            ))
            surge.write_text("".join(
                "[ %ds ] thds: 16 tps: 2.0\n" % second
                for second in range(1, 4)
            ))
            combined = build_combined_transaction_evidence(
                benchmark="sysbench", components=[
                    {"role": "baseline", "source": str(baseline),
                     "warmup_seconds": 2},
                    {"role": "surge", "source": str(surge),
                     "warmup_seconds": 0},
                ], machine_fingerprint="m", trace_id="s5",
                measure_seconds=3,
            )
            artifact = root / "transactions.json"
            artifact.write_text(json.dumps(combined))
            transactions, seconds, _ = read_transaction_evidence(
                artifact, machine_fingerprint="m", trace_id="s5",
                benchmark="sysbench",
            )
            self.assertEqual((transactions, seconds), (36.0, 3.0))
            surge.write_text("changed")
            with self.assertRaises(ValueError):
                read_transaction_evidence(
                    artifact, machine_fingerprint="m", trace_id="s5",
                    benchmark="sysbench",
                )


if __name__ == "__main__":
    unittest.main()
