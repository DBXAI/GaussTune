import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_ppt_pipeline import audit


class PptPipelineAuditTest(unittest.TestCase):
    def test_target_audit_is_fail_closed_without_strict_manifest(self):
        root = Path(__file__).resolve().parents[1]
        # The checked-in diagnostic may not be present in a minimal source
        # checkout; the audit's target boundary is tested with temporary docs.
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            target_path = work / "target.json"
            trace_path = work / "trace.json"
            target_path.write_text(json.dumps({
                "schema": "huawei7.target-config-diagnostic/v1",
                "valid": True,
                "configuration": {
                    "shared_buffers_mb": 512,
                    "work_mem_mb": 32,
                },
                "model_status": "out_of_domain_for_current_v3_native_sweep",
                "dataset_protocol": {"tpcc_reset_performed": False},
            }), encoding="utf-8")
            trace_path.write_text(json.dumps({
                "schema": "huawei7.ppt-trace-collection-diagnostic/v1",
                "valid": False,
                "configuration": {
                    "shared_buffers_mb": 512,
                    "work_mem_mb": 32,
                },
                "replay_validation": {
                    "mismatch_fraction": 0.0105,
                    "valid": False,
                },
            }), encoding="utf-8")
            report = audit(
                stage_spec=root / "config/ppt_five_stages.json",
                artifact_manifest=None,
                target_diagnostic=target_path,
                trace_diagnostic=trace_path,
                shared_buffers_mb=512,
                work_mem_mb=32,
            )
        self.assertTrue(report["code_chain"]["valid"])
        self.assertTrue(report["stage_contract"]["valid"])
        self.assertTrue(report["target"]["baseline_native"]["valid"])
        self.assertFalse(report["target"]["strict_trace_diagnostic"]["valid"])
        self.assertTrue(report["baseline_comparison_ready"])
        self.assertFalse(report["strict_ready"])


if __name__ == "__main__":
    unittest.main()
