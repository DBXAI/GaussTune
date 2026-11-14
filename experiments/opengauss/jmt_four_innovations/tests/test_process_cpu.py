from pathlib import Path
import tempfile
import unittest

from jmt.common.process_cpu import snapshot_process_trees, process_tree_cpu_delta


def task(proc, pid, tid, cpu, children="", birth=1, comm="worker (name)"):
    base = proc / str(pid)
    base.mkdir(exist_ok=True)
    (base / "comm").write_text(comm)
    stat = "%d (%s) " % (pid, comm) + " ".join(["S"] + ["0"] * 18 + [str(birth)]) + "\n"
    (base / "stat").write_text(stat)
    t = base / "task" / str(tid)
    t.mkdir(parents=True)
    (t / "stat").write_text(stat)
    (t / "schedstat").write_text("%d 0 0\n" % cpu)
    (t / "children").write_text(children)


class ProcessCPUAccountingTests(unittest.TestCase):
    def test_counts_sysbench_child_workers_not_just_runuser_wrapper(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            task(proc, 1, 1, 10, "2", comm="runuser")
            task(proc, 2, 2, 100, comm="sysbench")
            task(proc, 2, 3, 200, comm="sysbench")
            result = snapshot_process_trees(1, proc)
            self.assertEqual(result["total_cpu_ns"], 310)
            self.assertEqual([r["thread_count"] for r in result["processes"]], [1, 2])

    def test_all_thread_children_followed_and_overlap_roots_deduplicated(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            task(proc, 1, 1, 10)
            task(proc, 1, 4, 20, "2")
            task(proc, 2, 2, 30)
            self.assertEqual(snapshot_process_trees([1, 2], proc)["total_cpu_ns"], 60)

    def test_delta_and_identity_membership(self):
        before = {"root_pids": [1], "thread_cpu_ns": {"1:1:1:1": 100}}
        after = {"root_pids": [1], "thread_cpu_ns": {"1:1:1:1": 1_000_000_100}}
        self.assertEqual(process_tree_cpu_delta(before, after), 1)

    def test_reused_PID_not_subtracted_as_same_thread(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            process_tree_cpu_delta(
                {"root_pids": [1], "thread_cpu_ns": {"1:1:1:1": 100}},
                {"root_pids": [1], "thread_cpu_ns": {"1:2:1:2": 200}})

    def test_root_topology_change_rejected(self):
        with self.assertRaisesRegex(ValueError, "roots"):
            process_tree_cpu_delta({"root_pids": [1]}, {"root_pids": [1, 2]})

    def test_counter_regression_rejected(self):
        with self.assertRaisesRegex(ValueError, "decreased"):
            process_tree_cpu_delta(
                {"root_pids": [1], "thread_cpu_ns": {"x": 100}},
                {"root_pids": [1], "thread_cpu_ns": {"x": 99}})

    def test_missing_descendant_rejected_not_omitted(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            task(proc, 1, 1, 10, "999")
            with self.assertRaises(FileNotFoundError):
                snapshot_process_trees(1, proc)

    def test_empty_roots_rejected(self):
        with self.assertRaisesRegex(ValueError, "roots"):
            snapshot_process_trees([])


if __name__ == "__main__":
    unittest.main()
