import os
import random
from pathlib import Path
import tempfile
import unittest

from huawei7.pipeline import (
    CandidateResult,
    _aligned_dataset_fingerprint,
    _select_candidate,
    _sensitivity_report,
    evaluate_bundle,
)
from huawei7.fio_surface import FioPointResult, validate_holdout, write_rows
from huawei7.schema import PageKey, TraceEvent, write_trace
from huawei7.transaction_evidence import (
    build_transaction_evidence, tp_command_contract_id,
)
import hashlib
import json


class PipelineTest(unittest.TestCase):
    @staticmethod
    def _candidate(
        shared_buffers_mb,
        predicted_tps,
        *,
        work_mem=((18, 64),),
        ap_dynamic_peak_mb=64.0,
    ):
        return CandidateResult(
            shared_buffers_mb=shared_buffers_mb,
            work_mem=tuple(work_mem),
            valid=True,
            invalid_reason="",
            ap_dynamic_peak_mb=ap_dynamic_peak_mb,
            os_cache_mb=1024.0,
            p_sb=0.99,
            p_os=0.01,
            p_disk=0.0,
            tp_read_requests_per_tx=0.0,
            tp_write_requests_per_tx=0.0,
            ap_read_iops=0.0,
            ap_write_iops=0.0,
            predicted_tps=predicted_tps,
            transaction_latency_ms=1.0,
            disk_path_latency_ms=1.0,
        )

    def test_default_selection_remains_max_tps(self):
        low = self._candidate(2048, 99.0)
        high = self._candidate(5120, 100.0, ap_dynamic_peak_mb=500.0)

        selected, diagnostics = _select_candidate([low, high], {})

        self.assertIs(selected, high)
        self.assertEqual(diagnostics["policy"], "max_tps")
        self.assertEqual(diagnostics["eligible_candidate_count"], 2)

    def test_resource_selection_uses_declared_tolerance(self):
        low = self._candidate(
            2048, 99.0, work_mem=((18, 64),), ap_dynamic_peak_mb=64.0,
        )
        high = self._candidate(
            5120, 100.0, work_mem=((18, 832),), ap_dynamic_peak_mb=500.0,
        )

        selected, diagnostics = _select_candidate(
            [low, high],
            {
                "selection_policy": "resource_minimal_near_optimal",
                "selection_tps_tolerance": 0.01,
            },
        )
        self.assertIs(selected, low)
        self.assertEqual(diagnostics["eligible_candidate_count"], 2)
        self.assertAlmostEqual(
            diagnostics["selected_candidate"]["predicted_tps"], 99.0,
        )

        selected, diagnostics = _select_candidate(
            [low, high],
            {
                "selection_policy": "resource_minimal_near_optimal",
                "selection_tps_tolerance": 0.005,
            },
        )
        self.assertIs(selected, high)
        self.assertEqual(diagnostics["eligible_candidate_count"], 1)

    def test_sensitivity_report_preserves_existing_candidate_grid(self):
        rows = [
            self._candidate(2048, 99.0, work_mem=((18, 64),)),
            self._candidate(2048, 99.1, work_mem=((18, 832),)),
            self._candidate(5120, 100.0, work_mem=((18, 832),)),
        ]

        report = _sensitivity_report(
            rows, rows[-1], reference_best_tps=100.0,
        )

        self.assertEqual(
            [row["shared_buffers_mb"] for row in report["sb_sensitivity"]],
            [2048, 5120],
        )
        self.assertEqual(report["sb_sensitivity"][0]["candidate_count"], 2)
        self.assertAlmostEqual(
            report["sb_sensitivity"][0]["best_delta_from_reference_fraction"],
            -0.009,
        )
        self.assertEqual(
            report["selected_shared_buffers_mb"], 5120,
        )

    def test_adaptive_ap_and_tp_must_share_dataset_audit(self):
        fingerprint = "a" * 64
        command = {
            "schema": "huawei7.tp-command/v2",
            "dataset": {"dataset_fingerprint": fingerprint},
        }
        self.assertEqual(
            _aligned_dataset_fingerprint(
                command, {"dataset_fingerprint": fingerprint},
            ),
            fingerprint,
        )
        with self.assertRaisesRegex(ValueError, "different dataset audits"):
            _aligned_dataset_fingerprint(
                command, {"dataset_fingerprint": "b" * 64},
            )

    def test_single_path_reaches_real_fiemap_bio_surface_and_tps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            relation = data_dir / "base" / "42" / "99"
            relation.parent.mkdir(parents=True)
            with relation.open("wb") as handle:
                handle.write(b"x" * 16384)
                handle.flush()
                os.fsync(handle.fileno())
            page0 = PageKey(1663, 42, 99, -1, 0, 0)
            page1 = PageKey(1663, 42, 99, -1, 0, 1)
            events = [
                TraceEvent(1, 100, 7, "ACCESS", page=page0, workload_class="tp"),
                TraceEvent(2, 110, 7, "RETURN", buffer_id=1, observed_hit=False,
                           workload_class="tp"),
                TraceEvent(3, 120, 7, "UNPIN", page=page0, buffer_id=1,
                           workload_class="tp"),
                TraceEvent(4, 200, 7, "ACCESS", page=page1, workload_class="tp"),
                TraceEvent(5, 210, 7, "RETURN", buffer_id=2, observed_hit=False,
                           workload_class="tp"),
                TraceEvent(6, 220, 7, "UNPIN", page=page1, buffer_id=2,
                           workload_class="tp"),
            ]
            trace = root / "trace.csv"
            write_trace(trace, events)
            transaction_source = root / "sysbench.log"
            transaction_source.write_text("[ 1s ] thds: 1 tps: 1.0\n")
            transaction = root / "transactions.json"
            transaction.write_text(json.dumps(build_transaction_evidence(
                benchmark="sysbench", source=transaction_source,
                machine_fingerprint="test-machine", trace_id="trace-1",
                warmup_seconds=0, measure_seconds=1,
            )))
            collection = root / "collection.json"
            tp_command = root / "tp-command.json"
            tp_command_document = {
                "schema": "huawei7.tp-command/v1",
                "machine_fingerprint": "test-machine",
                "benchmark": "sysbench", "terminals": 1,
                "warmup_seconds": 0, "measure_seconds": 1,
                "dataset": {"tables": 16, "rows_per_table": 4_000_000},
                "argv": ["sysbench"],
            }
            tp_command_document["command_contract_id"] = tp_command_contract_id(
                tp_command_document
            )
            tp_command.write_text(json.dumps(tp_command_document))
            tp_command_sha = hashlib.sha256(tp_command.read_bytes()).hexdigest()
            overhead_samples = []
            overhead_seed = 78137
            overhead_schedule = [
                (kind, repeat) for repeat in (1, 2, 3)
                for kind in ("baseline", "probe")
            ]
            random.Random(overhead_seed).shuffle(overhead_schedule)
            for order, (kind, repeat) in enumerate(
                overhead_schedule, 1,
            ):
                sample_tps = 100.0 if kind == "baseline" else 99.0
                native = root / ("overhead-%s-%d.log" % (kind, repeat))
                native.write_text("[ 1s ] thds: 1 tps: %.1f\n" % sample_tps)
                raw = root / ("overhead-%s-%d.raw" % (kind, repeat))
                raw.write_text("ACCESS_A,1\n" if kind == "probe" else "")
                stderr = root / ("overhead-%s-%d.stderr" % (kind, repeat))
                stderr.write_text("")
                artifact = lambda path: {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                overhead_samples.append({
                    "kind": kind, "repeat": repeat, "order": order,
                    "trace_id": "overhead-%s-%d" % (kind, repeat),
                    "tps": sample_tps,
                    "driver_logs": [dict(artifact(native), role="baseline")],
                    "transaction_components": [{
                        "role": "baseline", "source": str(native),
                        "warmup_seconds": 0,
                        "source_artifact": artifact(native),
                    }],
                    "buffer_raw_sha256": (
                        artifact(raw)["sha256"] if kind == "probe" else None
                    ),
                    "buffer_raw_artifact": artifact(raw),
                    "buffer_stderr_artifact": artifact(stderr),
                    "probe_access_fragments": 1 if kind == "probe" else None,
                })
            collection.write_text(json.dumps({
                "schema": "huawei7.synchronized-cache-validation/v2",
                "machine_fingerprint": "test-machine",
                "benchmark": "sysbench", "trace_id": "trace-1",
                "terminals": 1,
                "target_db_node": 42, "actual_shared_buffers_mb": 2,
                "trace_csv": str(trace),
                "transaction_evidence": str(transaction),
                "transaction_evidence_sha256": hashlib.sha256(
                    transaction.read_bytes()
                ).hexdigest(),
                "tp_command_json_sha256": tp_command_sha,
                "tp_command_contract_id": tp_command_document["command_contract_id"],
                "tp_command_artifact": {
                    "path": str(tp_command), "sha256": tp_command_sha,
                },
                "block_summary": {"start_ns": 0, "end_ns": 1000},
                "valid": True,
            }))
            machine_artifact = root / "machine.json"
            machine_artifact.write_text(json.dumps({
                "schema": "huawei7.machine/v1",
                "machine_fingerprint": "test-machine",
                "memory_bytes": 5 * 1024 * 1024,
            }))
            memory_artifact = root / "memory-budget.json"
            snapshot_evidence = []
            for sb in (1, 2, 3):
                snapshot = root / ("memory-sb-%d.json" % sb)
                snapshot.write_text(json.dumps({
                    "schema": "huawei7.memory-snapshot/v1",
                    "machine_fingerprint": "test-machine", "valid": True,
                    "shared_buffers_mb": sb,
                    "idle_checks": [{
                        "active_sessions_before": 0,
                        "active_sessions_after": 0,
                    } for _ in range(3)],
                    "samples": [{}, {}, {}],
                }))
                snapshot_evidence.append({
                    "path": str(snapshot),
                    "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                })
            memory_artifact.write_text(json.dumps({
                "schema": "huawei7.memory-budget/v1",
                "machine_fingerprint": "test-machine", "valid": True,
                "host_mb": 5, "database_fixed_mb": 1,
                "system_other_reserve_mb": 1, "tunable_pool_mb": 3,
                "snapshot_evidence": snapshot_evidence,
            }))
            fio_training = root / "fio-training.csv"
            fio_holdout = root / "fio-holdout.csv"
            training_rows = [
                FioPointResult(
                    "train", repeat, tp, ap, 1.0, 100, 10, 0,
                    1.0 + tp + ap, 0.1, 30,
                )
                for tp in (1, 3) for ap in (0, 2) for repeat in (1, 2, 3)
            ]
            holdout_rows = [
                FioPointResult(
                    "holdout", repeat, 2, ap, 1.0, 100, 10, 0,
                    3.0 + ap, 0.1, 30,
                )
                for ap in (0, 1, 2) for repeat in (1, 2, 3)
            ]
            write_rows(fio_training, training_rows)
            write_rows(fio_holdout, holdout_rows)
            fio_report = validate_holdout(
                training_rows, holdout_rows, "test-machine", 0.01,
            )
            fio_report["schema"] = "huawei7.fio-surface-holdout/v2"
            fio_report["input_artifacts"] = {
                "training": {
                    "path": str(fio_training),
                    "sha256": hashlib.sha256(fio_training.read_bytes()).hexdigest(),
                },
                "holdout": {
                    "path": str(fio_holdout),
                    "sha256": hashlib.sha256(fio_holdout.read_bytes()).hexdigest(),
                },
            }
            service_sources = []
            for service_class in (
                "tp_read_ms", "tp_write_ms", "ap_read_ms", "ap_write_ms",
            ):
                for repeat in (1, 2, 3):
                    service_raw = root / (
                        "service-%s-%d.json" % (service_class, repeat)
                    )
                    direction = (
                        "read" if service_class.endswith("read_ms") else "write"
                    )
                    service_raw.write_text(json.dumps({
                        "jobs": [{direction: {
                            "total_ios": 1, "clat_ns": {"mean": 100000},
                        }}],
                    }))
                    service_sources.append({
                        "kind": "fio_raw", "service_class": service_class,
                        "repeat": repeat, "path": str(service_raw),
                        "sha256": hashlib.sha256(
                            service_raw.read_bytes()
                        ).hexdigest(),
                    })
            generic_source = root / "generic-source.json"
            generic_source.write_text("{}\n")
            generic_sha = hashlib.sha256(generic_source.read_bytes()).hexdigest()

            def sources(kind, count, prefix):
                return [{
                    "kind": kind, "trace_id": "%s-%d" % (prefix, index),
                    "path": str(generic_source), "sha256": generic_sha,
                } for index in range(count)]

            def unique_sources(kind, count, prefix):
                rows = []
                for index in range(count):
                    path = root / ("%s-%d.json" % (prefix, index))
                    path.write_text(json.dumps({"repeat": index}) + "\n")
                    rows.append({
                        "kind": kind, "trace_id": "%s-%d" % (prefix, index),
                        "path": str(path),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    })
                return rows

            os_sources = (
                sources("synchronized_collection", 6, "os-c")
                + sources("transaction_evidence", 6, "os-t")
            )
            sweep_sources = (
                sources("synchronized_collection", 9, "sweep-c")
                + sources("transaction_evidence", 9, "sweep-t")
            )
            calibration_sources = (
                sources("synchronized_collection", 3, "cal-c")
                + sources("transaction_evidence", 3, "cal-t")
            )
            ap_sources = []
            for source_kind in (
                "source_manifest", "width_evidence", "query_sql",
                "ap_command", "candidate_explain",
                "plan_switch_evidence", "plan_switch_explain",
                "plan_switch_collection",
            ):
                ap_sources += sources(source_kind, 1, source_kind)
            ap_sources += sources("training_explain", 9, "ap-runtime-train")
            ap_sources += sources(
                "training_explain_collection", 9, "ap-runtime-train-meta",
            )
            ap_sources += sources("holdout_explain", 3, "ap-runtime-holdout")
            ap_sources += sources(
                "holdout_explain_collection", 3, "ap-runtime-holdout-meta",
            )
            ap_sources += unique_sources("training_device_delta", 3, "ap-train")
            ap_sources += unique_sources("holdout_device_delta", 3, "ap-holdout")
            config = {
                "schema": "huawei7.pipeline-config/v1",
                "machine_fingerprint": "test-machine",
                "machine": str(machine_artifact),
                "memory_budget": str(memory_artifact),
                "memory_grid_mb": 1,
                "tp_benchmark": "sysbench",
                "tp_collection": str(collection),
                "os_cache_model": {
                    "schema": "huawei7.os-cache-model/v2",
                    "machine_fingerprint": "test-machine",
                    "benchmark": "sysbench", "valid": True,
                    "terminals": 1,
                    "baseline_terminals": 1, "surge_terminals": 0,
                    "surge_start_phase": "none",
                    "command_contract_id": tp_command_document["command_contract_id"],
                    "selected_parameters": {
                        "active_fraction": 0.5, "shadow_multiplier": 4,
                        "refault_distance_factor": 1,
                    },
                    "non_buffer_read_requests_per_tx": 0,
                    "non_buffer_write_requests_per_tx": 0,
                    "bio_coalescing": {
                        "merge_window_ns": 0, "max_request_bytes": 8192,
                    },
                    "holdout": {
                        "schema": "huawei7.component-holdout/v1",
                        "component": "os_cache_physical_reads",
                        "machine_fingerprint": "test-machine",
                        "training_trace_ids": ["train"],
                        "holdout_trace_ids": ["h1", "h2", "h3"],
                        "maximum_allowed_mape": 0.1,
                        "samples": [
                            {"trace_id": "h1", "observed": 10, "predicted": 10, "evidence_sha256": "a" * 64},
                            {"trace_id": "h2", "observed": 20, "predicted": 20, "evidence_sha256": "b" * 64},
                            {"trace_id": "h3", "observed": 30, "predicted": 30, "evidence_sha256": "c" * 64},
                        ],
                    },
                    "source_artifacts": os_sources,
                },
                "_removed_trace_csv": str(trace), "_removed_trace_transaction_count": 1,
                "_removed_measurement_start_ns": 0, "_removed_measurement_end_ns": 1000,
                "trace_attribution": {
                    "target_db_node": 42, "minimum_tp_access_fraction": 1.0,
                },
                "buffer_probe_overhead": {
                    "schema": "huawei7.buffer-probe-overhead/v2",
                    "machine_fingerprint": "test-machine", "valid": True,
                    "repeats_per_arm": 3, "slowdown_fraction": .01,
                    "randomization_seed": overhead_seed,
                    "maximum_slowdown_fraction": .05,
                    "benchmark": "sysbench",
                    "terminals": 1, "baseline_terminals": 1,
                    "surge_terminals": 0, "surge_start_phase": None,
                    "warmup_seconds": 0, "measure_seconds": 1,
                    "command_json_sha256": tp_command_sha,
                    "command_artifact": {
                        "path": str(tp_command), "sha256": tp_command_sha,
                    },
                    "command_contract_id": tp_command_document["command_contract_id"],
                    "samples": overhead_samples,
                    "baseline_median_tps": 100.0,
                    "probe_median_tps": 99.0,
                },
                "cache_validation": {
                    "actual_shared_buffers_mb": 2,
                    "maximum_hit_mismatch_fraction": 0.0,
                },
                "_removed_memory": {
                    "tunable_pool_mb": 3, "host_mb": 5,
                    "database_fixed_mb": 1, "system_other_reserve_mb": 1,
                    "grid_mb": 1,
                },
                "ap_model_bundle": {
                    "schema": "huawei7.ap-model-bundle/v1",
                    "machine_fingerprint": "test-machine", "valid": True,
                    "model_bundle_id": "model-1",
                    "query_sha256": {"18": "9" * 64},
                    "runtime_holdout": {
                        "schema": "huawei7.component-holdout/v1",
                        "component": "ap_runtime_seconds",
                        "machine_fingerprint": "test-machine",
                        "training_trace_ids": ["rt"],
                        "holdout_trace_ids": ["r1", "r2", "r3"],
                        "maximum_allowed_mape": 0.1,
                        "samples": [
                            {"trace_id": "r1", "observed": 1, "predicted": 1, "evidence_sha256": "d" * 64},
                            {"trace_id": "r2", "observed": 2, "predicted": 2, "evidence_sha256": "e" * 64},
                            {"trace_id": "r3", "observed": 3, "predicted": 3, "evidence_sha256": "f" * 64},
                        ],
                    },
                    "request_holdout": {
                        "schema": "huawei7.component-holdout/v1",
                        "component": "ap_physical_requests",
                        "machine_fingerprint": "test-machine",
                        "training_trace_ids": ["qt"],
                        "holdout_trace_ids": ["q1", "q2", "q3"],
                        "maximum_allowed_mape": 0.1,
                        "samples": [
                            {"trace_id": "q1", "observed": 1, "predicted": 1, "evidence_sha256": "1" * 64},
                            {"trace_id": "q2", "observed": 2, "predicted": 2, "evidence_sha256": "2" * 64},
                            {"trace_id": "q3", "observed": 3, "predicted": 3, "evidence_sha256": "3" * 64},
                        ],
                    },
                    "query_options": {
                        "18": [{
                            "work_mem_mb": 1, "dynamic_peak_mb": 1,
                            "read_requests": 1, "write_requests": 0,
                            "execution_seconds": 1, "plan_family": "hash-a",
                            "evidence": {
                                "machine_fingerprint": "test-machine",
                                "model_bundle_id": "model-1",
                                "explain_sha256": "0" * 64,
                            },
                        }],
                    },
                    "source_artifacts": ap_sources,
                },
                "tp_sweep": {
                    "schema": "huawei7.tp-sweep/v2",
                    "machine_fingerprint": "test-machine",
                    "benchmark": "sysbench", "valid": True,
                    "terminals": 1,
                    "baseline_terminals": 1, "surge_terminals": 0,
                    "surge_start_phase": "none",
                    "command_contract_id": tp_command_document["command_contract_id"],
                    "rows": [
                    {"shared_buffers_mb": 1, "joint_hit_ratio": 0.5,
                     "physical_reads_per_tx": 2, "sustainable_tps": 10,
                     "machine_fingerprint": "test-machine", "repeats": 3,
                     "evidence_id": "sweep-1"},
                    {"shared_buffers_mb": 2, "joint_hit_ratio": 1.0,
                     "physical_reads_per_tx": 1, "sustainable_tps": 20,
                     "machine_fingerprint": "test-machine", "repeats": 3,
                     "evidence_id": "sweep-2"},
                    {"shared_buffers_mb": 3, "joint_hit_ratio": 1.0,
                     "physical_reads_per_tx": 1, "sustainable_tps": 20,
                     "machine_fingerprint": "test-machine", "repeats": 3,
                     "evidence_id": "sweep-3"},
                    ],
                    "source_artifacts": sweep_sources,
                },
                "stage": {
                    "ap_queries": [18], "tp_terminals": 1,
                    "tp_baseline_terminals": 1, "tp_surge_terminals": 0,
                    "sb_sample_count": 2,
                    "_removed_runtime_holdout": {
                        "schema": "huawei7.component-holdout/v1",
                        "component": "ap_runtime_seconds",
                        "machine_fingerprint": "test-machine",
                        "training_trace_ids": ["rt"],
                        "holdout_trace_ids": ["r1", "r2", "r3"],
                        "maximum_allowed_mape": 0.1,
                        "samples": [
                            {"trace_id": "r1", "observed": 1, "predicted": 1, "evidence_sha256": "d" * 64},
                            {"trace_id": "r2", "observed": 2, "predicted": 2, "evidence_sha256": "e" * 64},
                            {"trace_id": "r3", "observed": 3, "predicted": 3, "evidence_sha256": "f" * 64},
                        ],
                    },
                    "_removed_request_holdout": {
                        "schema": "huawei7.component-holdout/v1",
                        "component": "ap_physical_requests",
                        "machine_fingerprint": "test-machine",
                        "training_trace_ids": ["qt"],
                        "holdout_trace_ids": ["q1", "q2", "q3"],
                        "maximum_allowed_mape": 0.1,
                        "samples": [
                            {"trace_id": "q1", "observed": 1, "predicted": 1, "evidence_sha256": "1" * 64},
                            {"trace_id": "q2", "observed": 2, "predicted": 2, "evidence_sha256": "2" * 64},
                            {"trace_id": "q3", "observed": 3, "predicted": 3, "evidence_sha256": "3" * 64},
                        ],
                    },
                    "_removed_query_options": {
                        "18": [{
                            "work_mem_mb": 1, "dynamic_peak_mb": 1,
                            "read_requests": 1, "write_requests": 0,
                            "execution_seconds": 1, "plan_family": "hash-a",
                            "evidence": {
                                "machine_fingerprint": "test-machine",
                                "model_bundle_id": "model-1",
                                "explain_sha256": "0" * 64,
                            },
                        }],
                    },
                },
                "storage": {
                    "merge_window_ns": 0, "max_request_bytes": 8192,
                    "fio_validation": fio_report,
                    "service_calibration": {
                        "schema": "huawei7.service-times/v2",
                        "machine_fingerprint": "test-machine", "valid": True,
                        "service_times_ms": {
                            "tp_read_ms": 0.1, "tp_write_ms": 0.1,
                            "ap_read_ms": 0.1, "ap_write_ms": 0.1,
                        },
                        "source_artifacts": service_sources,
                    },
                },
                "openGauss_data_dir": str(data_dir),
                "tp_calibration": {
                    "schema": "huawei7.tp-latency-calibration/v2",
                    "valid": True, "repeats": 3,
                    "trace_ids": ["tp1", "tp2", "tp3"],
                    "benchmark": "sysbench",
                    "terminals": 1, "accesses_per_tx": 2,
                    "baseline_terminals": 1, "surge_terminals": 0,
                    "surge_start_phase": "none",
                    "command_contract_id": tp_command_document["command_contract_id"],
                    "sb_latency_ms": 0.01, "os_latency_ms": 0.1,
                    "l_other_ms": 10, "machine_fingerprint": "test-machine",
                    "source_artifacts": calibration_sources,
                },
            }
            def freeze(name, value):
                path = root / (name + ".json")
                path.write_text(json.dumps(value))
                return str(path)

            config["os_cache_model"] = freeze(
                "os-cache-model", config["os_cache_model"],
            )
            config["buffer_probe_overhead"] = freeze(
                "probe-overhead", config["buffer_probe_overhead"],
            )
            config["tp_sweep"] = freeze("tp-sweep", config["tp_sweep"])
            config["ap_model_bundle"] = freeze(
                "ap-model-bundle", config["ap_model_bundle"],
            )
            config["tp_calibration"] = freeze(
                "tp-calibration", config["tp_calibration"],
            )
            storage = config["storage"]
            storage["fio_validation"] = freeze(
                "fio-validation", storage["fio_validation"],
            )
            storage["service_calibration"] = freeze(
                "service-calibration", storage["service_calibration"],
            )
            result = evaluate_bundle(config)
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["valid_candidate_count"], 1)
            self.assertEqual(result["best"]["tp_read_requests_per_tx"], 2.0)
            self.assertGreater(result["best"]["predicted_tps"], 0)
            # P16/P17 handoff remains auditable in the same candidate result;
            # these fields do not add a model stage.
            self.assertIn("tp_queue_depth", result["best"])
            self.assertIn("ap_queue_depth", result["best"])
            self.assertIn("average_access_latency_ms", result["best"])
            self.assertIn("l_other_ms", result["best"])


if __name__ == "__main__":
    unittest.main()
