import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from huawei7.provenance import sha256
from huawei7.reproduction_audit import audit_reproduction
from huawei7.stage_spec import read_stage_spec


class ReproductionAuditTest(unittest.TestCase):
    def test_complete_tree_is_rehashed_and_raw_tampering_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage_spec = Path(__file__).resolve().parents[1] / "config" / "ppt_five_stages.json"
            stages = read_stage_spec(stage_spec)
            dataset_fingerprint = "d" * 64
            machine = root / "machine.json"
            machine.write_text(json.dumps({
                "schema": "huawei7.machine/v1", "machine_fingerprint": "m",
            }))
            contract = root / "contract.json"
            contract.write_text(json.dumps({"schema": "contract"}))
            doctor = root / "doctor.json"
            doctor.write_text(json.dumps({
                "schema": "huawei7.doctor/v1", "valid": True,
                "provenance": {
                    "valid": True, "mismatches": [],
                    "gaussdb_sha256": "g", "expected_reference_gaussdb_sha256": "g",
                },
            }))
            fresh = root / "fresh.json"
            fresh.write_text(json.dumps({
                "schema": "huawei7.fresh-machine-doctor/v1", "valid": True,
                "failures": [], "free_bytes": 200, "minimum_free_bytes": 160,
                "contract_sha256": sha256(contract),
                "provenance": {"valid": True},
                "machine": {"machine_fingerprint": "m"},
            }))
            dataset = root / "dataset.json"
            dataset.write_text(json.dumps({
                "schema": "huawei7.dataset-contract-audit/v2", "valid": True,
                "failures": [], "machine_fingerprint": "m",
                "dataset_fingerprint": dataset_fingerprint,
                "machine_artifact": {"path": str(machine), "sha256": sha256(machine)},
                "contract_artifact": {"path": str(contract), "sha256": sha256(contract)},
            }))
            dummy = root / "evidence.json"
            dummy.write_text(json.dumps({"valid": True}))
            evidence_names = {
                "machine", "memory_budget", "os_cache_model",
                "buffer_probe_overhead", "tp_sweep", "ap_model_bundle",
                "fio_validation", "service_calibration", "tp_calibration",
                "tp_collection", "tp_trace", "transaction_evidence",
            }
            model_paths = {}
            query_hashes = {
                query: hashlib.sha256(("q%d" % query).encode()).hexdigest()
                for query in (2, 9, 13, 18, 21)
            }
            for benchmark in ("sysbench", "benchbase-tpcc"):
                for stage in stages:
                    key = (benchmark, stage.name)
                    config = root / (benchmark + "-" + stage.name + "-config.json")
                    config.write_text(json.dumps({
                        "machine_fingerprint": "m", "tp_benchmark": benchmark,
                        "stage": {
                            "tp_terminals": stage.tp_terminals,
                            "tp_baseline_terminals": stage.tp_baseline_terminals,
                            "tp_surge_terminals": stage.tp_surge_terminals,
                        },
                    }))
                    artifacts = {
                        name: {
                            "path": str(machine if name == "machine" else dummy),
                            "sha256": sha256(machine if name == "machine" else dummy),
                        } for name in evidence_names
                    }
                    model = root / (benchmark + "-" + stage.name + "-model.json")
                    model.write_text(json.dumps({
                        "schema": "huawei7.ppt-architecture-result/v2",
                        "machine_fingerprint": "m", "tp_benchmark": benchmark,
                        "dataset_fingerprint": dataset_fingerprint,
                        "tp_terminals": stage.tp_terminals,
                        "tp_baseline_terminals": stage.tp_baseline_terminals,
                        "tp_surge_terminals": stage.tp_surge_terminals,
                        "tp_surge_start_phase": (
                            "measurement" if stage.tp_surge_terminals else None
                        ),
                        "pipeline_config_artifact": {
                            "path": str(config), "sha256": sha256(config),
                        },
                        "evidence_artifacts": artifacts,
                        "evidence_sha256": {
                            name: value["sha256"] for name, value in artifacts.items()
                        },
                        "ap_query_sha256": {
                            str(query): query_hashes[query]
                            for query in stage.ap_queries
                        },
                    }))
                    model_paths[key] = model
            recommendations = root / "recommendations.json"
            recommendation_rows = []
            for benchmark in ("sysbench", "benchbase-tpcc"):
                for stage in stages:
                    model = model_paths[(benchmark, stage.name)]
                    recommendation_rows.append({
                        "benchmark": benchmark, "stage": stage.name,
                        "tp_terminals": stage.tp_terminals,
                        "tp_baseline_terminals": stage.tp_baseline_terminals,
                        "tp_surge_terminals": stage.tp_surge_terminals,
                        "tp_surge_start_phase": (
                            "measurement" if stage.tp_surge_terminals else None
                        ),
                        "shared_buffers_mb": 4096,
                        "work_mem_by_query": {
                            str(query): 64 for query in stage.ap_queries
                        },
                        "predicted_tps": 100.0, "model_result": str(model),
                        "model_result_sha256": sha256(model),
                        "dataset_fingerprint": dataset_fingerprint,
                        "query_sha256": {
                            str(query): query_hashes[query]
                            for query in stage.ap_queries
                        },
                    })
            recommendations.write_text(json.dumps({
                "schema": "huawei7.five-stage-recommendations/v3",
                "machine_fingerprint": "m",
                "dataset_fingerprint": dataset_fingerprint,
                "benchmarks": ["sysbench", "benchbase-tpcc"],
                "selection_frozen_before_real_stage_measurements": True,
                "stages": recommendation_rows,
            }))
            runtime = root / "runtime.json"
            runtime.write_text(json.dumps({"schema": "huawei7.stage-runtime/v1"}))
            restart_command = root / "restart.json"
            restart_command.write_text(json.dumps(["restart", "{shared_buffers_mb}"]))
            episodes = []
            medians = []
            first_raw = None
            for benchmark in ("sysbench", "benchbase-tpcc"):
                for stage in stages:
                    for repeat in range(1, 4):
                        episode_dir = root / benchmark / stage.name / str(repeat)
                        episode_dir.mkdir(parents=True)
                        raw_evidence = []
                        for role in (["baseline", "surge"]
                                     if stage.tp_surge_terminals else ["baseline"]):
                            log = episode_dir / (role + ".log")
                            log.write_text("real raw output\n")
                            first_raw = first_raw or log
                            raw_evidence.append({
                                "kind": "tp_driver_log", "role": role,
                                "path": str(log), "sha256": sha256(log),
                            })
                        for query in stage.ap_queries:
                            log = episode_dir / ("q%d.log" % query)
                            log.write_text("real AP output\n")
                            raw_evidence.append({
                                "kind": "ap_query_log", "query": query,
                                "path": str(log), "sha256": sha256(log),
                            })
                        summary = episode_dir / "stage_summary.json"
                        summary.write_text(json.dumps({
                            "schema": "huawei7.real-stage-episode/v2", "valid": True,
                            "machine_fingerprint": "m", "benchmark": benchmark,
                            "dataset_fingerprint": dataset_fingerprint,
                            "stage": stage.name, "repeat": repeat,
                            "tp_terminals": stage.tp_terminals,
                            "tp_baseline_terminals": stage.tp_baseline_terminals,
                            "tp_surge_terminals": stage.tp_surge_terminals,
                            "tp_surge_start_phase": (
                                "measurement" if stage.tp_surge_terminals else None
                            ),
                            "ap_queries": list(stage.ap_queries),
                            "warmup_seconds": 30, "measurement_seconds": 120,
                            "instrumentation_output_during_measurement": {
                                "filesystem": "tmpfs",
                                "mountpoint": "/dev/shm",
                                "promoted_after_workload_stopped": True,
                            },
                            "ap_failures": [],
                            "ap_active_slots_cancelled_at_boundary": len(stage.ap_queries),
                            "executor": "row; enable_vector_engine=off", "query_dop": 1,
                            "throughput_tps": 100.0, "predicted_tps": 100.0,
                            "model_result_sha256": sha256(model_paths[(benchmark, stage.name)]),
                            "input_artifacts": {
                                "stage_spec": {"path": str(stage_spec), "sha256": sha256(stage_spec)},
                                "recommendations": {"path": str(recommendations), "sha256": sha256(recommendations)},
                                "runtime_config": {
                                    "path": str(runtime), "sha256": sha256(runtime),
                                },
                                "dataset_audit": {
                                    "path": str(dataset), "sha256": sha256(dataset),
                                },
                            },
                            "raw_evidence": raw_evidence,
                        }))
                        restart_log = episode_dir / "restart.log"
                        restart_log.write_text("restart succeeded\n")
                        episodes.append({
                            "order": len(episodes) + 1,
                            "benchmark": benchmark, "stage": stage.name,
                            "repeat": repeat, "throughput_tps": 100.0,
                            "predicted_tps": 100.0, "summary": str(summary),
                            "summary_sha256": sha256(summary),
                            "restart_log": str(restart_log),
                            "restart_log_sha256": sha256(restart_log),
                        })
                    medians.append({
                        "benchmark": benchmark, "stage": stage.name, "repeats": 3,
                        "predicted_tps": 100.0, "median_tps": 100.0,
                        "minimum_tps": 100.0, "maximum_tps": 100.0,
                        "absolute_prediction_error_fraction": 0.0,
                    })
            schedule = root / "schedule.json"
            schedule.write_text(json.dumps({
                "schema": "huawei7.five-stage-randomized-schedule/v1",
                "machine_fingerprint": "m",
                "dataset_fingerprint": dataset_fingerprint,
                "seed": 90217, "repeats": 3,
                "warmup_seconds": 30, "measure_seconds": 120,
                "input_artifacts": {
                    "stage_spec": {
                        "path": str(stage_spec), "sha256": sha256(stage_spec),
                    },
                    "recommendations": {
                        "path": str(recommendations),
                        "sha256": sha256(recommendations),
                    },
                    "runtime_config": {
                        "path": str(runtime), "sha256": sha256(runtime),
                    },
                    "restart_command": {
                        "path": str(restart_command),
                        "sha256": sha256(restart_command),
                    },
                    "dataset_audit": {
                        "path": str(dataset), "sha256": sha256(dataset),
                    },
                },
                "episodes": [{
                    "order": row["order"], "benchmark": row["benchmark"],
                    "repeat": row["repeat"], "stage": row["stage"],
                } for row in episodes],
            }))
            final = root / "final.json"
            final.write_text(json.dumps({
                "schema": "huawei7.real-five-stage-validation/v2", "valid": True,
                "accuracy_valid": True, "machine_fingerprint": "m",
                "dataset_fingerprint": dataset_fingerprint,
                "recommendations_sha256": sha256(recommendations),
                "recommendations_frozen_before_measurement": True,
                "input_artifacts": {
                    "stage_spec": {"path": str(stage_spec), "sha256": sha256(stage_spec)},
                    "recommendations": {"path": str(recommendations), "sha256": sha256(recommendations)},
                    "runtime_config": {"path": str(runtime), "sha256": sha256(runtime)},
                    "restart_command": {"path": str(restart_command), "sha256": sha256(restart_command)},
                    "dataset_audit": {"path": str(dataset), "sha256": sha256(dataset)},
                    "randomized_schedule": {
                        "path": str(schedule), "sha256": sha256(schedule),
                    },
                },
                "benchmarks": ["sysbench", "benchbase-tpcc"],
                "stage_count": 5, "repeats": 3, "episode_count": 30,
                "randomization_seed": 90217,
                "episodes": episodes, "median_throughput": medians,
                "maximum_stage_mape": 0.2,
            }))
            arguments = dict(
                doctor_path=doctor, fresh_doctor_path=fresh,
                dataset_audit_path=dataset, machine_path=machine,
                recommendations_path=recommendations,
                final_validation_path=final, stage_spec_path=stage_spec,
            )
            result = audit_reproduction(**arguments)
            self.assertTrue(result["valid"])
            self.assertEqual(result["episode_count"], 30)
            assert first_raw is not None
            first_raw.write_text("tampered\n")
            with self.assertRaisesRegex((ValueError, RuntimeError), "changed"):
                audit_reproduction(**arguments)


if __name__ == "__main__":
    unittest.main()
