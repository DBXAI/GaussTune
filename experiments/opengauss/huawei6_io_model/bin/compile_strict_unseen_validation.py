#!/usr/bin/env python3
"""Compare frozen unseen-query predictions with later executions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

PAGE_BYTES = 8192
MIB = 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError("no validation rows")
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def normalized_plan_sha(path: Path) -> str:
    lines = [
        line.rstrip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(("SET", "EXPLAIN"))
    ]
    payload = "\n".join(lines) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preregister-dir", required=True, type=Path)
    parser.add_argument("--plan-families", required=True, type=Path)
    parser.add_argument("--actual-root", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    registration = json.loads(
        (args.preregister_dir / "preregistration.json").read_text(encoding="utf-8")
    )
    for filename, expected in registration["prediction_hashes"].items():
        actual = sha256(args.preregister_dir / filename)
        if actual != expected:
            raise SystemExit(
                f"frozen prediction hash mismatch for {filename}: {actual} != {expected}"
            )

    prediction_path = args.preregister_dir / "preregistered_query_predictions.csv"
    predictions = {
        (int(row["query_id"]), int(row["work_mem_mb"])): row
        for row in read_csv(prediction_path)
    }
    plan_rows = read_csv(args.plan_families)
    family_by_sha = {
        (int(row["query_id"]), row["plan_sha256"]): row["plan_family"]
        for row in plan_rows
    }

    output: list[dict[str, object]] = []
    for query_id in registration["target_query_ids"]:
        query_root = args.actual_root / f"q{query_id}"
        boundary_path = query_root / "boundary_results.csv"
        if not boundary_path.exists():
            continue
        for actual in read_csv(boundary_path):
            work_mem_mb = int(actual["work_mem_mb"])
            prediction = predictions.get((query_id, work_mem_mb))
            if prediction is None:
                raise SystemExit(f"no frozen prediction for Q{query_id} at {work_mem_mb}MB")
            point_root = query_root / f"workmem{work_mem_mb}mb"
            actual_family = family_by_sha.get(
                (query_id, normalized_plan_sha(point_root / "plan.txt")), "unknown"
            )
            predicted_spill = prediction["predicted_spill"].lower() == "true"
            predicted_safe_spill = (
                prediction.get("predicted_safe_spill", prediction["predicted_spill"]).lower()
                == "true"
            )
            actual_spill = actual["spill_detected"] == "1"
            predicted_io_mb = float(prediction["predicted_spill_io_mb"])
            actual_io_mb = (
                int(actual["max_temp_read_blocks"])
                + int(actual["max_temp_written_blocks"])
            ) * PAGE_BYTES / MIB
            absolute_io_error_mb = abs(predicted_io_mb - actual_io_mb)
            relative_io_error_pct = (
                100.0 * absolute_io_error_mb / actual_io_mb
                if actual_io_mb > 0
                else (0.0 if predicted_io_mb == 0 else None)
            )
            output.append(
                {
                    "query_id": query_id,
                    "work_mem_mb": work_mem_mb,
                    "predicted_plan_family": prediction["plan_family"],
                    "actual_plan_family": actual_family,
                    "plan_match": prediction["plan_family"] == actual_family,
                    "predicted_spill": predicted_spill,
                    "actual_spill": actual_spill,
                    "spill_class_match": predicted_spill == actual_spill,
                    "predicted_safe_spill": predicted_safe_spill,
                    "safe_spill_class_match": predicted_safe_spill == actual_spill,
                    "predicted_spill_io_mb": round(predicted_io_mb, 3),
                    "actual_temp_io_mb": round(actual_io_mb, 3),
                    "absolute_io_error_mb": round(absolute_io_error_mb, 3),
                    "relative_io_error_pct": (
                        round(relative_io_error_pct, 3)
                        if relative_io_error_pct is not None
                        else ""
                    ),
                    "predicted_no_spill_mb": prediction[
                        "predicted_max_no_spill_work_mem_mb"
                    ],
                    "predicted_safe_no_spill_mb": prediction.get(
                        "predicted_safe_no_spill_work_mem_mb",
                        prediction["predicted_max_no_spill_work_mem_mb"],
                    ),
                    "prediction_source": prediction["prediction_source"],
                    "same_query_trace_used": prediction["same_query_trace_used"],
                    "elapsed": actual["elapsed_seconds"],
                    "exit_status": actual["exit_status"],
                    "result_dir": str(point_root),
                }
            )

    expected_ids = set(registration["target_query_ids"])
    actual_ids = {int(row["query_id"]) for row in output}
    if actual_ids != expected_ids:
        raise SystemExit(
            f"incomplete target set: expected {sorted(expected_ids)}, got {sorted(actual_ids)}"
        )

    output.sort(key=lambda row: (int(row["query_id"]), int(row["work_mem_mb"])))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    point_path = args.out_dir / "strict_unseen_point_results.csv"
    write_csv(point_path, output)

    io_errors = [
        float(row["relative_io_error_pct"])
        for row in output
        if row["relative_io_error_pct"] != "" and float(row["actual_temp_io_mb"]) > 0
    ]
    per_query = {}
    for query_id in sorted(expected_ids):
        rows = [row for row in output if int(row["query_id"]) == query_id]
        per_query[str(query_id)] = {
            "points": len(rows),
            "plan_matches": sum(bool(row["plan_match"]) for row in rows),
            "spill_class_matches": sum(bool(row["spill_class_match"]) for row in rows),
            "safe_spill_class_matches": sum(
                bool(row["safe_spill_class_match"]) for row in rows
            ),
            "actual_spill_points": sum(bool(row["actual_spill"]) for row in rows),
        }
    summary = {
        "protocol": registration["protocol"],
        "registered_at_utc": registration["registered_at_utc"],
        "calibration_query_ids": registration["calibration_query_ids"],
        "target_query_ids": registration["target_query_ids"],
        "query_id_overlap": registration["query_id_overlap"],
        "frozen_prediction_hashes_verified": True,
        "point_count": len(output),
        "plan_matches": sum(bool(row["plan_match"]) for row in output),
        "spill_class_matches": sum(bool(row["spill_class_match"]) for row in output),
        "spill_class_accuracy_pct": round(
            100.0 * sum(bool(row["spill_class_match"]) for row in output) / len(output),
            3,
        ),
        "safe_spill_class_matches": sum(
            bool(row["safe_spill_class_match"]) for row in output
        ),
        "safe_spill_class_accuracy_pct": round(
            100.0
            * sum(bool(row["safe_spill_class_match"]) for row in output)
            / len(output),
            3,
        ),
        "spill_io_mape_pct_on_actual_spill_points": (
            round(sum(io_errors) / len(io_errors), 3) if io_errors else None
        ),
        "per_query": per_query,
    }
    summary_path = args.out_dir / "strict_unseen_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(point_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
