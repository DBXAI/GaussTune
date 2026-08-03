#!/usr/bin/env python3
"""Build and use a portable, machine-calibrated I/O-to-TPS model bundle."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any


MODEL_SCHEMA = "huawei6.portable-io-tps-model/v1"
CANDIDATE_REQUIRED = {
    "stage", "sb_mb", "work_mem_mb", "ap_cap", "tp_critical_io_per_tx",
    "ap_queue_depth", "tp_block_kib", "tp_issue_path", "ap_block_kib",
    "ap_io_pattern", "tp_baseline_tps", "tp_baseline_await_ms",
    "tp_baseline_io_per_tx", "extra_non_io_ms", "memory_safe", "plan_supported",
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def interpolate(points: dict[int, float], x: float, label: str) -> float:
    keys = sorted(points)
    if not keys:
        raise ValueError(f"{label} has no calibration points")
    if x < keys[0] or x > keys[-1]:
        raise ValueError(f"{label}={x:g} outside calibrated range [{keys[0]}, {keys[-1]}]")
    if x in points:
        return points[int(x)]
    lower = max(key for key in keys if key < x)
    upper = min(key for key in keys if key > x)
    weight = (x - lower) / (upper - lower)
    return points[lower] + weight * (points[upper] - points[lower])


def build_model(surface_path: Path, anchors_path: Path, inventory_path: Path | None, out: Path) -> dict[str, Any]:
    if out.exists():
        raise RuntimeError(f"refusing to overwrite frozen model: {out}")
    surface = read_json(surface_path)
    anchors_doc = read_json(anchors_path)
    cases = list(anchors_doc.get("cases", []))
    if not cases:
        raise ValueError("anchor summary has no cases")
    direct_points = {
        int(depth): float(value)
        for depth, value in surface["tp_added_await_ms_by_ap_queue_depth"].items()
    }
    grouped_multiplier: dict[int, list[float]] = {}
    for case in cases:
        depth = int(case["ap_queue_depth"])
        if depth <= 0:
            continue
        direct_added = interpolate(direct_points, depth, "AP queue depth")
        db_added = float(case["pressure_tp_await_ms"]) - float(case["baseline_tp_await_ms"])
        if direct_added <= 0 or db_added <= 0:
            raise ValueError(f"invalid added latency at anchor QD{depth}")
        grouped_multiplier.setdefault(depth, []).append(db_added / direct_added)
    if len(grouped_multiplier) < 2:
        raise ValueError("at least two nonzero queue-depth path anchors are required")
    path_points = {
        depth: statistics.fmean(values) for depth, values in grouped_multiplier.items()
    }
    baseline_tps = statistics.fmean(float(case["baseline_tp_tps"]) for case in cases)
    baseline_await = statistics.fmean(float(case["baseline_tp_await_ms"]) for case in cases)
    baseline_io_per_tx = statistics.fmean(float(case["baseline_tp_critical_io_per_tx"]) for case in cases)
    terminals_values = {int(case["terminals"]) for case in cases}
    tp_block_values = {round(float(case["tp_mean_request_kib"]), 3) for case in cases}
    if len(terminals_values) != 1:
        raise ValueError(f"anchor terminal count is inconsistent: {terminals_values}")
    terminals = terminals_values.pop()
    surface_tp_depth = int(surface["tp_queue_depth"])
    if surface_tp_depth != terminals:
        raise ValueError(
            f"direct surface TP queue depth {surface_tp_depth} differs from "
            f"database anchor terminals {terminals}"
        )
    inventory = read_json(inventory_path) if inventory_path else {}
    model = {
        "schema": MODEL_SCHEMA,
        "created_epoch_seconds": time.time(),
        "frozen": True,
        "contains_candidate_tps_labels": False,
        "machine_inventory": inventory,
        "domain": {
            "tp_terminals": terminals,
            "tp_block_kib_observed": statistics.fmean(tp_block_values),
            "tp_issue_path": "opengauss_buffered_blocking_read",
            "ap_block_kib": int(surface["ap_block_kib"]),
            "ap_io_pattern": "random_read",
            "ap_queue_depth_min": min(path_points),
            "ap_queue_depth_max": max(path_points),
            "surface_queue_depth_max": max(direct_points),
        },
        "tp_anchor": {
            "terminals": terminals,
            "baseline_tps": baseline_tps,
            "baseline_tp_await_ms": baseline_await,
            "baseline_tp_critical_io_per_tx": baseline_io_per_tx,
        },
        "device_surface": {
            "baseline_direct_tp_await_ms": float(surface["baseline_tp_await_ms"]),
            "direct_added_await_ms_by_ap_queue_depth": {
                str(depth): direct_points[depth] for depth in sorted(direct_points)
            },
        },
        "execution_path_transfer": {
            "buffered_to_direct_added_wait_multiplier": {
                str(depth): path_points[depth] for depth in sorted(path_points)
            },
            "anchor_fields_used": [
                "ap_queue_depth", "baseline_tp_await_ms", "pressure_tp_await_ms",
            ],
            "tps_used_to_fit_path_transfer": False,
        },
        "formula": {
            "latency": "L_pred = L0 + k_path(q_ap) * delta_L_device(q_ap)",
            "non_io": "R_nonio = N*1000/X0 - n0*L0",
            "response": "R_pred = R_nonio + n_candidate*L_pred + extra_non_io_ms",
            "tps": "TPS_pred = N*1000/R_pred",
            "fitted_tps_coefficient": False,
        },
        "sources": {
            "device_surface": {"path": str(surface_path.resolve()), "sha256": sha256(surface_path)},
            "tp_path_anchors": {"path": str(anchors_path.resolve()), "sha256": sha256(anchors_path)},
            "machine_inventory": (
                {"path": str(inventory_path.resolve()), "sha256": sha256(inventory_path)}
                if inventory_path else None
            ),
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
    return model


def predict_latency(model: dict[str, Any], queue_depth: float) -> tuple[float, float, float]:
    anchor = model["tp_anchor"]
    if queue_depth == 0:
        return float(anchor["baseline_tp_await_ms"]), 0.0, 1.0
    direct_points = {
        int(depth): float(value)
        for depth, value in model["device_surface"]["direct_added_await_ms_by_ap_queue_depth"].items()
    }
    path_points = {
        int(depth): float(value)
        for depth, value in model["execution_path_transfer"]["buffered_to_direct_added_wait_multiplier"].items()
    }
    direct_added = interpolate(direct_points, queue_depth, "ap_queue_depth")
    multiplier = interpolate(path_points, queue_depth, "ap_queue_depth path transfer")
    added = direct_added * multiplier
    return float(anchor["baseline_tp_await_ms"]) + added, direct_added, multiplier


def predict_candidate(model: dict[str, Any], row: dict[str, object]) -> dict[str, object]:
    missing = sorted(CANDIDATE_REQUIRED - set(row))
    if missing:
        raise ValueError(f"candidate missing fields: {', '.join(missing)}")
    if not parse_bool(row["memory_safe"]) or not parse_bool(row["plan_supported"]):
        raise ValueError("candidate is not memory-safe or plan-supported")
    anchor = model["tp_anchor"]
    domain = model["domain"]
    if abs(float(row["tp_block_kib"]) - float(domain["tp_block_kib_observed"])) > 1.0:
        raise ValueError(
            f"tp_block_kib={row['tp_block_kib']} differs from calibrated "
            f"{domain['tp_block_kib_observed']:.3f} KiB"
        )
    if str(row["tp_issue_path"]) != str(domain["tp_issue_path"]):
        raise ValueError(
            f"tp_issue_path={row['tp_issue_path']} is not calibrated; expected {domain['tp_issue_path']}"
        )
    if int(float(row["ap_block_kib"])) != int(domain["ap_block_kib"]):
        raise ValueError(
            f"ap_block_kib={row['ap_block_kib']} is not calibrated; expected {domain['ap_block_kib']}"
        )
    if str(row["ap_io_pattern"]) != str(domain["ap_io_pattern"]):
        raise ValueError(
            f"ap_io_pattern={row['ap_io_pattern']} is not calibrated; expected {domain['ap_io_pattern']}"
        )
    calibrated_terminals = int(anchor["terminals"])
    terminals = int(float(row.get("tp_terminals", calibrated_terminals)))
    has_candidate_baseline = all(
        str(row.get(name, "")).strip()
        for name in ("tp_baseline_tps", "tp_baseline_await_ms", "tp_baseline_io_per_tx")
    )
    if not has_candidate_baseline and terminals == calibrated_terminals:
        raise ValueError("candidate-specific TP baseline fields must be nonempty for every configuration")
    if terminals != calibrated_terminals and not has_candidate_baseline:
        raise ValueError(
            f"tp_terminals={terminals} differs from calibrated {calibrated_terminals}; "
            "candidate-specific TP baseline fields are required"
        )
    baseline_tps = float(row["tp_baseline_tps"]) if has_candidate_baseline else float(anchor["baseline_tps"])
    baseline_await = float(row["tp_baseline_await_ms"]) if has_candidate_baseline else float(anchor["baseline_tp_await_ms"])
    baseline_io_per_tx = (
        float(row["tp_baseline_io_per_tx"])
        if has_candidate_baseline else float(anchor["baseline_tp_critical_io_per_tx"])
    )
    critical_io_per_tx = float(row["tp_critical_io_per_tx"])
    queue_depth = float(row["ap_queue_depth"])
    predicted_await, direct_added, path_multiplier = predict_latency(model, queue_depth)
    # Shift the calibrated added wait onto a candidate-specific TP baseline when supplied.
    calibrated_l0 = float(anchor["baseline_tp_await_ms"])
    predicted_await = baseline_await + max(0.0, predicted_await - calibrated_l0)
    baseline_response_ms = terminals * 1000.0 / max(baseline_tps, 1e-12)
    non_io_ms = max(0.0, baseline_response_ms - baseline_io_per_tx * baseline_await)
    extra_non_io_ms = float(row.get("extra_non_io_ms", 0.0) or 0.0)
    predicted_response_ms = non_io_ms + critical_io_per_tx * predicted_await + extra_non_io_ms
    predicted_tps = terminals * 1000.0 / max(predicted_response_ms, 1e-12)
    offered = float(row.get("offered_tps", 0.0) or 0.0)
    if offered > 0:
        predicted_tps = min(predicted_tps, offered)
    slo = float(row.get("tps_slo", baseline_tps * 0.95) or baseline_tps * 0.95)
    result = dict(row)
    result.update({
        "candidate_id": row.get(
            "candidate_id",
            f"{row['stage']}:sb{row['sb_mb']}:wm{row['work_mem_mb']}:cap{row['ap_cap']}",
        ),
        "tp_terminals": terminals,
        "tp_baseline_tps_used": baseline_tps,
        "tp_baseline_await_ms_used": baseline_await,
        "tp_baseline_io_per_tx_used": baseline_io_per_tx,
        "predicted_direct_added_await_ms": direct_added,
        "predicted_path_multiplier": path_multiplier,
        "predicted_tp_await_ms": predicted_await,
        "predicted_non_io_ms": non_io_ms,
        "predicted_response_ms": predicted_response_ms,
        "predicted_tp_tps": predicted_tps,
        "predicted_tp_iops": predicted_tps * critical_io_per_tx,
        "tps_slo_used": slo,
        "tps_slo_met": predicted_tps >= slo,
        "model_domain_checked": True,
    })
    return result


def choose_recommendations(rows: list[dict[str, object]], tolerance: float) -> list[dict[str, object]]:
    output = []
    for stage in sorted({str(row["stage"]) for row in rows}):
        group = [row for row in rows if str(row["stage"]) == stage]
        best_baseline = max(float(row["tp_baseline_tps_used"]) for row in group)
        sb_knee_rows = [
            row for row in group
            if float(row["tp_baseline_tps_used"]) >= best_baseline * (1.0 - tolerance)
        ]
        knee_sb = min(int(float(row["sb_mb"])) for row in sb_knee_rows)
        tp_pool = [row for row in group if int(float(row["sb_mb"])) == knee_sb]
        tp_slo_pool = [row for row in tp_pool if bool(row["tps_slo_met"])] or tp_pool
        tp_first = max(
            tp_slo_pool,
            key=lambda row: (float(row.get("ap_utility", 0.0) or 0.0), float(row["predicted_tp_tps"])),
        )
        best_utility = max(float(row.get("ap_utility", 0.0) or 0.0) for row in group)
        ap_pool = [
            row for row in group
            if float(row.get("ap_utility", 0.0) or 0.0) >= best_utility * (1.0 - tolerance)
        ]
        ap_first = max(ap_pool, key=lambda row: (bool(row["tps_slo_met"]), float(row["predicted_tp_tps"])))
        joint = max(
            {str(row["candidate_id"]): row for row in (tp_first, ap_first)}.values(),
            key=lambda row: (
                bool(row["tps_slo_met"]),
                float(row.get("ap_utility", 0.0) or 0.0),
                float(row["predicted_tp_tps"]),
            ),
        )
        output.append({
            "stage": stage,
            "tp_first_candidate_id": tp_first["candidate_id"],
            "ap_first_candidate_id": ap_first["candidate_id"],
            "joint_candidate_id": joint["candidate_id"],
            "joint_sb_mb": joint["sb_mb"],
            "joint_work_mem_mb": joint["work_mem_mb"],
            "joint_ap_cap": joint["ap_cap"],
            "joint_predicted_tps": joint["predicted_tp_tps"],
            "joint_predicted_await_ms": joint["predicted_tp_await_ms"],
            "joint_tps_slo_met": joint["tps_slo_met"],
        })
    return output


def predict_file(model_path: Path, candidates_path: Path, out_dir: Path, tolerance: float) -> dict[str, Any]:
    model = read_json(model_path)
    if model.get("schema") != MODEL_SCHEMA or not model.get("frozen"):
        raise ValueError("input is not a frozen Huawei6 portable model")
    source = read_csv(candidates_path)
    if not source:
        raise ValueError("candidate CSV is empty")
    predicted = []
    rejected = []
    for index, row in enumerate(source, 2):
        try:
            predicted.append(predict_candidate(model, row))
        except ValueError as exc:
            rejected.append({"csv_line": index, "reason": str(exc), **row})
    if not predicted:
        raise ValueError("all candidates were rejected by model-domain checks")
    recommendations = choose_recommendations(predicted, tolerance)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "candidate_predictions.csv", predicted)
    write_csv(out_dir / "recommendations.csv", recommendations)
    if rejected:
        write_csv(out_dir / "rejected_candidates.csv", rejected)
    summary = {
        "model_sha256": sha256(model_path),
        "candidate_source_sha256": sha256(candidates_path),
        "predicted_candidates": len(predicted),
        "rejected_candidates": len(rejected),
        "recommendations": recommendations,
    }
    (out_dir / "prediction_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("build")
    build.add_argument("--surface", required=True, type=Path)
    build.add_argument("--anchors", required=True, type=Path)
    build.add_argument("--inventory", type=Path)
    build.add_argument("--out", required=True, type=Path)
    predict = sub.add_parser("predict")
    predict.add_argument("--model", required=True, type=Path)
    predict.add_argument("--candidates", required=True, type=Path)
    predict.add_argument("--out-dir", required=True, type=Path)
    predict.add_argument("--path-tolerance", type=float, default=0.03)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--model", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "build":
        model = build_model(args.surface, args.anchors, args.inventory, args.out)
        print(json.dumps({"model": str(args.out), "domain": model["domain"]}, indent=2))
    elif args.action == "predict":
        print(json.dumps(predict_file(args.model, args.candidates, args.out_dir, args.path_tolerance), indent=2))
    else:
        model = read_json(args.model)
        print(json.dumps({
            "schema": model.get("schema"),
            "created_epoch_seconds": model.get("created_epoch_seconds"),
            "domain": model.get("domain"),
            "tp_anchor": model.get("tp_anchor"),
            "formula": model.get("formula"),
            "sources": model.get("sources"),
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
