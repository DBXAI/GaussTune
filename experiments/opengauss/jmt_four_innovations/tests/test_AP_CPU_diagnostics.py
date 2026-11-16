import copy
import unittest

from diagnose_AP_runnable import summarize
from audit_AP_CPU_interference import validate_TP_CPU_sum


class CPUChainDiagnosticsTest(unittest.TestCase):
    def test_queue_wait_not_mislabeled_IO_blocking(self):
        before = {"identity": [1, 2, 3], "nice": 0, "CPU_ns": 0,
                  "runqueue_wait_ns": 0, "sample_ns": 1_000_000_000}
        after = dict(before, CPU_ns=400_000_000, runqueue_wait_ns=3_200_000_000,
                     sample_ns=5_000_000_000)
        result = summarize(before, after)
        self.assertAlmostEqual(result["CPU_cores"], .1)
        self.assertAlmostEqual(result["runqueue_wait_fraction"], .8)
        self.assertAlmostEqual(result["other_or_blocked_fraction_approx"], .1)
        self.assertTrue(result["not_exact_disjoint_kernel_state_accounting"])

    def test_diagnostic_identity_or_priority_changes_rejected(self):
        before = {"identity": [1, 2, 3], "nice": 0, "CPU_ns": 0,
                  "runqueue_wait_ns": 0, "sample_ns": 1_000_000_000}
        for update in ({"identity": [1, 2, 4]}, {"nice": 1},
                       {"runqueue_wait_ns": -1}, {"sample_ns": 0}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                summarize(before, dict(before, **{"sample_ns": 2_000_000_000, **update}))

    def test_TP_CPU_does_not_include_AP_and_raw_sum_is_not_blindly_trusted(self):
        raw = {"backend_linux_tids": [1, 2], "TP_CPU_seconds": 2.,
               "CPU_bracket": {
                   "before": {"backend_cpu_ns": {"1": 0, "2": 0, "99": 0}},
                   "after": {"backend_cpu_ns": {"1": 1_000_000_000, "2": 1_000_000_000,
                                               "99": 4_000_000_000}}}}
        validate_TP_CPU_sum(raw)
        wrong = copy.deepcopy(raw)
        wrong["TP_CPU_seconds"] = 6.
        with self.assertRaises(ValueError):
            validate_TP_CPU_sum(wrong)


if __name__ == "__main__":
    unittest.main()
