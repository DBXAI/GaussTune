#!/usr/bin/env python3
"""Replay separate calibration, freeze one CPU slope; holdout never fits."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"), str(ROOT/"vendor"), str(ROOT/"tools")]
from collect_current_tp_matrix import dump, digest
from collect_AP_CPU_interference import point_from_window
from jmt.innovation_4_performance.cpu_interference import fit_interference, backend_multiplier
from jmt.common.db import Gsql


def load(path):
    return json.loads(Path(path).read_text())


def replay_calibration(path):
    """Recompute all fit labels from exact windows, not editable summaries."""
    path = Path(path)
    collection, policy = load(path/"COLLECTION.json"), load(path/"POLICY.json")
    restore, machine = load(path/"SB_RESTORE.json"), load(path/"MACHINE.json")
    if (collection.get("complete") is not True
            or collection.get("restoration_verified") is not True
            or collection.get("error") is not None or collection.get("cleanup_errors")
            or restore["config_restored_byte_exact"] is not True
            or restore["server_status_matches_original"] is not True
            or len(collection["rows"]) != 12 or len(collection["controls"]) != 2):
        raise ValueError("incomplete/unrestored independent calibration")
    for relative, expected in policy["source_sha256"].items():
        if digest(path/"source_snapshot"/relative) != expected:
            raise ValueError("calibration runtime source snapshot changed")
    if machine["machine_fingerprint"] != policy["machine_fingerprint"]:
        raise ValueError("calibration machine mismatch")
    hashes = policy["SQL_sha256s"]
    for query, expected in hashes.items():
        text = (path/"queries"/("q%s.sql" % query)).read_text().rstrip("\n")
        if hashlib.sha256(text.encode()).hexdigest() != expected:
            raise ValueError("calibration SQL identity changed")
    controls = {}
    postmaster_pid = None
    for control in collection["controls"]:
        repeat = control["repeat"]
        raw = load(path/control["directory"]/("TP_ONLY_r%02d.WINDOW.json" % repeat))
        if (raw["device_error_completion_events"] or raw["transaction_count_rescaled"]
                or raw["AP_CPU_seconds"] or raw["scored_seconds"] != 20):
            raise ValueError("invalid independent TP-only CPU reference")
        p = point_from_window(raw)
        if p != control["point"]:
            raise ValueError("CPU reference not reproduced from raw endpoints")
        controls[repeat] = (p, control)
        if postmaster_pid is not None and raw["gaussdb_pid"] != postmaster_pid:
            raise ValueError("postmaster changed during calibration")
        postmaster_pid = raw["gaussdb_pid"]
    computed, keys = [], set()
    for saved in collection["rows"]:
        key = saved["key"]
        if Path(key).name != key or key in keys:
            raise ValueError("unsafe or duplicate calibration key")
        keys.add(key)
        case = path/key
        repeat = int(key.rsplit("_r", 1)[1])
        pre = load(case/"PRE_AP.json")
        raw_path = case/("CAL_r%02d.WINDOW.json" % repeat)
        raw = load(raw_path)
        control, original_control = controls[repeat]
        queries = pre["AP_queries"]
        if (pre["control"] != original_control or raw["gaussdb_pid"] != postmaster_pid
                or raw["device_error_completion_events"] or raw["transaction_count_rescaled"]
                or raw["scored_seconds"] != 20 or raw["shared_buffers_mib"] != 4096
                or sorted(map(int, raw["AP_CPU_seconds"])) != sorted(queries)
                or pre["AP_sql_sha256s"] != [hashes[str(q)] for q in queries]
                or pre["role"] != ("calibration" if repeat == 1 else "heldout")):
            raise ValueError("calibration cohort/role/control/window binding failed")
        starts, measured_AP_cpu = [], 0.
        expected_members = {}
        for q in queries:
            start, stop, result = (
                load(case/("q%d.%s.json" % (q, name))) for name in ("START", "STOP", "RESULT"))
            if (start["sql_sha256"] != hashes[str(q)] or start["work_mem_mib"] != 64
                    or str(start["identity"]["linux_tid"]) not in raw["CPU_bracket"]["before"]["backend_cpu_ns"]
                    or not pre["saved_monotonic_ns"] < start["request_sent_monotonic_ns"]
                    < raw["measurement_window"]["start_ns"]
                    < raw["measurement_window"]["end_ns"] < stop["requested_monotonic_ns"]
                    or result["statement_completed"] is not False):
                raise ValueError("SQL/CPU/forecast/cancellation chronology mismatch")
            starts.append(start["request_sent_monotonic_ns"])
            tid = str(start["identity"]["linux_tid"])
            if int(tid) in raw["backend_linux_tids"]:
                raise ValueError("AP task overlaps enrolled TP backend")
            cpu = (raw["CPU_bracket"]["after"]["backend_cpu_ns"][tid]
                   - raw["CPU_bracket"]["before"]["backend_cpu_ns"][tid])/1e9
            if cpu < 0 or cpu != raw["AP_CPU_seconds"][str(q)] or cpu > 1.05*raw["scored_seconds"]:
                raise ValueError("AP calibration feature not reproduced from schedstat")
            measured_AP_cpu += cpu
            expected_members[start["application_name"]] = start["identity"]["backend_pid"]
        for endpoint in ("before", "after"):
            members = raw["AP_members_"+endpoint]
            if (len(members) != len(queries) or any(r["state"] != "active" for r in members)
                    or {r["application_name"]: r["pid"] for r in members} != expected_members):
                raise ValueError("calibration AP cohort changed during window")
        measured = point_from_window(raw)
        row = {
            "key": key, "role": pre["role"], "boot_id": policy["boot_id"],
            "postmaster_start_time": restore["after"]["postmaster_start_time"],
            "machine_fingerprint": machine["machine_fingerprint"],
            "shared_buffers_mib": 4096, "TP_terminals": 128,
            "AP_sql_sha256s": pre["AP_sql_sha256s"],
            "AP_CPU_cores": measured_AP_cpu/raw["scored_seconds"],
            "control_backend_cpu_seconds_per_tx": control["backend_cpu_seconds_per_tx"],
            "mixed_backend_cpu_seconds_per_tx": measured["backend_cpu_seconds_per_tx"],
            "CPU_endpoints_within_100ms": True, "transaction_count_rescaled": False,
            "control_finished_before_episode": control["calibration_window_end_ns"] < min(starts),
            "window_sha256": digest(raw_path),
        }
        if row != saved or row != load(case/"CPU_TRAINING_ROW.json"):
            raise ValueError("calibration training row not reproduced from raw windows")
        computed.append(row)
    expected = {("_".join(map(str, g))+"_r%02d" % r)
                for g in policy["groups"] for r in (1, 2)}
    if keys != expected:
        raise ValueError("missing independent calibration episodes")
    driver = load(path/"DRIVER_VALIDATION.json")
    if (driver["errors"] or not driver["logs"] or any(
            not r["interval_error_rates"] or any(r["interval_error_rates"])
            or r.get("final_ignored_errors") not in (None, 0)
            or digest(path/Path(r["path"]).name) != r["sha256"]
            for r in driver["logs"])):
        raise ValueError("TP driver errors in CPU calibration")
    return computed, machine


def build_model(collection, target_frozen):
    rows, machine = replay_calibration(collection)
    target = load(target_frozen)
    excluded = sorted({r["sql_sha256"] for r in target["AP_profiles"]["LOW"].values()})
    model = fit_interference([r for r in rows if r["role"] == "calibration"],
                             excluded_target_sql_sha256s=excluded)
    holdout = []
    for row in rows:
        if row["role"] != "heldout":
            continue
        actual = row["mixed_backend_cpu_seconds_per_tx"]
        forecast = row["control_backend_cpu_seconds_per_tx"] * backend_multiplier(
            model, row["AP_CPU_cores"])
        holdout.append({
            "key": row["key"], "measured_AP_CPU_cores_used_for_CPU_service_test_only": row["AP_CPU_cores"],
            "predicted_backend_cpu_seconds_per_tx": forecast, "actual_backend_cpu_seconds_per_tx": actual,
            "absolute_relative_error": abs(forecast/actual-1),
            "no_inflation_absolute_relative_error": abs(
                row["control_backend_cpu_seconds_per_tx"]/actual-1)})
    model.update({
        "created_at": datetime.now().astimezone().isoformat(),
        "frozen_monotonic_ns": time.monotonic_ns(),
        "training_repeat": 1, "heldout_repeat": 2,
        "heldout_CPU_service_diagnostic": holdout,
        "heldout_CPU_service_MAPE": sum(r["absolute_relative_error"] for r in holdout)/len(holdout),
        "heldout_no_inflation_CPU_service_MAPE": sum(
            r["no_inflation_absolute_relative_error"] for r in holdout)/len(holdout),
        "heldout_or_target_TPS_used_for_fit": False,
        "heldout_evaluates_service_given_AP_CPU_not_forecast_AP_CPU": True,
        "source_inventory": {str(p.relative_to(collection)): digest(p)
            for p in sorted(Path(collection).rglob("*")) if p.is_file()
            and p.suffix not in (".private", ".pyc") and "__pycache__" not in p.parts},
        "training_model_source_sha256": digest(ROOT/"src/jmt/innovation_4_performance/cpu_interference.py"),
        "excluded_target_source_sha256": digest(target_frozen)})
    return model


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--collection", type=Path, required=True)
    p.add_argument("--target-frozen", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    model = build_model(args.collection, args.target_frozen)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=20)
    if (model["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            or model["postmaster_start_time"] != client.json("postgres",
            "SELECT json_build_object('start',pg_postmaster_start_time())")["start"]):
        raise ValueError("calibration boot/postmaster no longer current")
    dump(args.out, model)
    print(json.dumps({k: v for k, v in model.items() if k not in (
        "source_inventory", "heldout_CPU_service_diagnostic")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
