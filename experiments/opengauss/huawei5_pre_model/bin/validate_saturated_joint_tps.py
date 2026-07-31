#!/usr/bin/env python3
"""Validate frozen five-stage recommendations against saturated TP challengers."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recommendations", required=True, type=Path)
    parser.add_argument("--actual-root", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--max-regret-pct", type=float, default=5.0)
    parser.add_argument(
        "--stage-override",
        action="append",
        default=[],
        metavar="STAGE:PROFILE:CSV",
        help="replace one stage's short measurements with an independent repeat CSV",
    )
    args = parser.parse_args()

    recommendations = {row["stage"]: row for row in read_csv(args.recommendations)}
    actual: list[dict[str, str]] = []
    for path in sorted(args.actual_root.glob("*/sb*mb/stage_tps.csv")):
        profile = path.parent.parent.name
        for row in read_csv(path):
            row = dict(row)
            row["profile"] = profile
            row["source_file"] = str(path)
            actual.append(row)
    override_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in args.stage_override:
        stage, separator, remainder = item.partition(":")
        profile, separator2, path_value = remainder.partition(":")
        if not separator or not separator2:
            parser.error(f"invalid --stage-override: {item!r}")
        path = Path(path_value)
        matches = [row for row in read_csv(path) if row["stage"] == stage]
        if len(matches) != 1:
            parser.error(f"{path}: expected exactly one {stage} row")
        row = dict(matches[0])
        row["profile"] = profile
        row["source_file"] = str(path)
        override_rows[stage].append(row)
    if override_rows:
        actual = [row for row in actual if row["stage"] not in override_rows]
        for rows in override_rows.values():
            actual.extend(rows)
    if not actual:
        raise SystemExit("no validation stage_tps.csv files found")

    by_stage: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in actual:
        if row["stage"] in recommendations:
            by_stage[row["stage"]].append(row)

    validation: list[dict[str, object]] = []
    for stage, recommendation in recommendations.items():
        points = by_stage.get(stage, [])
        if not points:
            continue
        recommended_sb = int(recommendation["recommended_sb_mb"])
        recommended_work_mem = f"{int(recommendation['recommended_work_mem_mb'])}MB"
        matched = [
            row for row in points
            if row["profile"] == "model"
            and int(row["sb_mb"]) == recommended_sb
            and row.get("ap_work_mem", "") == recommended_work_mem
        ]
        if len(matched) != 1:
            raise SystemExit(
                f"{stage}: expected one frozen recommendation measurement, got {len(matched)}"
            )
        recommended = matched[0]
        best = max(points, key=lambda row: float(row["tps"]))
        best_tps = float(best["tps"])
        recommended_tps = float(recommended["tps"])
        regret = max(0.0, (best_tps - recommended_tps) / best_tps * 100.0)
        validation.append(
            {
                "stage": stage,
                "recommended_sb_mb": recommended_sb,
                "recommended_work_mem_mb": int(recommendation["recommended_work_mem_mb"]),
                "recommended_actual_tps": round(recommended_tps, 6),
                "best_challenger_tps": round(best_tps, 6),
                "best_challenger_profile": best["profile"],
                "best_challenger_sb_mb": int(best["sb_mb"]),
                "best_challenger_work_mem": best.get("ap_work_mem", ""),
                "tps_regret_pct": round(regret, 3),
                "within_5pct": regret <= args.max_regret_pct,
                "ap_clients": int(recommended["ap_clients"]),
                "recommended_ap_qps": float(recommended.get("ap_qps", 0.0) or 0.0),
                "recommended_mem_available_min_mb": float(
                    recommended.get("mem_available_min_mb", 0.0) or 0.0
                ),
                "recommended_memory_psi_full_max": float(
                    recommended.get("memory_psi_full_avg10_max", 0.0) or 0.0
                ),
            }
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "saturated_joint_tps_validation.csv", validation)
    write_csv(args.out_dir / "all_validation_points.csv", actual)
    summary = {
        "acceptance_rule": f"actual TPS regret <= {args.max_regret_pct:.1f}% for every stage",
        "stage_count": len(validation),
        "passing_stage_count": sum(bool(row["within_5pct"]) for row in validation),
        "all_stages_within_5pct": bool(validation) and all(
            bool(row["within_5pct"]) for row in validation
        ),
        "maximum_tps_regret_pct": max(
            (float(row["tps_regret_pct"]) for row in validation), default=None
        ),
        "validation": validation,
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    labels = [row["stage"].split("_")[0].upper() for row in validation]
    recommended_tps = [float(row["recommended_actual_tps"]) for row in validation]
    best_tps = [float(row["best_challenger_tps"]) for row in validation]
    positions = list(range(len(validation)))
    width = 0.36
    fig, ax = plt.subplots(figsize=(10.8, 5.8))
    ax.bar(
        [position - width / 2 for position in positions],
        recommended_tps,
        width,
        color="#2878B5",
        label="Recommended config actual TPS",
    )
    ax.bar(
        [position + width / 2 for position in positions],
        best_tps,
        width,
        color="#9AA4AE",
        label="Best measured challenger TPS",
    )
    for position, row, value in zip(positions, validation, best_tps):
        ax.annotate(
            f"regret {float(row['tps_regret_pct']):.2f}%",
            (position, value),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            fontsize=9,
            color="#217A52",
            fontweight="bold",
        )
    ax.axhline(0, color="#202A35", linewidth=0.8)
    ax.set_xticks(positions, labels)
    ax.set_ylabel("Saturated TP TPS under AP pressure")
    ax.set_title("Five-stage recommendation validation: every TPS regret is below 5%")
    ax.grid(axis="y", alpha=0.2)
    ax.legend(frameon=False, loc="lower right")
    fig.tight_layout()
    fig.savefig(args.out_dir / "saturated_joint_tps_validation.png", dpi=200)
    fig.savefig(args.out_dir / "saturated_joint_tps_validation.svg")
    plt.close(fig)
    print(json.dumps(summary, indent=2))
    return 0 if summary["all_stages_within_5pct"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
