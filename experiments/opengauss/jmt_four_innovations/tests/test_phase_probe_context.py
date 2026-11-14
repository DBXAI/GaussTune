import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import measure_current_mixed_five_stages as mixed
import validate_capacity_TPS_live as live


class PhaseProbeContextTests(unittest.TestCase):
    def test_probe_hook_runs_with_observer_attached_and_closes_on_failure(self):
        events = []

        class StopProbe(Exception):
            pass

        class Observer:
            def __init__(self, *args):
                self.bpf, self.interval = object(), None
                events.append("attached")

            def close(self):
                events.append("closed")

        run = mixed.Run.__new__(mixed.Run)
        run.sb = SimpleNamespace(pid=1)

        def hook(stage, repeat, expected, gate, observer):
            self.assertEqual((stage, repeat, expected, gate), ("S1", 1, [18], False))
            self.assertIsNotNone(observer.bpf)
            self.assertIsNone(observer.interval)
            events.append("probe")
            raise StopProbe()

        run.before_scored_window = hook
        with patch.object(mixed, "TPWindowObserver", Observer), self.assertRaises(StopProbe):
            run.window("S1", 1, [18])
        self.assertEqual(events, ["attached", "probe", "closed"])

    def test_no_phase_model_does_not_start_extra_probes(self):
        run = live.DiagnosticRun.__new__(live.DiagnosticRun)
        run.args = SimpleNamespace(phase_model=None)
        run.before_scored_window("S1", 1, [18], False, None)


if __name__ == "__main__":
    unittest.main()
