import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from collect_AP_CPU_interference import GROUPS, point_from_window
from collect_current_tp_matrix import digest
from freeze_AP_CPU_interference import build_model, replay_calibration


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def raw_window(start, *, ap=None, ratio=1.):
    end = start+20*10**9
    tids = list(range(128))
    before = {str(t): 0 for t in tids}
    after = {str(t): int(2.25*1e9*ratio) for t in tids}
    ap = ap or {}
    for q, cores in ap.items():
        before[str(q+1000)] = 0
        after[str(q+1000)] = int(cores*20*1e9)
    return {
        "gaussdb_pid": 10, "backend_linux_tids": tids, "shared_buffers_mib": 4096,
        "transaction_count_rescaled": False, "committed_transactions": 180000,
        "scored_seconds": 20, "device_error_completion_events": 0,
        "measurement_window": {"start_ns": start, "end_ns": end},
        "AP_CPU_seconds": {str(q): after[str(q+1000)]/1e9 for q in ap},
        "AP_members_before": [{"application_name": "cal_q%d" % q, "pid": q+2000,
                               "state": "active"} for q in ap],
        "AP_members_after": [{"application_name": "cal_q%d" % q, "pid": q+2000,
                              "state": "active"} for q in ap],
        "CPU_bracket": {
            "before": {"started_ns": start, "finished_ns": start+1_000_000,
                       "backend_cpu_ns": before, "driver_process_tree": {
                           "root_pids": [9], "thread_cpu_ns": {"9:1:9:1": 0}}},
            "after": {"started_ns": end, "finished_ns": end+1_000_000,
                      "backend_cpu_ns": after, "driver_process_tree": {
                          "root_pids": [9], "thread_cpu_ns": {"9:1:9:1": 21_600_000_000}}},
            "qualification": "not a CPU snapshot"}}


def fixture(root, heldout_ratio_extra=0.):
    hashes = {}
    for q in (101, 102, 103):
        text = "SELECT %d" % q
        path = root/"queries"/("q%d.sql" % q)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text+"\n")
        hashes[str(q)] = hashlib.sha256(text.encode()).hexdigest()
    write(root/"POLICY.json", {
        "boot_id": "boot", "machine_fingerprint": "machine", "groups": GROUPS,
        "SQL_sha256s": hashes, "source_sha256": {}})
    write(root/"MACHINE.json", {"machine_fingerprint": "machine"})
    write(root/"SB_RESTORE.json", {"config_restored_byte_exact": True,
        "server_status_matches_original": True, "after": {"postmaster_start_time": "start"}})
    (root/"base.sysbench.log").write_text("test-only raw driver log\n")
    write(root/"DRIVER_VALIDATION.json", {"errors": [], "logs": [
        {"interval_error_rates": [0., 0.], "path": str(root/"base.sysbench.log"),
         "sha256": digest(root/"base.sysbench.log")}]})
    rows, controls = [], []
    for repeat in (1, 2):
        offset = repeat*1000*10**9
        directory = "control_r%02d" % repeat
        raw = raw_window(offset)
        write(root/directory/("TP_ONLY_r%02d.WINDOW.json" % repeat), raw)
        control = {"repeat": repeat, "directory": directory, "point": point_from_window(raw)}
        controls.append(control)
        for i, group in enumerate(GROUPS):
            key = "_".join(map(str, group))+"_r%02d" % repeat
            role = "calibration" if repeat == 1 else "heldout"
            window_start = offset+(100+100*i)*10**9
            ap_cores = .25+.1*i
            raw = raw_window(window_start, ap={q: ap_cores/len(group) for q in group},
                             ratio=1+.03*ap_cores+(heldout_ratio_extra if repeat == 2 else 0))
            raw_path = root/key/("CAL_r%02d.WINDOW.json" % repeat)
            write(raw_path, raw)
            write(root/key/"PRE_AP.json", {
                "AP_queries": group, "AP_sql_sha256s": [hashes[str(q)] for q in group],
                "control": control, "saved_monotonic_ns": window_start-3*10**9, "role": role})
            for q in group:
                write(root/key/("q%d.START.json" % q), {
                    "sql_sha256": hashes[str(q)], "work_mem_mib": 64,
                    "identity": {"linux_tid": q+1000, "backend_pid": q+2000},
                    "application_name": "cal_q%d" % q,
                    "request_sent_monotonic_ns": window_start-2*10**9})
                write(root/key/("q%d.STOP.json" % q), {
                    "requested_monotonic_ns": window_start+21*10**9})
                write(root/key/("q%d.RESULT.json" % q), {"statement_completed": False})
            measured = point_from_window(raw)
            row = {
                "key": key, "role": role, "boot_id": "boot", "postmaster_start_time": "start",
                "machine_fingerprint": "machine", "shared_buffers_mib": 4096, "TP_terminals": 128,
                "AP_sql_sha256s": [hashes[str(q)] for q in group],
                "AP_CPU_cores": sum(raw["AP_CPU_seconds"].values())/20,
                "control_backend_cpu_seconds_per_tx": control["point"]["backend_cpu_seconds_per_tx"],
                "mixed_backend_cpu_seconds_per_tx": measured["backend_cpu_seconds_per_tx"],
                "CPU_endpoints_within_100ms": True, "transaction_count_rescaled": False,
                "control_finished_before_episode": True, "window_sha256": digest(raw_path)}
            rows.append(row)
            write(root/key/"CPU_TRAINING_ROW.json", row)
    write(root/"COLLECTION.json", {"complete": True, "restoration_verified": True,
        "error": None, "cleanup_errors": [], "rows": rows, "controls": controls})
    target = root.parent/(root.name+"_target.json")
    write(target, {"AP_profiles": {"LOW": {"target": {"sql_sha256": "targetSQL"}}}})
    return target


class FreezeCPUInterferenceTests(unittest.TestCase):
    def test_rebuilds_labels_and_trains_first_repeat_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/"cal"
            target = fixture(root)
            rows, _ = replay_calibration(root)
            self.assertEqual(len(rows), 12)
            model = build_model(root, target)
            self.assertEqual(model["training_episodes"], 6)
            self.assertAlmostEqual(model["coefficient_per_AP_core"], .03, places=8)
            self.assertEqual(len(model["heldout_CPU_service_diagnostic"]), 6)
            self.assertFalse(model["heldout_or_target_TPS_used_for_fit"])

    def test_changing_all_heldout_labels_cannot_change_fitted_parameter(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp)/"a", Path(tmp)/"b"
            ma, mb = build_model(a, fixture(a)), build_model(b, fixture(b, .20))
            self.assertEqual(ma["coefficient_per_AP_core"], mb["coefficient_per_AP_core"])
            self.assertGreater(mb["heldout_CPU_service_MAPE"], ma["heldout_CPU_service_MAPE"]+.1)

    def test_changed_raw_window_not_trusted_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/"cal"
            fixture(root)
            path = root/"101_r01/CAL_r01.WINDOW.json"
            raw = json.loads(path.read_text())
            raw["committed_transactions"] += 100
            write(path, raw)
            with self.assertRaises(ValueError):
                replay_calibration(root)

    def test_incomplete_or_unrestored_not_reused(self):
        for key in ("complete", "restoration_verified"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)/"cal"
                fixture(root)
                path = root/"COLLECTION.json"
                value = json.loads(path.read_text())
                value[key] = False
                write(path, value)
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    replay_calibration(root)

    def test_future_control_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/"cal"
            fixture(root)
            path = root/"101_r01/PRE_AP.json"
            value = json.loads(path.read_text())
            value["saved_monotonic_ns"] = 10**30
            write(path, value)
            with self.assertRaisesRegex(ValueError, "chronology"):
                replay_calibration(root)

    def test_wrong_SQL_hash_and_CPU_endpoint_rejected(self):
        for kind in ("SQL", "CPU"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)/"cal"
                fixture(root)
                if kind == "SQL":
                    (root/"queries/q101.sql").write_text("SELECT 999\n")
                else:
                    p = root/"101_r01/CAL_r01.WINDOW.json"
                    value = json.loads(p.read_text())
                    value["CPU_bracket"]["after"]["finished_ns"] += 1_000_000_000
                    write(p, value)
                with self.assertRaises(ValueError):
                    replay_calibration(root)


if __name__ == "__main__":
    unittest.main()
