import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from huawei7.schema import PageKey, TraceEvent, write_trace
from huawei7.tp_sweep import build_tp_sweep
from huawei7.transaction_evidence import (
    build_transaction_evidence, tp_command_contract_id,
)


class TpSweepArtifactTest(unittest.TestCase):
    def test_uniform_repeated_sweep_derives_first_99_percent_point(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os_model = root / "os.json"
            os_model.write_text(json.dumps({
                "schema": "huawei7.os-cache-model/v2", "machine_fingerprint": "m",
                "benchmark": "sysbench", "valid": True,
                "non_buffer_read_requests_per_tx": 0,
                "selected_parameters": {"active_fraction": .5,
                                        "shadow_multiplier": 4,
                                        "refault_distance_factor": 1},
            }))

            def one(trace_id, shared_buffers):
                sample = root / trace_id
                sample.mkdir()
                page = PageKey(1663, 42, 99, -1, 0, 0)
                events = [
                    TraceEvent(1, 1, 7, "ACCESS", page=page, workload_class="tp"),
                    TraceEvent(2, 2, 7, "RETURN", buffer_id=1, observed_hit=False,
                               workload_class="tp"),
                    TraceEvent(3, 3, 7, "UNPIN", page=page, buffer_id=1,
                               workload_class="tp"),
                    TraceEvent(4, 4, 7, "ACCESS", page=page, workload_class="tp"),
                    TraceEvent(5, 5, 7, "RETURN", buffer_id=1, observed_hit=True,
                               workload_class="tp"),
                    TraceEvent(6, 6, 7, "UNPIN", page=page, buffer_id=1,
                               workload_class="tp"),
                ]
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
                    "trace_id": trace_id, "machine_fingerprint": "m", "valid": True,
                    "terminals": 1, "actual_shared_buffers_mb": shared_buffers,
                    "tp_command_contract_id": command_document["command_contract_id"],
                    "tp_command_artifact": {
                        "path": str(command.resolve()), "sha256": command_sha,
                    },
                    "trace_quality": {"tp_access_fraction": 1.0},
                    "block_summary": {"rows": [
                        {"workload_class": "tp", "rw": "R", "requests": 1},
                    ]},
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
                return {"trace_id": trace_id,
                        "collection": str(collection.relative_to(root)),
                        "os_cache_mb": 0,
                        "transaction_evidence": str(transaction.relative_to(root))}

            points = []
            for shared_buffers in (1, 2, 3):
                points.append({
                    "shared_buffers_mb": shared_buffers,
                    "samples": [one("sb%d-r%d" % (shared_buffers, repeat), shared_buffers)
                                for repeat in range(3)],
                })
            result = build_tp_sweep({
                "schema": "huawei7.tp-sweep-manifest/v1",
                "machine_fingerprint": "m", "benchmark": "sysbench",
                "os_cache_model": os_model.name,
                "maximum_hit_mismatch_fraction": 0,
                "hit_plateau_fraction": .99, "points": points,
            }, root)
            self.assertTrue(result["valid"])
            self.assertEqual(result["grid_mb"], 1)
            self.assertEqual(result["b_high_mb"], 1)


if __name__ == "__main__":
    unittest.main()
