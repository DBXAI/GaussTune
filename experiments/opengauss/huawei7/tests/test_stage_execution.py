import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from huawei7.stage_execution import (
    ap_gsql_command, benchbase_xml, parse_sysbench_tps, read_recommendations,
    sysbench_command, tp_connection, validate_stage_raw_evidence,
)
from huawei7.stage_spec import read_stage_spec


class StageExecutionTest(unittest.TestCase):
    def test_exact_recommendations_and_both_tp_drivers(self):
        stages = read_stage_spec(
            Path(__file__).resolve().parents[1] / "config" / "ppt_five_stages.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "recommendations.json"
            model_paths = {}
            machine_artifact = root / "machine.json"
            machine_artifact.write_text(json.dumps({
                "schema": "huawei7.machine/v1", "machine_fingerprint": "m",
            }))
            dummy_artifact = root / "evidence.json"
            dummy_artifact.write_text(json.dumps({"valid": True}))
            machine_sha = hashlib.sha256(machine_artifact.read_bytes()).hexdigest()
            dummy_sha = hashlib.sha256(dummy_artifact.read_bytes()).hexdigest()
            evidence_names = {
                "machine", "memory_budget", "os_cache_model",
                "buffer_probe_overhead", "tp_sweep", "ap_model_bundle",
                "fio_validation", "service_calibration", "tp_calibration",
                "tp_collection", "tp_trace", "transaction_evidence",
            }
            for benchmark in ("sysbench", "benchbase-tpcc"):
                for stage in stages:
                    key = benchmark + "-" + stage.name
                    model_path = root / (key + ".json")
                    config_path = root / (key + "-config.json")
                    config_path.write_text(json.dumps({
                        "machine_fingerprint": "m", "tp_benchmark": benchmark,
                        "stage": {
                            "tp_terminals": stage.tp_terminals,
                            "tp_baseline_terminals": stage.tp_baseline_terminals,
                            "tp_surge_terminals": stage.tp_surge_terminals,
                        },
                    }))
                    evidence_artifacts = {
                        name: {
                            "path": str(
                                machine_artifact if name == "machine"
                                else dummy_artifact
                            ),
                            "sha256": machine_sha if name == "machine" else dummy_sha,
                        } for name in evidence_names
                    }
                    model_path.write_text(json.dumps({
                        "schema": "huawei7.ppt-architecture-result/v2",
                        "machine_fingerprint": "m", "tp_benchmark": benchmark,
                        "dataset_fingerprint": "d" * 64,
                        "tp_terminals": stage.tp_terminals,
                        "tp_baseline_terminals": stage.tp_baseline_terminals,
                        "tp_surge_terminals": stage.tp_surge_terminals,
                        "tp_surge_start_phase": (
                            "measurement" if stage.tp_surge_terminals else None
                        ),
                        "pipeline_config_artifact": {
                            "path": str(config_path),
                            "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
                        },
                        "evidence_artifacts": evidence_artifacts,
                        "evidence_sha256": {
                            name: row["sha256"]
                            for name, row in evidence_artifacts.items()
                        },
                        "ap_query_sha256": {
                            str(query): hashlib.sha256(
                                ("q%d" % query).encode()
                            ).hexdigest()
                            for query in stage.ap_queries
                        },
                    }))
                    model_paths[key] = model_path
            path.write_text(json.dumps({
                "schema": "huawei7.five-stage-recommendations/v3",
                "machine_fingerprint": "m",
                "dataset_fingerprint": "d" * 64,
                "benchmarks": ["sysbench", "benchbase-tpcc"],
                "selection_frozen_before_real_stage_measurements": True,
                "stages": [{
                    "benchmark": benchmark, "stage": stage.name,
                    "tp_terminals": stage.tp_terminals,
                    "tp_baseline_terminals": stage.tp_baseline_terminals,
                    "tp_surge_terminals": stage.tp_surge_terminals,
                    "tp_surge_start_phase": (
                        "measurement" if stage.tp_surge_terminals else None
                    ),
                    "shared_buffers_mb": 4096,
                    "predicted_tps": 100,
                    "work_mem_by_query": {str(query): 64 for query in stage.ap_queries},
                    "model_result": str(model_paths[benchmark + "-" + stage.name]),
                    "model_result_sha256": hashlib.sha256(
                        model_paths[benchmark + "-" + stage.name].read_bytes()
                    ).hexdigest(),
                    "dataset_fingerprint": "d" * 64,
                    "query_sha256": {
                        str(query): hashlib.sha256(
                            ("q%d" % query).encode()
                        ).hexdigest()
                        for query in stage.ap_queries
                    },
                } for benchmark in ("sysbench", "benchbase-tpcc") for stage in stages],
            }))
            recommendations = read_recommendations(path, stages, "m")
            self.assertEqual(tuple(dict(recommendations[("sysbench", "S4")].work_mem_by_query)),
                             (2, 9, 13, 18, 21))
            original_recommendations = path.read_text()
            invalid = json.loads(original_recommendations)
            invalid["stages"][0]["model_result_sha256"] = "z" * 64
            path.write_text(json.dumps(invalid))
            with self.assertRaises(ValueError):
                read_recommendations(path, stages, "m")
            path.write_text(original_recommendations)
            dummy_artifact.write_text(json.dumps({"valid": False}))
            with self.assertRaisesRegex(ValueError, "evidence.*changed"):
                read_recommendations(path, stages, "m")
        config = {
            "postgres": {"host": "127.0.0.1", "port": 5432},
            "tp": {
                "sysbench": {
                    "binary": "/usr/bin/sysbench", "script": "oltp.lua",
                    "database": "sb", "user": "sys_user",
                    "password_env": "SYS_PASSWORD",
                    "tables": 16, "table_size": 4_000_000,
                },
                "benchbase-tpcc": {
                    "database": "tpcc", "user": "tpcc_user",
                    "password_env": "TPCC_PASSWORD",
                    "batch_size": 128, "warehouses": 125,
                },
            },
        }
        baseline_command = sysbench_command(config, terminals=128, total_seconds=90)
        surge_command = sysbench_command(config, terminals=16, total_seconds=60)
        secret_command = sysbench_command(
            config, terminals=128, total_seconds=90,
            config_file=Path("/dev/shm/sysbench-secret.cfg"),
        )
        self.assertIn("--threads=128", baseline_command)
        self.assertIn("--threads=16", surge_command)
        self.assertIn("--pgsql-user=sys_user", baseline_command)
        self.assertIn(
            "--config-file=/dev/shm/sysbench-secret.cfg", secret_command,
        )
        self.assertNotIn("--config-file", " ".join(baseline_command))
        self.assertEqual(tp_connection(config, "sysbench")["password_env"],
                         "SYS_PASSWORD")
        self.assertEqual(tp_connection(config, "benchbase-tpcc")["password_env"],
                         "TPCC_PASSWORD")
        peer_config = json.loads(json.dumps(config))
        peer_config["postgres"].update({
            "host": "/tmp", "local_peer_os_user": "omm",
        })
        peer_config["tp"]["sysbench"]["user"] = "omm"
        peer_command = sysbench_command(
            peer_config, terminals=1, total_seconds=10,
        )
        self.assertEqual(
            peer_command[:4], ("/usr/sbin/runuser", "-u", "omm", "--"),
        )
        peer_config["postgres"]["host"] = "127.0.0.1"
        with self.assertRaisesRegex(ValueError, "local peer"):
            sysbench_command(peer_config, terminals=1, total_seconds=10)
        xml = benchbase_xml(config, terminals=128, warmup_seconds=30,
                            measure_seconds=60, password="p&x")
        surge_xml = benchbase_xml(config, terminals=16, warmup_seconds=0,
                                  measure_seconds=60, password="p&x")
        self.assertIn("<terminals>128</terminals>", xml)
        self.assertIn("<terminals>16</terminals>", surge_xml)
        self.assertIn("<username>tpcc_user</username>", xml)
        self.assertIn("p&amp;x", xml)
        with tempfile.TemporaryDirectory() as query_directory:
            query = Path(query_directory) / "q.sql"
            query.write_text("select 1;")
            ap_command = ap_gsql_command(
                {"postgres": {
                    "gsql": "gsql", "ap_user": "u", "ap_database": "ap",
                    "ap_password_env": "AP_PASSWORD",
                    "ld_library_path": "/opt/lib",
                }}, query_file=query, work_mem_mb=64,
                application_name="ppt5_ap_test",
            )
            self.assertIn("SET enable_vector_engine=off", ap_command[-1])
            self.assertEqual(
                Path(ap_command[1]).name, "run_gsql_with_password.py"
            )
            analyzed = ap_gsql_command(
                {"postgres": {
                    "gsql": "gsql", "ap_user": "u", "ap_database": "ap",
                    "ap_password_env": "AP_PASSWORD",
                    "ld_library_path": "/opt/lib",
                }}, query_file=query, work_mem_mb=64,
                application_name="ppt5_ap_calibration",
                explain_analyze=True,
            )
            self.assertIn(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)", analyzed[-1],
            )
            self.assertIn("-At", analyzed)
        tps, samples = parse_sysbench_tps(
            "[ 30s ] thds: 1 tps: 10.0\n[ 31s ] thds: 1 tps: 20.0\n", 30,
        )
        self.assertEqual((tps, samples), (20.0, 1))

    def test_stage_raw_log_hash_is_reverified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tp.log"
            path.write_text("real output\n")
            dataset = Path(directory) / "dataset.json"
            dataset.write_text(json.dumps({"dataset_fingerprint": "d" * 64}))
            summary = {
                "executor": "row; enable_vector_engine=off",
                "query_dop": 1,
                "stage": "S1", "tp_terminals": 128,
                "dataset_fingerprint": "d" * 64,
                "tp_baseline_terminals": 128, "tp_surge_terminals": 0,
                "tp_surge_start_phase": None,
                "instrumentation_output_during_measurement": {
                    "filesystem": "tmpfs", "mountpoint": "/dev/shm",
                    "promoted_after_workload_stopped": True,
                },
                "input_artifacts": {
                    name: {
                        "path": str(dataset if name == "dataset_audit" else path),
                        "sha256": hashlib.sha256(
                            (dataset if name == "dataset_audit" else path).read_bytes()
                        ).hexdigest(),
                    } for name in (
                        "stage_spec", "recommendations", "runtime_config",
                        "dataset_audit",
                    )
                },
                "raw_evidence": [{
                    "kind": "tp_driver_log", "role": "baseline",
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }],
            }
            validate_stage_raw_evidence(summary)
            path.write_text("changed\n")
            with self.assertRaisesRegex(RuntimeError, "missing or changed"):
                validate_stage_raw_evidence(summary)

    def test_s5_cannot_collapse_surge_into_one_144_terminal_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tp.log"
            path.write_text("real output\n")
            dataset = Path(directory) / "dataset.json"
            dataset.write_text(json.dumps({"dataset_fingerprint": "d" * 64}))
            summary = {
                "executor": "row; enable_vector_engine=off", "query_dop": 1,
                "dataset_fingerprint": "d" * 64,
                "stage": "S5", "tp_terminals": 144,
                "tp_baseline_terminals": 144, "tp_surge_terminals": 0,
                "tp_surge_start_phase": None,
                "input_artifacts": {
                    name: {
                        "path": str(dataset if name == "dataset_audit" else path),
                        "sha256": hashlib.sha256(
                            (dataset if name == "dataset_audit" else path).read_bytes()
                        ).hexdigest(),
                    } for name in (
                        "stage_spec", "recommendations", "runtime_config",
                        "dataset_audit",
                    )
                },
                "raw_evidence": [{
                    "kind": "tp_driver_log", "role": "baseline",
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }],
            }
            with self.assertRaisesRegex(RuntimeError, "S5|topology"):
                validate_stage_raw_evidence(summary)


if __name__ == "__main__":
    unittest.main()
