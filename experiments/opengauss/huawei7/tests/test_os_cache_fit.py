import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from huawei7.os_cache_fit import fit_os_cache_model
from huawei7.schema import PageKey, TraceEvent, write_trace
from huawei7.transaction_evidence import (
    build_transaction_evidence, tp_command_contract_id,
)


class OsCacheFitTest(unittest.TestCase):
    def test_real_fiemap_training_and_disjoint_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def sample(trace_id):
                sample_root = root / trace_id
                data = sample_root / "data"
                relation_dir = data / "base" / "42"
                relation_dir.mkdir(parents=True)
                relation = relation_dir / "99"
                with relation.open("wb") as handle:
                    handle.write(os.urandom(16384))
                    handle.flush()
                    os.fsync(handle.fileno())
                pages = [PageKey(1663, 42, 99, -1, 0, index) for index in (0, 1)]
                events = []
                seq = 1
                for index, page in enumerate(pages):
                    timestamp = 100 + index * 100
                    events.extend([
                        TraceEvent(seq, timestamp, 7, "ACCESS", page=page,
                                   workload_class="tp"),
                        TraceEvent(seq + 1, timestamp + 1, 7, "RETURN", buffer_id=index + 1,
                                   observed_hit=False, workload_class="tp"),
                        TraceEvent(seq + 2, timestamp + 2, 7, "UNPIN", page=page,
                                   buffer_id=index + 1, workload_class="tp"),
                    ])
                    seq += 3
                write_trace(sample_root / "buffer_trace.csv", events)
                collection = sample_root / "collection.json"
                command = sample_root / "tp-command.json"
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
                    "benchmark": "sysbench", "terminals": 1,
                    "actual_shared_buffers_mb": 1,
                    "tp_command_contract_id": command_document["command_contract_id"],
                    "tp_command_artifact": {
                        "path": str(command.resolve()), "sha256": command_sha,
                    },
                    "block_summary": {
                        "start_ns": 0, "end_ns": 1000,
                        "rows": [{"workload_class": "tp", "rw": "R", "requests": 2}],
                    },
                }))
                log = sample_root / "sysbench.log"
                log.write_text("[ 1s ] thds: 1 tps: 1.0\n")
                transaction = sample_root / "transactions.json"
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
                    "data_dir": str(data.relative_to(root)),
                    "shared_buffers_mb": 1, "os_cache_mb": 0,
                    "transaction_evidence": str(transaction.relative_to(root)),
                }

            training = [sample("train-%d" % index) for index in range(3)]
            holdout = [sample("hold-%d" % index) for index in range(3)]
            result = fit_os_cache_model({
                "schema": "huawei7.os-cache-fit-manifest/v1",
                "machine_fingerprint": "m", "benchmark": "sysbench",
                "training_samples": training, "holdout_samples": holdout,
                "parameter_candidates": [{
                    "active_fraction": .5, "shadow_multiplier": 4,
                    "refault_distance_factor": 1,
                }],
                "merge_window_ns": 0, "max_request_bytes": 8192,
                "maximum_holdout_mape": .01,
            }, root)
            self.assertTrue(result["valid"])
            self.assertEqual(result["holdout_result"]["samples"], 3)
            self.assertEqual(result["bio_coalescing"]["max_request_bytes"], 8192)


if __name__ == "__main__":
    unittest.main()
