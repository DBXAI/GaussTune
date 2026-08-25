import json
from pathlib import Path
import tempfile
import unittest

from huawei7.joint_contention import (
    build_joint_contention_document, validate_joint_contention_evidence,
)
from huawei7.provenance import sha256


class JointContentionTest(unittest.TestCase):
    def test_complete_matrix_recomputes_and_raw_tampering_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            machine = "m" * 64
            dataset = "d" * 64
            recommendations = root / "recommendations.json"
            recommendations.write_text(json.dumps({
                "machine_fingerprint": machine,
                "dataset_fingerprint": dataset,
            }))
            episodes = []
            summary_paths = []
            order = 0
            for benchmark in ("benchbase-tpcc", "sysbench"):
                for stage_number in range(1, 6):
                    stage = "S%d" % stage_number
                    for repeat in range(1, 4):
                        order += 1
                        summary = root / (
                            "%s-%s-r%d.json" % (benchmark, stage, repeat)
                        )
                        throughput = float(100 * stage_number + repeat)
                        summary.write_text(json.dumps({
                            "schema": "huawei7.real-stage-episode/v2",
                            "valid": True,
                            "machine_fingerprint": machine,
                            "dataset_fingerprint": dataset,
                            "benchmark": benchmark,
                            "stage": stage,
                            "repeat": repeat,
                            "throughput_tps": throughput,
                            "predicted_tps": 1000.0,
                            "shared_buffers_mb": 4096,
                            "work_mem_by_query": {"18": 64},
                            "model_result_sha256": "a" * 64,
                        }))
                        summary_paths.append(summary)
                        episodes.append({
                            "order": order,
                            "benchmark": benchmark,
                            "stage": stage,
                            "repeat": repeat,
                            "throughput_tps": throughput,
                            "predicted_tps": 1000.0,
                            "summary": str(summary),
                            "summary_sha256": sha256(summary),
                        })
            validation = root / "validation.json"
            validation.write_text(json.dumps({
                "schema": "huawei7.real-five-stage-validation/v2",
                "machine_fingerprint": machine,
                "dataset_fingerprint": dataset,
                "recommendations_frozen_before_measurement": True,
                "recommendations_sha256": sha256(recommendations),
                "accuracy_valid": False,
                "stage_count": 5,
                "repeats": 3,
                "input_artifacts": {
                    "recommendations": {
                        "path": str(recommendations),
                        "sha256": sha256(recommendations),
                    },
                },
                "episodes": episodes,
            }))
            document = build_joint_contention_document(validation)
            validate_joint_contention_evidence(document)
            self.assertEqual(len(document["rows"]), 10)
            self.assertEqual(document["rows"][0]["observed_median_tps"], 102.0)
            summary_paths[0].write_text("{}")
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                validate_joint_contention_evidence(document)


if __name__ == "__main__":
    unittest.main()
