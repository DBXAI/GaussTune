#!/usr/bin/env python3
"""Compare trace-based SB recommendations with real SB sweep measurements."""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def sb_from_dir(path: Path) -> int | None:
    match = re.fullmatch(r"sb(\d+)mb", path.name)
    return int(match.group(1)) if match else None


def actual_rows(run_dir: Path) -> list[dict[str, str]]:
    preferred = run_dir / "stage_measurements_continuous_actuals.csv"
    if preferred.exists():
        return read_csv(preferred)
    fallback = run_dir / "continuous_best_predictions.csv"
    if fallback.exists():
        rows = []
        for row in read_csv(fallback):
            rows.append(
                {
                    "mode": row["mode"],
                    "sb_mb": row["sb_mb"],
                    "meas_sb_hr": row["meas_sb_hr"],
                    "meas_os_hr": row["meas_os_hr"],
                    "meas_combined": row["meas_combined"],
                }
            )
        return rows
    stage_rows = []
    for path in sorted((run_dir / "stages").glob("*/measurements.csv")):
        stage_rows.extend(read_csv(path))
    return stage_rows


def pct(value: object) -> str:
    return f"{float(value):.6f}"


def render_plot(out_root: Path, rows: list[dict[str, object]]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    stages = list(dict.fromkeys(str(row["stage"]) for row in rows))
    fig, axes = plt.subplots(len(stages), 1, figsize=(11, 3.2 * len(stages)), sharex=True)
    if len(stages) == 1:
        axes = [axes]
    for ax, stage in zip(axes, stages):
        sub = sorted([row for row in rows if row["stage"] == stage], key=lambda r: int(r["sb_mb"]))
        xs = [int(row["sb_mb"]) for row in sub]
        actual = [float(row["actual_combined"]) for row in sub]
        pred = [float(row["pred_combined"]) for row in sub]
        actual_best = max(sub, key=lambda r: float(r["actual_combined"]))
        pred_best = max(sub, key=lambda r: float(r["pred_combined"]))
        ax.plot(xs, actual, marker="o", linewidth=2.2, label="actual combined")
        ax.plot(xs, pred, marker="s", linewidth=1.8, label="pred combined")
        ax.axvline(int(actual_best["sb_mb"]), color="#2c7a3f", linestyle="--", linewidth=1.2)
        ax.axvline(int(pred_best["sb_mb"]), color="#b34545", linestyle=":", linewidth=1.4)
        ax.set_title(
            f"{stage}: actual best {actual_best['sb_mb']}MB, pred best {pred_best['sb_mb']}MB",
            loc="left",
            fontsize=11,
        )
        ax.set_ylabel("combined hit rate")
        ax.grid(True, alpha=0.25)
    axes[-1].set_xscale("log", base=2)
    ticks = sorted({int(row["sb_mb"]) for row in rows})
    axes[-1].set_xticks(ticks)
    axes[-1].set_xticklabels([str(tick) for tick in ticks])
    axes[-1].set_xlabel("shared_buffers (MB)")
    axes[0].legend(loc="best")
    fig.suptitle("Huawei5 SB recommendation validation: predicted vs actual", fontsize=14)
    fig.tight_layout(rect=[0, 0.01, 1, 0.98])
    fig.savefig(out_root / "recommendation_validation_pred_vs_actual.png", dpi=180)
    fig.savefig(out_root / "recommendation_validation_pred_vs_actual.svg")
    plt.close(fig)


def write_report(out_root: Path, summary: list[dict[str, object]], rows: list[dict[str, object]]) -> None:
    lines = [
        "# Huawei5 SB Recommendation Validation",
        "",
        f"- Sweep root: `{out_root}`",
        "- Objective: compare trace-based predicted combined hit rate with real workload measurements.",
        "- Actual combined uses per-stage `pg_stat_database` SB hit rate plus disk-byte based OS conditional hit rate.",
        "",
        "## Per-Stage Best",
        "",
        "| stage | tested SB MB | predicted best SB | actual best SB | pred@actual-best | actual best combined | recommendation matched |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    by_stage: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_stage[str(row["stage"])].append(row)
    for row in summary:
        tested = ",".join(str(int(r["sb_mb"])) for r in sorted(by_stage[str(row["stage"])], key=lambda r: int(r["sb_mb"])))
        lines.append(
            "| {stage} | {tested} | {pred_best_sb_mb} | {actual_best_sb_mb} | "
            "{pred_combined_at_actual_best} | {actual_best_combined} | {matched} |".format(
                tested=tested,
                **row,
            )
        )
    lines += [
        "",
        "## Files",
        "",
        f"- Row CSV: `{out_root / 'recommendation_validation_by_stage.csv'}`",
        f"- Summary CSV: `{out_root / 'recommendation_validation_summary.csv'}`",
        f"- Plot: `{out_root / 'recommendation_validation_pred_vs_actual.png'}`",
    ]
    (out_root / "SB_RECOMMENDATION_VALIDATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep-root", required=True)
    parser.add_argument("--prediction-csv", required=True)
    args = parser.parse_args()

    out_root = Path(args.sweep_root)
    pred_rows = read_csv(Path(args.prediction_csv))
    pred_by_key = {
        (row["stage"], int(float(row["sb_mb"]))): row
        for row in pred_rows
    }

    rows: list[dict[str, object]] = []
    for run_dir in sorted(out_root.glob("sb*mb"), key=lambda p: sb_from_dir(p) or -1):
        sb_mb = sb_from_dir(run_dir)
        if sb_mb is None:
            continue
        for actual in actual_rows(run_dir):
            stage = actual.get("mode") or actual.get("stage")
            if not stage:
                continue
            pred = pred_by_key.get((stage, sb_mb))
            if pred is None:
                continue
            actual_combined = float(actual["meas_combined"])
            pred_combined = float(pred["combined_hit_rate_pred"])
            rows.append(
                {
                    "stage": stage,
                    "sb_mb": sb_mb,
                    "actual_sb": pct(actual["meas_sb_hr"]),
                    "pred_sb": pct(pred["sb_hit_rate_pred"]),
                    "sb_err_pp": f"{(pred_sb := float(pred['sb_hit_rate_pred']) - float(actual['meas_sb_hr'])) * 100.0:.6f}",
                    "actual_os": pct(actual["meas_os_hr"]),
                    "pred_os": pct(pred["os_cond_hit_rate_pred"]),
                    "os_err_pp": f"{(float(pred['os_cond_hit_rate_pred']) - float(actual['meas_os_hr'])) * 100.0:.6f}",
                    "actual_combined": pct(actual_combined),
                    "pred_combined": pct(pred_combined),
                    "combined_err_pp": f"{(pred_combined - actual_combined) * 100.0:.6f}",
                    "run_dir": str(run_dir),
                }
            )

    if not rows:
        raise SystemExit(f"no comparable completed SB runs found in {out_root}")

    stage_order = list(dict.fromkeys(row["stage"] for row in pred_rows))
    rows.sort(key=lambda r: (stage_order.index(str(r["stage"])) if r["stage"] in stage_order else 999, int(r["sb_mb"])))
    write_csv(out_root / "recommendation_validation_by_stage.csv", rows)

    summary: list[dict[str, object]] = []
    for stage in stage_order:
        sub = [row for row in rows if row["stage"] == stage]
        if not sub:
            continue
        pred_best = max(sub, key=lambda r: float(r["pred_combined"]))
        actual_best = max(sub, key=lambda r: float(r["actual_combined"]))
        summary.append(
            {
                "stage": stage,
                "pred_best_sb_mb": int(pred_best["sb_mb"]),
                "pred_best_combined": pct(pred_best["pred_combined"]),
                "actual_best_sb_mb": int(actual_best["sb_mb"]),
                "actual_best_combined": pct(actual_best["actual_combined"]),
                "pred_combined_at_actual_best": pct(actual_best["pred_combined"]),
                "actual_combined_at_pred_best": pct(pred_best["actual_combined"]),
                "matched": "yes" if int(pred_best["sb_mb"]) == int(actual_best["sb_mb"]) else "no",
            }
        )

    write_csv(out_root / "recommendation_validation_summary.csv", summary)
    render_plot(out_root, rows)
    write_report(out_root, summary, rows)
    print(out_root / "SB_RECOMMENDATION_VALIDATION.md")
    print(out_root / "recommendation_validation_by_stage.csv")
    print(out_root / "recommendation_validation_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
