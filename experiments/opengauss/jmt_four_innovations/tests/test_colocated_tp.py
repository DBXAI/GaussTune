import unittest

from jmt.innovation_4_performance.colocated_tp import exact_tp_point, summarize_point


def window():
    return {
        "backend_linux_tids": list(range(128)), "transaction_count_rescaled": False,
        "committed_transactions": 1000, "scored_seconds": 10.,
        "shared_buffers_mib": 5120,
        "measurement_window": {"start_ns": 1_000_000_000, "end_ns": 11_000_000_000},
        "CPU_snapshot_before": {
            "started_ns": 1_000_000_000, "finished_ns": 1_010_000_000,
            "backend_cpu_ns": {str(tid): 0 for tid in range(128)},
            "driver_process_tree": {"root_pids": [2], "thread_cpu_ns": {"2:1:2:1": 0}}},
        "CPU_snapshot_after": {
            "started_ns": 11_000_000_000, "finished_ns": 11_010_000_000,
            "backend_cpu_ns": {str(tid): 1_000_000_000 if tid == 1 else 0 for tid in range(128)},
            "driver_process_tree": {"root_pids": [2], "thread_cpu_ns": {"2:1:2:1": 100_000_000}}},
    }


class ColocatedTPTests(unittest.TestCase):
    def test_separates_backend_and_actual_process_tree_CPU(self):
        p = summarize_point(window(), topology="TP128")
        self.assertEqual(p["baseline_tps"], 100)
        self.assertAlmostEqual(p["backend_cpu_seconds_per_tx"], .001)
        self.assertAlmostEqual(p["driver_cpu_seconds_per_tx"], .0001)
        self.assertFalse(p["automatic_recommendation_qualified"])

    def test_no_interpolation_or_flat_tail_expansion(self):
        with self.assertRaisesRegex(ValueError, "exact"):
            exact_tp_point({"points": {}}, topology="TP128", shared_buffers_mib=8192)

    def test_unknown_topology_rejected(self):
        with self.assertRaisesRegex(ValueError, "topology"):
            summarize_point(window(), topology="flat144")

    def test_backend_membership_count_checked(self):
        w = window()
        w["backend_linux_tids"] = [1]
        with self.assertRaisesRegex(ValueError, "topology"):
            summarize_point(w, topology="TP128")

    def test_S5_requires_actual_surge_gate(self):
        w = window()
        w["backend_linux_tids"] = list(range(144))
        with self.assertRaisesRegex(ValueError, "gate"):
            summarize_point(w, topology="S5_128_plus_16")

    def test_S5_gate_and_two_topologies_are_distinct(self):
        w = window()
        w.update(backend_linux_tids=list(range(144)), not_flat_144_baseline=True,
                 surge_gate_monotonic_ns=1_000_000_000)
        for endpoint in ("before", "after"):
            w["CPU_snapshot_" + endpoint]["backend_cpu_ns"].update(
                {str(tid): 0 for tid in range(128, 144)})
        self.assertEqual(summarize_point(w, topology="S5_128_plus_16")["terminals"], 144)

    def test_heldout_TP_labels_not_accepted_as_training_points(self):
        p = summarize_point(window(), topology="TP128")
        p["role"] = "heldout_TP_only"
        with self.assertRaisesRegex(ValueError, "heldout"):
            exact_tp_point({"points": {"TP128:5120": p}}, topology="TP128",
                           shared_buffers_mib=5120)

    def test_CPU_TID_set_must_match_enrolled_backends(self):
        w = window()
        for endpoint in ("before", "after"):
            w["CPU_snapshot_" + endpoint]["backend_cpu_ns"]["999"] = 0
        with self.assertRaisesRegex(ValueError, "membership"):
            summarize_point(w, topology="TP128")

    def test_wide_CPU_endpoint_is_not_rescaled(self):
        w = window()
        w["CPU_snapshot_after"]["started_ns"] += 200_000_000
        w["CPU_snapshot_after"]["finished_ns"] += 200_000_000
        with self.assertRaisesRegex(ValueError, "endpoint"):
            summarize_point(w, topology="TP128")

    def test_process_tree_replaced_thread_rejected(self):
        w = window()
        w["CPU_snapshot_after"]["driver_process_tree"]["thread_cpu_ns"] = {"x": 100}
        with self.assertRaisesRegex(ValueError, "identity"):
            summarize_point(w, topology="TP128")

    def test_scaled_commits_rejected(self):
        w = window()
        w["transaction_count_rescaled"] = True
        with self.assertRaisesRegex(ValueError, "rescaled"):
            summarize_point(w, topology="TP128")


if __name__ == "__main__":
    unittest.main()
