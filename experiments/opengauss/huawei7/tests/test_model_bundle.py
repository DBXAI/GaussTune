import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from huawei7.model_bundle import (
    _request_observation_interval, _require_blind_explain, build_model_bundle,
)
from huawei7.operator_model import parse_explain, plan_family, walk_plan
from huawei7.plan_switch import build_plan_switch_evidence


class ModelBundleTest(unittest.TestCase):
    def test_request_interval_excludes_unmodeled_direction_background(self):
        delta = {"samples": [{
            "read_requests_delta": read,
            "write_requests_delta": write,
        } for read, write in ((10, 1000), (20, 2000), (-5, 3000))]}
        lower, upper, values = _request_observation_interval(
            delta, 15.0, ("read",),
        )
        self.assertEqual(values, (10.0, 20.0, 0.0))
        self.assertEqual((lower, upper), (0.0, 20.0))

    def test_candidate_explain_cannot_contain_actual_outcome(self):
        with self.assertRaises(ValueError):
            _require_blind_explain({"Plan": {"Actual Rows": 10}})

    def test_training_holdout_and_candidate_are_disjoint_and_versioned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text("{}\n")
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            query = root / "q18.sql"
            query.write_text("select 1;\n")
            query_sha = hashlib.sha256(query.read_bytes()).hexdigest()

            base_plan = {"Plan": {
                "Node Type": "Aggregate", "Strategy": "Hashed",
                "Plan Rows": 100, "Plan Width": 16,
                "Plans": [{
                    "Node Type": "Sort", "Plan Rows": 1000, "Plan Width": 32,
                    "Plans": [{"Node Type": "Seq Scan", "Plan Rows": 1000,
                               "Plan Width": 32, "Relation Name": "t"}],
                }],
            }}
            parsed = parse_explain([base_plan])
            family = plan_family(parsed)
            width_rows = []
            sample_sql = "select avg(pg_column_size(row(1))), 30"
            sample_sql_sha = hashlib.sha256(sample_sql.encode()).hexdigest()
            for node in walk_plan(parsed):
                width_rows.append({
                    "node_signature": node.signature, "plan_family": family,
                    "plan_width": max(1, node.plan_width),
                    "actual_width": max(1, node.plan_width),
                    "method": "pg_column_size", "sample_count": 30,
                    "source_sha256": sample_sql_sha,
                    "sample_sql": sample_sql,
                    "query_id": "18", "query_sha256": query_sha,
                    "query_dop": 1,
                })
            widths = root / "widths.json"
            widths.write_text(json.dumps({
                "schema": "huawei7.width-anchors/v1",
                "machine_fingerprint": "machine", "anchors": width_rows,
            }))

            def write_run(name, runtime_ms):
                document = [{
                    "Plan": {
                        **base_plan["Plan"], "Actual Rows": 100,
                        "Actual Total Time": runtime_ms,
                        "Plans": [{
                            **base_plan["Plan"]["Plans"][0],
                            "Actual Rows": 1000, "Actual Total Time": runtime_ms * .8,
                            "Plans": [{
                                **base_plan["Plan"]["Plans"][0]["Plans"][0],
                                "Actual Rows": 1000, "Actual Total Time": runtime_ms * .6,
                                "Shared Hit Blocks": 60, "Shared Read Blocks": 40,
                                "Shared Dirtied Blocks": 0,
                            }],
                        }],
                    },
                    "Total Runtime": runtime_ms,
                }]
                explain = root / (name + ".json")
                explain.write_text(json.dumps(document))
                explain_sha = hashlib.sha256(explain.read_bytes()).hexdigest()
                collection = root / (name + ".collection.json")
                collection.write_text(json.dumps({
                    "schema": "huawei7.explain-collection/v1",
                    "machine_fingerprint": "machine", "query_id": "18",
                    "work_mem_mb": 1, "query_sha256": query_sha,
                    "explain_sha256": explain_sha,
                    "executor": "row; enable_vector_engine=off",
                    "query_dop": 1,
                    "valid": True,
                }))
                delta = root / (name + ".delta.json")
                command = root / (name + ".command.json")
                command.write_text(json.dumps({
                    "schema": "huawei7.ap-command/v1",
                    "machine_fingerprint": "machine", "query_id": "18",
                    "query_sha256": query_sha, "work_mem_mb": 1,
                    "executor": "row; enable_vector_engine=off",
                    "query_dop": 1,
                    "argv": ["gsql"],
                }))
                delta.write_text(json.dumps({
                    "schema": "huawei7.isolated-device-delta/v1",
                    "machine_fingerprint": "machine", "valid": True,
                    "repeats": 3, "median_read_requests": 25,
                    "median_write_requests": 0,
                    "query_id": "18", "query_sha256": query_sha,
                    "work_mem_mb": 1, "plan_family": family,
                    "executor": "row; enable_vector_engine=off",
                    "query_dop": 1,
                    "command_artifact": str(command.resolve()),
                    "command_artifact_sha256": hashlib.sha256(
                        command.read_bytes()
                    ).hexdigest(),
                }))
                return {
                    "trace_id": name, "explain_analyze": explain.name,
                    "explain_collection": collection.name, "query_id": 18,
                    "device_delta": delta.name, "work_mem_mb": 1, "dop": 1,
                }

            training = [write_run("train-%02d" % index, 100 + index)
                        for index in range(9)]
            holdout = [write_run("hold-%02d" % index, 105 + index)
                       for index in range(3)]
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps([base_plan]))
            candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
            candidate_collection = root / "candidate.collection.json"
            candidate_collection.write_text(json.dumps({
                "schema": "huawei7.blind-explain-collection/v1",
                "machine_fingerprint": "machine", "query_id": "18",
                "work_mem_mb": 1, "query_sha256": query_sha,
                "explain_sha256": candidate_sha, "blind": True,
                "executor": "row; enable_vector_engine=off", "valid": True,
                "query_dop": 1,
            }))
            switch_manifest = {
                "schema": "huawei7.plan-switch-manifest/v1",
                "machine_fingerprint": "machine", "query_id": 18,
                "minimum_mb": 1, "maximum_mb": 1, "grid_mb": 1,
                "plans": [{
                    "work_mem_mb": 1, "explain": candidate.name,
                    "collection": candidate_collection.name,
                }],
            }
            switch_evidence = root / "switch.json"
            switch_evidence.write_text(json.dumps(
                build_plan_switch_evidence(switch_manifest, root)
            ))
            manifest = {
                "schema": "huawei7.ap-calibration-manifest/v1",
                "machine_fingerprint": "machine",
                "source_manifest": source.name,
                "source_manifest_sha256": source_sha,
                "query_files": {"18": query.name},
                "width_evidence": widths.name,
                "training_runs": training, "holdout_runs": holdout,
                "maximum_runtime_mape": .2, "maximum_request_mape": .2,
                "work_mem_search": {
                    "18": {"minimum_mb": 1, "maximum_mb": 1, "grid_mb": 1,
                           "plan_switch_evidence": switch_evidence.name},
                },
                "candidate_plans": [{
                    "query_id": 18, "work_mem_mb": 1,
                    "explain": candidate.name,
                }],
            }
            result = build_model_bundle(manifest, root)
            self.assertTrue(result["valid"])
            self.assertEqual(result["schema"], "huawei7.ap-model-bundle/v1")
            self.assertEqual(len(result["model_bundle_id"]), 64)
            self.assertIsNone(result["dataset_fingerprint"])
            self.assertEqual(len(result["query_options"]["18"]), 1)
            self.assertEqual(
                result["work_mem_candidate_contract"]["18"]
                ["required_candidates_mb"], [1],
            )
            self.assertEqual(
                result["query_options"]["18"][0]["evidence"]["model_bundle_id"],
                result["model_bundle_id"],
            )
            query.write_text("select 2;\n")
            with self.assertRaisesRegex(ValueError, "declared AP query|bind this AP run"):
                build_model_bundle(manifest, root)
            query.write_text("select 1;\n")
            candidate.write_text(json.dumps([base_plan], indent=2))
            with self.assertRaisesRegex(
                ValueError, "underlying evidence|blind plan-switch grid row",
            ):
                build_model_bundle(manifest, root)


if __name__ == "__main__":
    unittest.main()
