import unittest
from unittest.mock import patch

from scripts import run_tp_calibration_matrix as matrix


def metrics(**overrides):
    value = {
        "sustainable_tps": 100.0,
        "shared_buffer_hit_ratio": .95,
        "buffer_accesses_per_tx": 200.0,
        "physical_read_requests_per_tx": 2.0,
        "physical_read_bytes_per_tx": 16384.0,
    }
    value.update(overrides)
    return value


class TpMatrixPreconditioningTest(unittest.TestCase):
    def converged(self, rows):
        with patch.object(matrix, "_tp_response_metrics", side_effect=lambda x: x):
            return matrix._precondition_converged(
                rows, maximum_span=.20, maximum_hit_span=.02,
            )

    def test_requires_three_recent_runs_and_accepts_all_stable_metrics(self):
        self.assertFalse(self.converged([metrics(), metrics()]))
        self.assertTrue(self.converged([
            metrics(sustainable_tps=91, shared_buffer_hit_ratio=.94),
            metrics(sustainable_tps=100, shared_buffer_hit_ratio=.95),
            metrics(sustainable_tps=109, shared_buffer_hit_ratio=.96),
        ]))

    def test_rejects_if_any_physical_metric_is_unstable(self):
        self.assertFalse(self.converged([
            metrics(physical_read_bytes_per_tx=12000),
            metrics(physical_read_bytes_per_tx=16000),
            metrics(physical_read_bytes_per_tx=20000),
        ]))

    def test_uses_only_declared_recent_window(self):
        self.assertTrue(self.converged([
            metrics(sustainable_tps=1),
            metrics(sustainable_tps=100),
            metrics(sustainable_tps=101),
            metrics(sustainable_tps=99),
        ]))

    def test_rejects_hit_ratio_span_above_absolute_gate(self):
        self.assertFalse(self.converged([
            metrics(shared_buffer_hit_ratio=.92),
            metrics(shared_buffer_hit_ratio=.95),
            metrics(shared_buffer_hit_ratio=.96),
        ]))


if __name__ == "__main__":
    unittest.main()
