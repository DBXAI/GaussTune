#!/usr/bin/env python3
"""Calibrate Huawei5 SB/OS cache-hit predictions with machine-local sweep data.

The trace replay model is kept as the base model.  This script learns a small
per-stage residual correction from completed real SB sweep points:

    calibrated_metric = raw_metric + smooth_residual(stage, log2(shared_buffers))

It is intentionally a calibration layer, not a replacement for the trace replay.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable


TARGETS = ("sb", "os", "combined")


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


def pct(value: object) -> str:
    return f"{float(value):.6f}"


def pp(value: float) -> str:
    return f"{value * 100.0:.6f}"


def clamp_rate(value: float) -> float:
    return max(0.0, min(0.999999, value))


def combined_rate(sb_hit: float, os_cond_hit: float) -> float:
    return 1.0 - (1.0 - sb_hit) * (1.0 - os_cond_hit)


def sb_from_dir(path: Path) -> int | None:
    match = re.fullmatch(r"sb(\d+)mb", path.name)
    return int(match.group(1)) if match else None


def config_status(sweep_root: Path | None, sb_mb: int, tested_sbs: set[int]) -> str:
    if sweep_root is None:
        return "tested_ok" if sb_mb in tested_sbs else "untested"
    failed = sorted(
        sb
        for sb in (sb_from_dir(path) for path in sweep_root.glob("sb*mb"))
        if sb is not None and (sweep_root / f"sb{sb}mb" / "FAILED.txt").exists()
    )
    if sb_mb in tested_sbs:
        return "tested_ok"
    if sb_mb in failed:
        return "failed"
    if failed and sb_mb > min(failed):
        return "above_failed"
    return "untested"


def usable_for_recommendation(status: str) -> bool:
    return status not in {"failed", "above_failed"}


class KernelResidualCalibrator:
    def __init__(self, rows: list[dict[str, str]], bandwidth: float) -> None:
        self.rows = rows
        self.bandwidth = bandwidth
        self.residuals: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
        for row in rows:
            stage = row["stage"]
            x = math.log2(int(float(row["sb_mb"])))
            self.residuals[(stage, "sb")].append((x, float(row["actual_sb"]) - float(row["pred_sb"])))
            self.residuals[(stage, "os")].append((x, float(row["actual_os"]) - float(row["pred_os"])))

    def residual(self, stage: str, target: str, sb_mb: int, exclude_row: dict[str, str] | None = None) -> float:
        points = list(self.residuals.get((stage, target), []))
        if exclude_row is not None:
            exclude_x = math.log2(int(float(exclude_row["sb_mb"])))
            exclude_res = float(exclude_row[f"actual_{target}"]) - float(exclude_row[f"pred_{target}"])
            removed = False
            kept: list[tuple[float, float]] = []
            for x, res in points:
                if not removed and abs(x - exclude_x) < 1e-12 and abs(res - exclude_res) < 1e-12:
                    removed = True
                    continue
                kept.append((x, res))
            points = kept
        if not points:
            return 0.0

        x = math.log2(sb_mb)
        weights = [
            (math.exp(-0.5 * ((x - px) / self.bandwidth) ** 2), residual)
            for px, residual in points
        ]
        weight_sum = sum(weight for weight, _ in weights)
        if weight_sum <= 1e-12:
            return min(points, key=lambda point: abs(point[0] - x))[1]
        return sum(weight * residual for weight, residual in weights) / weight_sum

    def calibrate_metric(
        self,
        stage: str,
        target: str,
        sb_mb: int,
        raw_value: float,
        exclude_row: dict[str, str] | None = None,
    ) -> float:
        return clamp_rate(raw_value + self.residual(stage, target, sb_mb, exclude_row=exclude_row))

    def calibrate_pair(
        self,
        stage: str,
        sb_mb: int,
        raw_sb: float,
        raw_os: float,
        exclude_row: dict[str, str] | None = None,
    ) -> tuple[float, float, float]:
        cal_sb = self.calibrate_metric(stage, "sb", sb_mb, raw_sb, exclude_row=exclude_row)
        cal_os = self.calibrate_metric(stage, "os", sb_mb, raw_os, exclude_row=exclude_row)
        return cal_sb, cal_os, combined_rate(cal_sb, cal_os)


def metric_errors(rows: Iterable[dict[str, object]], pred_prefix: str) -> dict[str, float]:
    errors: dict[str, list[float]] = {target: [] for target in TARGETS}
    for row in rows:
        for target in TARGETS:
            errors[target].append(abs(float(row[f"{pred_prefix}_{target}"]) - float(row[f"actual_{target}"])))
    return {target: statistics.mean(values) for target, values in errors.items()}


def stage_best(rows: list[dict[str, object]], pred_key: str) -> dict[str, dict[str, object]]:
    by_stage: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_stage[str(row["stage"])].append(row)
    best: dict[str, dict[str, object]] = {}
    for stage, sub in by_stage.items():
        best[stage] = {
            "actual": max(sub, key=lambda row: float(row["actual_combined"])),
            "pred": max(sub, key=lambda row: float(row[pred_key])),
        }
    return best


def recommendation_count(rows: list[dict[str, object]], pred_key: str) -> int:
    return sum(
        int(int(best["actual"]["sb_mb"]) == int(best["pred"]["sb_mb"]))
        for best in stage_best(rows, pred_key).values()
    )


def render_plot(out_root: Path, rows: list[dict[str, object]]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    stages = list(dict.fromkeys(str(row["stage"]) for row in rows))
    fig, axes = plt.subplots(len(stages), 1, figsize=(11, 3.15 * len(stages)), sharex=True)
    if len(stages) == 1:
        axes = [axes]
    for ax, stage in zip(axes, stages):
        sub = sorted([row for row in rows if row["stage"] == stage], key=lambda row: int(row["sb_mb"]))
        xs = [int(row["sb_mb"]) for row in sub]
        actual = [float(row["actual_combined"]) for row in sub]
        raw = [float(row["raw_combined"]) for row in sub]
        cal = [float(row["calibrated_combined"]) for row in sub]
        actual_best = max(sub, key=lambda row: float(row["actual_combined"]))
        cal_best = max(sub, key=lambda row: float(row["calibrated_combined"]))
        ax.plot(xs, actual, marker="o", linewidth=2.2, label="actual combined")
        ax.plot(xs, raw, marker="s", linewidth=1.4, linestyle=":", label="raw prediction")
        ax.plot(xs, cal, marker="^", linewidth=1.8, label="calibrated prediction")
        ax.axvline(int(actual_best["sb_mb"]), color="#2c7a3f", linestyle="--", linewidth=1.1)
        ax.axvline(int(cal_best["sb_mb"]), color="#b34545", linestyle=":", linewidth=1.3)
        ax.set_title(
            f"{stage}: actual best {actual_best['sb_mb']}MB, calibrated best {cal_best['sb_mb']}MB",
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
    fig.suptitle("Huawei5 calibrated cache-hit prediction validation", fontsize=14)
    fig.tight_layout(rect=[0, 0.01, 1, 0.98])
    fig.savefig(out_root / "calibrated_prediction_validation.png", dpi=180)
    fig.savefig(out_root / "calibrated_prediction_validation.svg")
    plt.close(fig)


def write_report(
    out_root: Path,
    validation_rows: list[dict[str, object]],
    metrics_rows: list[dict[str, object]],
    summary_rows: list[dict[str, object]],
    recommendation_rows: list[dict[str, object]],
    bandwidth: float,
) -> None:
    raw = next(row for row in metrics_rows if row["mode"] == "raw")
    loo = next(row for row in metrics_rows if row["mode"] == "calibrated_leave_one_out")
    ins = next(row for row in metrics_rows if row["mode"] == "calibrated_in_sample")

    lines = [
        "# Huawei5 Calibrated Cache-Hit Model",
        "",
        f"- Calibration bandwidth: `{bandwidth}` in `log2(shared_buffers_mb)` space.",
        "- Base model: trace replay predictions.",
        "- Calibration: per-stage smoothed residuals for SB hit rate and OS conditional hit rate.",
        "- Combined hit rate is recomputed from calibrated SB/OS rates.",
        "",
        "## Error",
        "",
        "| mode | SB MAE pp | OS MAE pp | combined MAE pp | recommendation matches |",
        "|---|---:|---:|---:|---:|",
        (
            f"| raw | {raw['sb_mae_pp']} | {raw['os_mae_pp']} | {raw['combined_mae_pp']} | "
            f"{raw['recommendation_matches']}/5 |"
        ),
        (
            f"| calibrated leave-one-out | {loo['sb_mae_pp']} | {loo['os_mae_pp']} | "
            f"{loo['combined_mae_pp']} | {loo['recommendation_matches']}/5 |"
        ),
        (
            f"| calibrated in-sample | {ins['sb_mae_pp']} | {ins['os_mae_pp']} | "
            f"{ins['combined_mae_pp']} | {ins['recommendation_matches']}/5 |"
        ),
        "",
        "## Tested-Point Recommendations",
        "",
        "| stage | actual best SB | raw best SB | calibrated best SB | raw matched | calibrated matched |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in summary_rows:
        lines.append(
            "| {stage} | {actual_best_sb_mb} | {raw_best_sb_mb} | {calibrated_best_sb_mb} | "
            "{raw_matched} | {calibrated_matched} |".format(**row)
        )

    lines += [
        "",
        "## Calibrated Grid Recommendations",
        "",
        "| stage | recommended SB | calibrated combined | config status |",
        "|---|---:|---:|---|",
    ]
    for row in recommendation_rows:
        lines.append(
            "| {stage} | {recommended_sb_mb} | {calibrated_combined} | {config_status} |".format(**row)
        )

    lines += [
        "",
        "## Files",
        "",
        f"- Calibrated validation rows: `{out_root / 'calibrated_validation_by_stage.csv'}`",
        f"- Calibrated all-grid predictions: `{out_root / 'calibrated_stage_predictions.csv'}`",
        f"- Metrics: `{out_root / 'calibrated_model_metrics.csv'}`",
        f"- Plot: `{out_root / 'calibrated_prediction_validation.png'}`",
    ]
    (out_root / "CALIBRATED_CACHE_HIT_MODEL.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep-root", required=True)
    parser.add_argument("--prediction-csv", required=True)
    parser.add_argument("--validation-csv", default="")
    parser.add_argument("--bandwidth", type=float, default=0.25)
    args = parser.parse_args()

    sweep_root = Path(args.sweep_root)
    validation_csv = Path(args.validation_csv) if args.validation_csv else sweep_root / "recommendation_validation_by_stage.csv"
    validation = read_csv(validation_csv)
    predictions = read_csv(Path(args.prediction_csv))
    calibrator = KernelResidualCalibrator(validation, args.bandwidth)
    tested_sbs = {int(float(row["sb_mb"])) for row in validation}
    pred_by_key = {
        (row["stage"], int(float(row["sb_mb"]))): row
        for row in predictions
    }

    validation_rows: list[dict[str, object]] = []
    loo_rows: list[dict[str, object]] = []
    for row in validation:
        stage = row["stage"]
        sb_mb = int(float(row["sb_mb"]))
        raw_sb = float(row["pred_sb"])
        raw_os = float(row["pred_os"])
        cal_sb, cal_os, cal_combined = calibrator.calibrate_pair(stage, sb_mb, raw_sb, raw_os)
        loo_sb, loo_os, loo_combined = calibrator.calibrate_pair(
            stage,
            sb_mb,
            raw_sb,
            raw_os,
            exclude_row=row,
        )
        actual_combined = float(row["actual_combined"])
        out = {
            "stage": stage,
            "sb_mb": sb_mb,
            "actual_sb": pct(row["actual_sb"]),
            "raw_sb": pct(raw_sb),
            "calibrated_sb": pct(cal_sb),
            "raw_sb_err_pp": pp(raw_sb - float(row["actual_sb"])),
            "calibrated_sb_err_pp": pp(cal_sb - float(row["actual_sb"])),
            "actual_os": pct(row["actual_os"]),
            "raw_os": pct(raw_os),
            "calibrated_os": pct(cal_os),
            "raw_os_err_pp": pp(raw_os - float(row["actual_os"])),
            "calibrated_os_err_pp": pp(cal_os - float(row["actual_os"])),
            "actual_combined": pct(actual_combined),
            "raw_combined": pct(row["pred_combined"]),
            "calibrated_combined": pct(cal_combined),
            "raw_combined_err_pp": pp(float(row["pred_combined"]) - actual_combined),
            "calibrated_combined_err_pp": pp(cal_combined - actual_combined),
            "loo_sb": pct(loo_sb),
            "loo_os": pct(loo_os),
            "loo_combined": pct(loo_combined),
        }
        validation_rows.append(out)
        loo = dict(out)
        loo["calibrated_sb"] = pct(loo_sb)
        loo["calibrated_os"] = pct(loo_os)
        loo["calibrated_combined"] = pct(loo_combined)
        loo_rows.append(loo)

    stage_order = list(dict.fromkeys(row["stage"] for row in predictions))
    validation_rows.sort(key=lambda row: (stage_order.index(str(row["stage"])), int(row["sb_mb"])))
    write_csv(sweep_root / "calibrated_validation_by_stage.csv", validation_rows)

    all_predictions: list[dict[str, object]] = []
    for pred in predictions:
        stage = pred["stage"]
        sb_mb = int(float(pred["sb_mb"]))
        raw_sb = float(pred["sb_hit_rate_pred"])
        raw_os = float(pred["os_cond_hit_rate_pred"])
        cal_sb, cal_os, cal_combined = calibrator.calibrate_pair(stage, sb_mb, raw_sb, raw_os)
        status = config_status(sweep_root, sb_mb, tested_sbs)
        row = dict(pred)
        row.update(
            {
                "config_status": status,
                "usable_for_recommendation": "yes" if usable_for_recommendation(status) else "no",
                "calibrated_sb_hit_rate": pct(cal_sb),
                "calibrated_os_cond_hit_rate": pct(cal_os),
                "calibrated_combined_hit_rate": pct(cal_combined),
            }
        )
        all_predictions.append(row)
    write_csv(sweep_root / "calibrated_stage_predictions.csv", all_predictions)

    raw_metric_rows = [
        {
            "stage": row["stage"],
            "sb_mb": row["sb_mb"],
            "actual_sb": row["actual_sb"],
            "actual_os": row["actual_os"],
            "actual_combined": row["actual_combined"],
            "raw_sb": row["raw_sb"],
            "raw_os": row["raw_os"],
            "raw_combined": row["raw_combined"],
            "calibrated_sb": row["raw_sb"],
            "calibrated_os": row["raw_os"],
            "calibrated_combined": row["raw_combined"],
        }
        for row in validation_rows
    ]
    raw_errors = metric_errors(raw_metric_rows, "raw")
    ins_errors = metric_errors(validation_rows, "calibrated")
    loo_errors = metric_errors(loo_rows, "calibrated")
    metrics_rows = [
        {
            "mode": "raw",
            "sb_mae_pp": pp(raw_errors["sb"]),
            "os_mae_pp": pp(raw_errors["os"]),
            "combined_mae_pp": pp(raw_errors["combined"]),
            "recommendation_matches": recommendation_count(raw_metric_rows, "raw_combined"),
        },
        {
            "mode": "calibrated_leave_one_out",
            "sb_mae_pp": pp(loo_errors["sb"]),
            "os_mae_pp": pp(loo_errors["os"]),
            "combined_mae_pp": pp(loo_errors["combined"]),
            "recommendation_matches": recommendation_count(loo_rows, "calibrated_combined"),
        },
        {
            "mode": "calibrated_in_sample",
            "sb_mae_pp": pp(ins_errors["sb"]),
            "os_mae_pp": pp(ins_errors["os"]),
            "combined_mae_pp": pp(ins_errors["combined"]),
            "recommendation_matches": recommendation_count(validation_rows, "calibrated_combined"),
        },
    ]
    write_csv(sweep_root / "calibrated_model_metrics.csv", metrics_rows)

    summary_rows: list[dict[str, object]] = []
    for stage in stage_order:
        sub = [row for row in validation_rows if row["stage"] == stage]
        if not sub:
            continue
        actual_best = max(sub, key=lambda row: float(row["actual_combined"]))
        raw_best = max(sub, key=lambda row: float(row["raw_combined"]))
        cal_best = max(sub, key=lambda row: float(row["calibrated_combined"]))
        summary_rows.append(
            {
                "stage": stage,
                "actual_best_sb_mb": int(actual_best["sb_mb"]),
                "raw_best_sb_mb": int(raw_best["sb_mb"]),
                "calibrated_best_sb_mb": int(cal_best["sb_mb"]),
                "actual_best_combined": actual_best["actual_combined"],
                "raw_best_combined": raw_best["raw_combined"],
                "calibrated_best_combined": cal_best["calibrated_combined"],
                "raw_matched": "yes" if int(actual_best["sb_mb"]) == int(raw_best["sb_mb"]) else "no",
                "calibrated_matched": "yes" if int(actual_best["sb_mb"]) == int(cal_best["sb_mb"]) else "no",
            }
        )
    write_csv(sweep_root / "calibrated_validation_summary.csv", summary_rows)

    recommendation_rows: list[dict[str, object]] = []
    for stage in stage_order:
        sub = [
            row for row in all_predictions
            if row["stage"] == stage and row["usable_for_recommendation"] == "yes"
        ]
        if not sub:
            continue
        best = max(sub, key=lambda row: float(row["calibrated_combined_hit_rate"]))
        recommendation_rows.append(
            {
                "stage": stage,
                "recommended_sb_mb": int(float(best["sb_mb"])),
                "calibrated_combined": best["calibrated_combined_hit_rate"],
                "calibrated_sb": best["calibrated_sb_hit_rate"],
                "calibrated_os": best["calibrated_os_cond_hit_rate"],
                "config_status": best["config_status"],
            }
        )
    write_csv(sweep_root / "calibrated_stage_recommendations.csv", recommendation_rows)

    render_plot(sweep_root, validation_rows)
    write_report(
        sweep_root,
        validation_rows,
        metrics_rows,
        summary_rows,
        recommendation_rows,
        args.bandwidth,
    )
    print(sweep_root / "CALIBRATED_CACHE_HIT_MODEL.md")
    print(sweep_root / "calibrated_validation_by_stage.csv")
    print(sweep_root / "calibrated_stage_predictions.csv")
    print(sweep_root / "calibrated_model_metrics.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
