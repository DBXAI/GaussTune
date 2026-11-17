import unittest
from unittest.mock import patch
from types import SimpleNamespace

from jmt.innovation_4_performance.boundary_observer import BoundaryObserver, pair_events, resolve_symbols


class BoundaryObserverTests(unittest.TestCase):
    def event(self, t, kind, returned, tid=5, cpu=None):
        return {"mono_ns": t, "scheduled_cpu_ns": t if cpu is None else cpu,
                "pointer": 0 if returned else 123, "tid": tid, "kind": kind, "exit": returned}

    def test_pairs_separate_stacks_by_thread_and_kind(self):
        events = [self.event(1, 0, 0), self.event(2, 1, 0),
                  self.event(3, 1, 1), self.event(4, 0, 1)]
        pairs = pair_events(events)
        self.assertEqual([p["kind"] for p in pairs], ["Hash build", "ExecutorRun"])
        self.assertEqual(pairs[-1]["pointer"], 123)
        self.assertTrue(pairs[-1]["CPU_not_exact_boundary_runtime_current_timeslice_not_flushed"])

    def test_missing_entry_or_exit_rejected(self):
        for events in ([self.event(1, 0, 0)], [self.event(1, 0, 1)]):
            with self.assertRaises(ValueError):
                pair_events(events)

    def test_runtime_backwards_rejected(self):
        with self.assertRaises(ValueError):
            pair_events([self.event(1, 0, 0, cpu=10), self.event(2, 0, 1, cpu=9)])

    def test_symbol_resolution_ignores_const_func_name_not_just_substring(self):
        output = "\n".join([
            "0001 T _Z20standard_ExecutorRunP9QueryDesc13ScanDirectionl",
            "0002 T _Z13MultiExecHashP9HashState",
            "0003 T _Z21tuplesort_performsortP14Tuplesortstate",
            "0004 r _ZZ21tuplesort_performsortP14TuplesortstateE8__func__"])
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=output)):
            result = resolve_symbols("ignored")
        self.assertEqual(result["Sort"], "_Z21tuplesort_performsortP14Tuplesortstate")

    def test_stripped_binary_does_not_enable_observer(self):
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="")):
            with self.assertRaises(ValueError):
                resolve_symbols("ignored")

    def test_stale_array_slot_after_reset_not_used_for_task_identity(self):
        class Table(dict):
            Key = int
        e = SimpleNamespace(mono_ns=99, scheduled_cpu_ns=1, pointer=2, tid=5, kind=0, exit=0)
        observer = BoundaryObserver.__new__(BoundaryObserver)
        observer.reset_ns = 100
        observer.bpf = {"count": Table({0: [1, 0]}), "events": Table({0: [e, e]})}
        self.assertEqual(observer.snapshot(), [])
        e.mono_ns = 101
        self.assertEqual(observer.snapshot()[0]["tid"], 5)


if __name__ == "__main__":
    unittest.main()
