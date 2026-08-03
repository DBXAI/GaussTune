#!/usr/bin/env python3
"""Convert AP8 TP-only replay hits to TPS and evaluate holdout SB points."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib.pyplot as plt


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = math.sqrt(
        sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys)
    )
    return numerator / denominator if denominator else None


def fit_line(xs: list[float], ys: list[float]) -> tuple[float, float]:
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    return y_mean - slope * x_mean, slope


def hit_feature(hit_rate: float) -> float:
    return -math.log(max(1e-6, 1.0 - hit_rate))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay-csv", required=True, type=Path)
    parser.add_argument("--actual-root", required=True, type=Path)
    parser.add_argument("--historical-csv", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--anchor-sb-mb", type=int, default=1504)
    parser.add_argument("--no-ap-ceiling-tps", type=float, default=1324.392697)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    historical = [
        row
        for row in read_csv(args.historical_csv)
        if row["stage"] == "stage5_tp_surge"
    ]
    train_x = [hit_feature(float(row["sb_hit_rate"])) for row in historical]
    train_y = [float(row["tps"]) for row in historical]
    intercept, slope = fit_line(train_x, train_y)

    actual_by_sb = {}
    for path in args.actual_root.glob("sb*mb/stage_tps.csv"):
        row = read_csv(path)[0]
        actual_by_sb[int(row["sb_mb"])] = row

    replay_rows = sorted(read_csv(args.replay_csv), key=lambda row: int(row["sb_mb"]))
    raw_predictions = {
        int(row["sb_mb"]): intercept + slope * hit_feature(float(row["tp_sb_hit_rate"]))
        for row in replay_rows
    }
    anchor_actual = float(actual_by_sb[args.anchor_sb_mb]["tps"])
    anchor_raw = raw_predictions[args.anchor_sb_mb]
    scale = anchor_actual / anchor_raw

    rows = []
    for replay in replay_rows:
        sb_mb = int(replay["sb_mb"])
        actual = float(actual_by_sb[sb_mb]["tps"])
        predicted = min(args.no_ap_ceiling_tps, max(0.0, raw_predictions[sb_mb] * scale))
        rows.append(
            {
                "sb_mb": sb_mb,
                "tp_sb_hit_rate_pred": replay["tp_sb_hit_rate"],
                "tps_pred": f"{predicted:.6f}",
                "tps_actual": f"{actual:.6f}",
                "abs_error_tps": f"{abs(predicted - actual):.6f}",
                "ape_pct": f"{abs(predicted - actual) / actual * 100:.3f}",
                "is_anchor": "yes" if sb_mb == args.anchor_sb_mb else "no",
            }
        )

    output_csv = args.out_dir / "ap8_trace_tps_prediction_vs_actual.csv"
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    holdout = [row for row in rows if row["is_anchor"] == "no"]
    predicted_values = [float(row["tps_pred"]) for row in holdout]
    actual_values = [float(row["tps_actual"]) for row in holdout]
    mae = sum(abs(pred - actual) for pred, actual in zip(predicted_values, actual_values)) / len(holdout)
    mape = sum(abs(pred - actual) / actual for pred, actual in zip(predicted_values, actual_values)) / len(holdout) * 100
    predicted_best = max(rows, key=lambda row: float(row["tps_pred"]))
    actual_best = max(rows, key=lambda row: float(row["tps_actual"]))
    metrics = {
        "training_workload": "historical AP4 saturated 32-terminal sweep",
        "target_workload": "AP8 saturated 32-terminal sweep",
        "anchor_sb_mb": args.anchor_sb_mb,
        "holdout_points": len(holdout),
        "holdout_mae_tps": mae,
        "holdout_mape_pct": mape,
        "holdout_pearson": pearson(predicted_values, actual_values),
        "predicted_best_sb_mb": int(predicted_best["sb_mb"]),
        "actual_best_sb_mb": int(actual_best["sb_mb"]),
        "historical_fit_intercept": intercept,
        "historical_fit_slope": slope,
        "anchor_scale": scale,
        "no_ap_ceiling_tps": args.no_ap_ceiling_tps,
    }
    (args.out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")

    sbs = [int(row["sb_mb"]) for row in rows]
    predicted = [float(row["tps_pred"]) for row in rows]
    actual = [float(row["tps_actual"]) for row in rows]
    fig, ax = plt.subplots(figsize=(10.8, 5.7))
    ax.plot(sbs, actual, color="#202A35", marker="o", linewidth=2.6, label="Actual AP8 TPS")
    ax.plot(sbs, predicted, color="#D85140", marker="s", linestyle="--", linewidth=2.5, label="Predicted TPS")
    ax.axvline(args.anchor_sb_mb, color="#138A86", linestyle=":", linewidth=1.8, label="1504MB anchor")
    ax.axhline(args.no_ap_ceiling_tps, color="#7D8792", linestyle="--", linewidth=1.2, label="No-AP ceiling")
    ax.set_xscale("log", base=2)
    ax.set_xticks(sbs, [str(value) for value in sbs], rotation=30)
    ax.set_xlabel("shared_buffers (MB)")
    ax.set_ylabel("TPS")
    ax.set_title(
        "AP8 workload: trace-based TPS prediction versus actual\n"
        "Shape trained on AP4; only 1504MB target point is used as an anchor",
        fontsize=14,
        fontweight="bold",
    )
    ax.grid(alpha=0.2)
    ax.legend(frameon=False)
    for x, value in zip(sbs, actual):
        ax.annotate(f"{value:.0f}", (x, value), xytext=(0, 8), textcoords="offset points", ha="center", fontsize=8)
    for x, value in zip(sbs, predicted):
        ax.annotate(f"{value:.0f}", (x, value), xytext=(0, -15), textcoords="offset points", ha="center", fontsize=8, color="#D85140")
    fig.tight_layout()
    chart = args.out_dir / "ap8_trace_tps_prediction_vs_actual.png"
    fig.savefig(chart, dpi=200, bbox_inches="tight")
    fig.savefig(chart.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)

    print(output_csv)
    print(chart)
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
