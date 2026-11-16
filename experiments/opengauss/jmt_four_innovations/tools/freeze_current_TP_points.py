#!/usr/bin/env python3
"""Freeze exact physical-SB TP-only repeat 1; repeat 2 never trains the model."""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.innovation_4_performance.colocated_tp import summarize_point

POINTS = (2048, 4096, 6144, 8192)


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_point(window, sb, repeat):
    if window["shared_buffers_mib"] != sb or window["repeat"] != repeat:
        raise ValueError("calibration window SB/repeat mismatch")
    if (window["device_error_completion_events"] or window["driver_exit_code"]
            or window["driver_ignored_errors"]):
        raise ValueError("TP-only collector/device/driver errors")
    duration = (window["measurement_window"]["end_ns"]
                - window["measurement_window"]["start_ns"]) / 1e9
    point = summarize_point(window, topology="TP128")
    if (not math.isclose(duration, window["scored_seconds"], rel_tol=1e-12)
            or not math.isclose(point["baseline_tps"], window["observed_committed_TPS"],
                                rel_tol=1e-12)):
        raise ValueError("TP-only TPS/scored duration differs from raw commits")
    point["role"] = "calibration" if repeat == 1 else "heldout_TP_only"
    return point


def freeze(run):
    cal = run / "TP_calibration"
    matrix, policy, restore = (read(cal / name) for name in (
        "MATRIX.json", "COLLECTION_POLICY.json", "SB_RESTORE.json"))
    registered = read(run / "PRE_REGISTERED_POLICY.json")
    if not (matrix["complete"] and matrix["restoration_verified"]
            and restore["config_restored_byte_exact"] and restore["server_status_matches_original"]
            and matrix["collection_error"] is None):
        raise ValueError("complete clean restored TP-only matrix required")
    if (set(policy["SB_points"]) != set(POINTS) or policy["repeats"] != 2
            or registered["training_repeat"] != 1 or registered["heldout_repeat"] != 2):
        raise ValueError("preregistered train/holdout policy changed")
    if registered["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
        raise ValueError("TP collection cannot cross machine boots")
    expected = {(sb, r) for sb in POINTS for r in (1, 2)}
    if (len(matrix["rows"]) != 8 or
            {(r["shared_buffers_mib"], r["repeat"]) for r in matrix["rows"]} != expected):
        raise ValueError("exact 4x2 TP-only observations required")
    inputs = [run / "PRE_REGISTERED_POLICY.json"] + [
        cal / n for n in ("MATRIX.json", "COLLECTION_POLICY.json", "SB_RESTORE.json", "MACHINE.json")]
    model = {
        "schema": "jmt.exact-SB-colocated-TP-points/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "frozen_after_TP_collection_monotonic_ns": time.monotonic_ns(),
        "training_repeat": 1, "holdout_repeat": 2,
        "machine_fingerprint": matrix["machine_fingerprint"], "boot_id": registered["boot_id"],
        "postmaster_start_time": restore["after"]["postmaster_start_time"],
        "points": {}, "holdout": [], "source_windows": {},
        "mixed_TPS_labels_used": False, "no_stage_or_query_ID_coefficients": True,
        "fresh_cache_penalty_or_TP_physical_IO_calibrated": False,
        "automatic_recommendation_qualified": False}
    for sb in POINTS:
        rows = []
        for repeat in (1, 2):
            folder = cal / ("sb%d_r%02d" % (sb, repeat))
            path, status = folder / "WINDOW.json", read(folder / "SB_STATUS.json")
            if (status["postmaster_start_time"] != model["postmaster_start_time"]
                    or status["target_buffers"] != sb * 128):
                raise ValueError("TP-only native status identity/target mismatch")
            window = read(path)
            row = next(r for r in matrix["rows"]
                       if r["shared_buffers_mib"] == sb and r["repeat"] == repeat)
            if sha(path) != row["window_sha256"] or row["TPS"] != window["observed_committed_TPS"]:
                raise ValueError("raw calibration window binding mismatch")
            rows.append(checked_point(window, sb, repeat))
            model["source_windows"][str(path.relative_to(run))] = sha(path)
            inputs.extend((path, folder / "SB_STATUS.json", folder / "sysbench.log"))
        train, test = rows
        model["points"]["TP128:" + str(sb)] = train
        model["holdout"].append({
            "topology": "TP128", "SB_mib": sb, "observed": test,
            "predicted_TPS_from_repeat1": train["baseline_tps"],
            "absolute_relative_error": abs(train["baseline_tps"] / test["baseline_tps"] - 1)})
    model["heldout_TP_only_MAPE"] = statistics.mean(
        r["absolute_relative_error"] for r in model["holdout"])
    model["input_sha256"] = {str(p.relative_to(run)): sha(p) for p in inputs}
    return model


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    model = freeze(args.run)
    with args.out.open("x") as stream:
        json.dump(model, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"points": model["points"], "heldout_TP_only_MAPE": model[
        "heldout_TP_only_MAPE"]}))


if __name__ == "__main__":
    main()
