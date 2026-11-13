#!/usr/bin/env python3
"""Build a current, window-checked TP response table; freeze train/holdout split."""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(matrix_path):
    matrix_path = Path(matrix_path)
    matrix = json.loads(matrix_path.read_text())
    if (matrix.get("schema") != "jmt.current-TP-kernel-window-matrix/v1"
            or matrix.get("complete") is not True or matrix.get("restoration_verified") is not True
            or matrix.get("collection_error") is not None):
        raise ValueError("current TP matrix incomplete or restoration failed")
    grouped = {}
    for source in matrix["rows"]:
        path = Path(source["window_path"])
        if digest(path) != source["window_sha256"]:
            raise ValueError("TP source hash changed")
        row = json.loads(path.read_text())
        window = row["measurement_window"]
        if (window.get("clock") != "monotonic_ns"
                or row["driver_exit_code"] != 0 or row["driver_ignored_errors"] != 0
                or row["device_error_completion_events"] != 0
                or row["committed_transactions"] <= 0):
            raise ValueError("invalid transaction/device evidence")
        duration = (window["end_ns"] - window["start_ns"]) / 1e9
        if duration <= 0 or abs(duration - row["scored_seconds"]) > 1e-9:
            raise ValueError("transaction/device duration mismatch; no proportional repairs")
        tps = row["committed_transactions"] / duration
        if abs(tps - source["TPS"]) > 1e-9:
            raise ValueError("TP table differs from raw exact window")
        if len(row["backend_linux_tids"]) != 128:
            raise ValueError("this model is explicitly the 128 baseline, not the S5 surge")
        if row["benchmark_profile"] != "current_16x1M_sysbench_readonly_omm_local_socket":
            raise ValueError("benchmark identity changed")
        for cpu in (row["CPU_snapshot_before"], row["CPU_snapshot_after"]):
            if not 0 <= cpu["finished_ns"] - cpu["started_ns"] < 100_000_000:
                raise ValueError("CPU sampling boundary is too wide")
        if abs(row["CPU_snapshot_before"]["started_ns"] - window["start_ns"]) > 100_000_000:
            raise ValueError("CPU start is too far from the commit window")
        if abs(row["CPU_snapshot_after"]["started_ns"] - window["end_ns"]) > 100_000_000:
            raise ValueError("CPU end is too far from the commit window")
        grouped.setdefault(source["shared_buffers_mib"], []).append((source, row))
    if len(grouped) < 3:
        raise ValueError("at least three real SB settings required")
    rows, sources = [], []
    for sb, values in sorted(grouped.items()):
        ordered = sorted(values, key=lambda p: p[0]["repeat"])
        if len(ordered) != 3 or [p[0]["repeat"] for p in ordered] != [1, 2, 3]:
            raise ValueError("each point needs two training repeats and one holdout")
        training, heldout = ordered[:2], ordered[2]
        tps = statistics.mean(p[0]["TPS"] for p in training)
        cpu = statistics.mean(p[0]["CPU_ms_per_tx"] for p in training) / 1000
        heldout_tps = heldout[0]["TPS"]
        error = abs(tps - heldout_tps) / heldout_tps
        hit = statistics.mean(p[0]["native_hit_ratio"] for p in training)
        rows.append({
            "shared_buffers_mib": sb, "sustainable_tps": tps,
            "tp_cpu_seconds_per_tx": cpu,
            "native_publication_window_hit_ratio": hit,
            "TPS_training_values": [p[0]["TPS"] for p in training],
            "TPS_training_range": [min(p[0]["TPS"] for p in training),
                                   max(p[0]["TPS"] for p in training)],
            "heldout_TPS": heldout_tps, "heldout_absolute_relative_error": error,
            "repeat_3_not_used_for_selection": True,
            "whole_device_read_events_per_tx_training_proxy": statistics.mean(
                p[0]["read_completion_events_per_tx_whole_device"] for p in training),
            "whole_device_events_are_not_qualified_TP_owned_physical_requests": True,
        })
        sources.extend({"path": p[0]["window_path"], "sha256": p[0]["window_sha256"],
                        "role": "calibration" if i < 2 else "holdout"}
                       for i, p in enumerate(ordered))
    best = max(rows, key=lambda r: (r["sustainable_tps"], -r["shared_buffers_mib"]))
    best_actual = max(rows, key=lambda r: r["heldout_TPS"])
    return {
        "schema": "jmt.current-TP-response/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "machine_fingerprint": matrix["machine_fingerprint"],
        "matrix": str(matrix_path.resolve()), "matrix_sha256": digest(matrix_path),
        "benchmark_profile": "current_16x1M_sysbench_readonly_omm_local_socket",
        "baseline_terminals": 128, "surge_terminals": 0,
        "selection_policy": "max_TPS", "rows": rows, "source_artifacts": sources,
        "TP_only_selected_SB_mib": best["shared_buffers_mib"],
        "TP_only_selected_predicted_TPS": best["sustainable_tps"],
        "TP_only_selected_heldout_TPS": best["heldout_TPS"],
        "holdout": {"maximum_absolute_relative_error": max(r["heldout_absolute_relative_error"] for r in rows),
                    "TPS_prediction_within_20pct": all(r["heldout_absolute_relative_error"] <= .2 for r in rows),
                    "winner_SB_on_holdout": best_actual["shared_buffers_mib"],
                    "training_winner_also_holdout_winner": best_actual["shared_buffers_mib"] == best["shared_buffers_mib"]},
        "physical_TP_attribution_qualified": False,
        "probe_overhead_independently_qualified": False,
        "joint_five_stage_model": False,
        "automatic_recommendation_qualified": False,
        "qualification": "Fresh current TP-only response curve, no AP, no fabricated S5. A TP-only winner is not a mixed-load SB recommendation.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.matrix)
    with args.out.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({key: result[key] for key in (
        "TP_only_selected_SB_mib", "TP_only_selected_predicted_TPS",
        "TP_only_selected_heldout_TPS", "holdout")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
