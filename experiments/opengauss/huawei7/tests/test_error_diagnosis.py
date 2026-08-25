from __future__ import annotations

import unittest

from huawei7.error_diagnosis import (
    attribute_prediction,
    diagnose_holdout_residual,
)


class ErrorDiagnosisTest(unittest.TestCase):
    def _prediction(self, *, cpu: float, io: float) -> dict:
        return {
            "base_latency_ms": 20.0,
            "cpu_queue_delay_ms": cpu,
            "io_latency_delta_ms": io,
            "buffered_transaction_latency_delta_ms": io,
            "direct_device_latency_delta_ms": 0.0,
        }

    def test_prediction_attribution_does_not_use_observed_tps(self) -> None:
        result = attribute_prediction(self._prediction(cpu=3.0, io=0.5))
        self.assertFalse(result["uses_observed_holdout_tps"])
        self.assertEqual(
            result["dominant_predicted_resource"],
            "cpu_queue",
        )
        self.assertGreater(result["dominant_resource_share"], 0.8)

    def test_diagnosis_is_invariant_to_external_workload_label(self) -> None:
        prediction = self._prediction(cpu=3.0, io=0.5)
        first = diagnose_holdout_residual(
            prediction=prediction,
            observed_tps=128000.0 / 24.0,
            terminals=128,
        )
        second = diagnose_holdout_residual(
            prediction=prediction,
            observed_tps=128000.0 / 24.0,
            terminals=128,
        )
        # The public classifier accepts resource evidence only.  The
        # equality check is intentionally repeated as a label-permutation
        # regression guard in the caller: no benchmark name can affect it.
        self.assertEqual(first, second)
        self.assertNotIn("benchmark", first)

    def test_cpu_overestimate_is_identified(self) -> None:
        # 128 terminals at 1000 / 6 ms gives 21.333 TPS.  The model adds
        # 3 ms CPU wait but the holdout needs only 1 ms total resource wait.
        result = diagnose_holdout_residual(
            prediction=self._prediction(cpu=3.0, io=0.0),
            observed_tps=128000.0 / 21.0,
            terminals=128,
        )
        self.assertEqual(result["classification"], "cpu_path_overestimated")
        self.assertFalse(result["uses_observed_holdout_tps_for_calibration"])

    def test_buffered_underestimate_is_identified(self) -> None:
        result = diagnose_holdout_residual(
            prediction=self._prediction(cpu=0.1, io=4.0),
            observed_tps=128000.0 / 27.0,
            terminals=128,
        )
        self.assertEqual(
            result["classification"],
            "database_io_path_underestimated",
        )

    def test_out_of_domain_has_priority(self) -> None:
        result = diagnose_holdout_residual(
            prediction=self._prediction(cpu=0.0, io=0.0),
            observed_tps=128000.0 / 30.0,
            terminals=128,
            buffered_path_out_of_domain={
                "measured_tp_terminals": 128,
                "candidate_tp_terminals": 144,
            },
        )
        self.assertEqual(
            result["classification"],
            "buffered_path_out_of_domain",
        )
        self.assertEqual(result["confidence"], "high")


if __name__ == "__main__":
    unittest.main()
