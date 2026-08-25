import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from huawei7.schema import PageKey, TraceEvent, write_trace
from huawei7.tp_calibration import build_tp_calibration
from huawei7.transaction_evidence import (
    build_transaction_evidence, tp_command_contract_id,
)


class TpCalibrationTest(unittest.TestCase):
    def test_sb_os_disk_latencies_and_lother_come_from_repeated_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os_model = root / "os_model.json"
            os_model.write_text(json.dumps({
                "schema": "huawei7.os-cache-model/v2",
                "machine_fingerprint": "m", "benchmark": "sysbench",
                "valid": True,
                "selected_parameters": {
                    "active_fraction": .5, "shadow_multiplier": 4,
                    "refault_distance_factor": 1,
                },
            }))

            def one(trace_id):
                sample = root / trace_id
                sample.mkdir()
                p0 = PageKey(1663, 42, 99, -1, 0, 0)
                p1 = PageKey(1663, 42, 99, -1, 0, 1)
                # One-slot SB, two-slot OS: disk, SB, disk, OS, SB.
                accesses = [(p0, False), (p0, True), (p1, False),
                            (p0, False), (p0, True)]
                events = []
                sequence = 1
                timestamp = 100
                for index, (page, hit) in enumerate(accesses):
                    events.extend([
                        TraceEvent(sequence, timestamp, 7, "ACCESS", page=page,
                                   workload_class="tp"),
                        TraceEvent(sequence + 1, timestamp + 10, 7, "RETURN",
                                   buffer_id=index + 1, observed_hit=hit,
                                   workload_class="tp"),
                        TraceEvent(sequence + 2, timestamp + 11, 7, "UNPIN",
                                   page=page, buffer_id=index + 1,
                                   workload_class="tp"),
                    ])
                    sequence += 3
                    timestamp += 100
                write_trace(sample / "buffer_trace.csv", events)
                collection = sample / "collection.json"
                command = sample / "tp-command.json"
                command_document = {
                    "schema": "huawei7.tp-command/v1",
                    "machine_fingerprint": "m", "benchmark": "sysbench",
                    "terminals": 1, "warmup_seconds": 0,
                    "measure_seconds": 1,
                    "dataset": {"tables": 16, "rows_per_table": 4_000_000},
                    "argv": ["sysbench"],
                }
                command_document["command_contract_id"] = tp_command_contract_id(
                    command_document
                )
                command.write_text(json.dumps(command_document))
                command_sha = hashlib.sha256(command.read_bytes()).hexdigest()
                collection.write_text(json.dumps({
                    "schema": "huawei7.synchronized-cache-validation/v2",
                    "trace_id": trace_id, "machine_fingerprint": "m",
                    "terminals": 1,
                    "actual_shared_buffers_mb": 8192 / 1024 / 1024,
                    "tp_command_contract_id": command_document["command_contract_id"],
                    "tp_command_artifact": {
                        "path": str(command.resolve()), "sha256": command_sha,
                    },
                    "valid": True,
                    "trace_quality": {"tp_access_fraction": 1.0},
                }))
                log = sample / "sysbench.log"
                log.write_text("[ 1s ] thds: 1 tps: 1.0\n")
                transaction = sample / "transactions.json"
                transaction.write_text(json.dumps(build_transaction_evidence(
                    benchmark="sysbench", source=log,
                    machine_fingerprint="m", trace_id=trace_id,
                    warmup_seconds=0, measure_seconds=1,
                )))
                collection_document = json.loads(collection.read_text())
                collection_document.update({
                    "benchmark": "sysbench",
                    "transaction_evidence": str(transaction.resolve()),
                    "transaction_evidence_sha256": hashlib.sha256(
                        transaction.read_bytes()
                    ).hexdigest(),
                })
                collection.write_text(json.dumps(collection_document))
                return {
                    "trace_id": trace_id,
                    "collection": str(collection.relative_to(root)),
                    "shared_buffers_mb": 8192 / 1024 / 1024,
                    "os_cache_mb": 16384 / 1024 / 1024,
                    "transaction_evidence": str(transaction.relative_to(root)),
                }

            result = build_tp_calibration({
                "schema": "huawei7.tp-calibration-manifest/v1",
                "machine_fingerprint": "m", "benchmark": "sysbench",
                "terminals": 1,
                "os_cache_model": os_model.name,
                "minimum_path_samples": 1,
                "maximum_hit_mismatch_fraction": 0,
                "samples": [one("r%d" % index) for index in range(3)],
            }, root)
            self.assertTrue(result["valid"])
            self.assertEqual(result["repeats"], 3)
            self.assertEqual(result["samples"][0]["path_counts"],
                             {"sb": 2, "os": 1, "disk": 2})
            self.assertGreater(result["l_other_ms"], 0)


if __name__ == "__main__":
    unittest.main()
