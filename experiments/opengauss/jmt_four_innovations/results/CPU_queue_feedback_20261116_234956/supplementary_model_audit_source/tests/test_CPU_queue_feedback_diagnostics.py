from copy import deepcopy
import unittest
from audit_CPU_queue_feedback import runnable_diagnostic


class RunnableAuditTests(unittest.TestCase):
    def fixture(self):
        before = {
            "started_ns":1_000_000_000,"finished_ns":1_010_000_000,
            "backend_runqueue_wait_ns":{str(i):0 for i in range(128)},
            "backend_timeslices":{str(i):0 for i in range(128)},
            "AP_task_diagnostics":{"999":{
                "identity":[100,999,7],"nice":0,"CPU_ns":0,"runqueue_wait_ns":0,
                "sample_ns":1_005_000_000,"read_span_ns":1_000_000,
                "io_submissions_not_completions":{
                    "rchar":0,"wchar":0,"read_bytes":0,"write_bytes":0,"cancelled_write_bytes":0}}}}
        after = deepcopy(before)
        after.update(started_ns=21_000_000_000,finished_ns=21_010_000_000)
        after["backend_runqueue_wait_ns"] = {str(i):16_000_000_000 for i in range(128)}
        after["backend_timeslices"] = {str(i):100 for i in range(128)}
        after["AP_task_diagnostics"]["999"].update(
            CPU_ns=2_000_000_000,runqueue_wait_ns=16_000_000_000,sample_ns=21_005_000_000)
        raw = {"gaussdb_pid":100,"backend_linux_tids":list(range(128)),
               "CPU_bracket":{"before":before,"after":after}}
        starts = {"9":{"identity":{"linux_tid":999}}}
        return raw,starts

    def test_runqueue_is_not_IO_stall(self):
        result = runnable_diagnostic(*self.fixture())
        self.assertAlmostEqual(result["AP"]["9"]["CPU_cores"],.1)
        self.assertAlmostEqual(result["AP"]["9"]["runqueue_wait_fraction"],.8)
        self.assertAlmostEqual(result["TP_mean_runqueue_fraction_per_backend"],.8)
        self.assertFalse(result["exact_disjoint_kernel_states_or_IO_ownership_qualified"])

    def test_task_or_process_identity_change_rejected(self):
        for identity in ([100,998,7],[100,999,8],[101,999,7]):
            raw,starts = self.fixture()
            raw["CPU_bracket"]["after"]["AP_task_diagnostics"]["999"]["identity"] = identity
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                runnable_diagnostic(raw,starts)

    def test_sample_outside_cpu_bracket_rejected(self):
        raw,starts = self.fixture()
        raw["CPU_bracket"]["after"]["AP_task_diagnostics"]["999"]["sample_ns"] += 20_000_000
        with self.assertRaises(ValueError):
            runnable_diagnostic(raw,starts)

    def test_long_read_rejected(self):
        raw,starts = self.fixture()
        raw["CPU_bracket"]["after"]["AP_task_diagnostics"]["999"]["read_span_ns"] = 100_000_000
        with self.assertRaises(ValueError):
            runnable_diagnostic(raw,starts)

    def test_negative_tp_queue_or_slices_rejected(self):
        for field in ("backend_runqueue_wait_ns","backend_timeslices"):
            raw,starts = self.fixture()
            raw["CPU_bracket"]["after"][field]["0"] = -1
            with self.subTest(field=field), self.assertRaises(ValueError):
                runnable_diagnostic(raw,starts)

    def test_negative_io_submissions_rejected(self):
        raw,starts = self.fixture()
        raw["CPU_bracket"]["after"]["AP_task_diagnostics"]["999"][
            "io_submissions_not_completions"]["write_bytes"] = -1
        with self.assertRaises(ValueError):
            runnable_diagnostic(raw,starts)

    def test_zero_write_is_not_full_query_no_spill_claim(self):
        result = runnable_diagnostic(*self.fixture())
        self.assertEqual(result["AP"]["9"]["IO_submission_counter_deltas_not_spill_completions"][
            "write_bytes"],0)
        self.assertTrue(result["AP"]["9"]["syscall_write_characters_are_not_query_owned_spill_bytes"])
