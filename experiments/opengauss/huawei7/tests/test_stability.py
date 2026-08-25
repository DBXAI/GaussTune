import unittest

from huawei7.stability import (
    assess_precondition_convergence, assess_warmup_stability,
    cache_normalization_from_text, storage_quiescence_from_text,
    summarize_repeat_stability,
    transaction_rate_windows,
)


def snapshot(sequence, transactions, *, database="tp"):
    return {
        "schema": "huawei7.native-database-stats-snapshot/v1",
        "datid": 42,
        "datname": database,
        "stats_reset": "never",
        "collected_start_ns": sequence * 5_000_000_000,
        "collected_end_ns": sequence * 5_000_000_000 + 10_000_000,
        "xact_commit": transactions,
        "xact_rollback": 0,
    }


class StabilityTest(unittest.TestCase):
    def test_cache_normalization_requires_one_exact_oid_record(self):
        record = {
            "schema": "huawei7.workload-cache-normalization/v1",
            "method": "POSIX_FADV_DONTNEED while openGauss is stopped",
            "database_oids": [1, 2], "file_count": 2,
            "logical_bytes_advised": 10,
            "server_stopped_during_eviction": True, "valid": True,
        }
        text = "noise\n" + __import__("json").dumps(record) + "\n"
        self.assertEqual(cache_normalization_from_text(text, [2, 1]), record)
        with self.assertRaisesRegex(RuntimeError, "differs"):
            cache_normalization_from_text(text, [1])

    def test_stable_tail_is_accepted_and_all_windows_are_retained(self):
        rows = [
            snapshot(0, 0), snapshot(1, 100), snapshot(2, 600),
            snapshot(3, 1100), snapshot(4, 1610),
        ]
        report = assess_warmup_stability(rows)
        self.assertTrue(report["valid"])
        self.assertEqual(report["window_count"], 4)
        self.assertEqual(report["required_tail_windows"], 3)
        self.assertLess(report["tail_relative_span"], .20)

    def test_rising_tail_and_counter_reset_fail_closed(self):
        rising = [
            snapshot(0, 0), snapshot(1, 100), snapshot(2, 300),
            snapshot(3, 700), snapshot(4, 1500),
        ]
        self.assertFalse(assess_warmup_stability(rising)["valid"])
        reset = [snapshot(0, 100), snapshot(1, 50)]
        with self.assertRaisesRegex(ValueError, "backwards"):
            transaction_rate_windows(reset)

    def test_short_marker_window_is_retained_but_excluded_from_tail(self):
        rows = [
            snapshot(0, 0), snapshot(1, 500), snapshot(2, 1000),
            snapshot(3, 1500), snapshot(4, 2000),
        ]
        marker = snapshot(5, 2600)
        marker["collected_start_ns"] = (
            rows[-1]["collected_end_ns"] + 3_000_000_000
        )
        marker["collected_end_ns"] = marker["collected_start_ns"] + 10_000_000
        rows.append(marker)
        self.assertFalse(assess_warmup_stability(rows)["valid"])
        report = assess_warmup_stability(rows, minimum_window_seconds=4.0)
        self.assertTrue(report["valid"])
        self.assertEqual(report["schema"], "huawei7.tp-warmup-stability/v2")
        self.assertEqual(report["window_count"], 5)
        self.assertEqual(report["eligible_window_count"], 4)
        self.assertEqual(report["excluded_trailing_short_window_numbers"], [5])
        self.assertEqual(report["selected_tail_window_numbers"], [2, 3, 4])

    def test_two_block_gate_does_not_alias_a_stationary_tpcc_cycle(self):
        # The last three windows alone look like a 12% rising drift, while the
        # adjacent complete three-window block means differ by less than 1%.
        deltas = [450, 409, 384, 382, 426, 435]
        total = 0
        rows = [snapshot(0, total)]
        for sequence, delta in enumerate(deltas, 1):
            total += delta
            rows.append(snapshot(sequence, total))
        self.assertFalse(assess_warmup_stability(rows)["valid"])
        report = assess_warmup_stability(rows, comparison_blocks=2)
        self.assertTrue(report["valid"])
        self.assertEqual(report["schema"], "huawei7.tp-warmup-stability/v3")
        self.assertEqual(report["comparison_blocks"], 2)
        self.assertEqual(
            report["comparison_block_window_numbers"],
            [[1, 2, 3], [4, 5, 6]],
        )
        self.assertLess(report["comparison_block_mean_relative_drift"], .01)

    def test_database_identity_change_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "comparable"):
            transaction_rate_windows([
                snapshot(0, 0), snapshot(1, 100, database="other"),
            ])

    def test_repeat_gate_keeps_the_full_range(self):
        accepted = summarize_repeat_stability([100, 103, 97])
        self.assertTrue(accepted["valid"])
        rejected = summarize_repeat_stability([100, 103, 40])
        self.assertFalse(rejected["valid"])
        self.assertEqual(rejected["throughputs_tps"], [100.0, 103.0, 40.0])

    def test_precondition_convergence_uses_the_complete_tail(self):
        rising = assess_precondition_convergence([100, 200, 300])
        self.assertFalse(rising["valid"])
        settled = assess_precondition_convergence([100, 200, 300, 302, 297])
        self.assertTrue(settled["valid"])
        self.assertEqual(settled["tail_throughputs_tps"], [300.0, 302.0, 297.0])

    def test_storage_quiescence_requires_one_valid_record(self):
        record = {
            "schema": "huawei7.storage-quiescence/v1",
            "device": "/dev/nvme0n1",
            "checkpoint_completed": True,
            "required_consecutive_samples": 3,
            "accepted_consecutive_samples": 3,
            "samples": [{}, {}, {}],
            "valid": True,
        }
        text = "CHECKPOINT\n" + __import__("json").dumps(record) + "\n"
        self.assertEqual(storage_quiescence_from_text(text), record)
        record["valid"] = False
        with self.assertRaisesRegex(RuntimeError, "differs"):
            storage_quiescence_from_text(__import__("json").dumps(record))


if __name__ == "__main__":
    unittest.main()
