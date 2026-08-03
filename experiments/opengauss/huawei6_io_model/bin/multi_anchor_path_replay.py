#!/usr/bin/env python3
"""Execution-path-aware multi-anchor cache replay evaluation.

The original trace replay is retained as a prior/fallback.  When detailed
measurements from multiple anchor SB runs are available, this model predicts
four path rates as functions of log2(SB): database hits, database reads,
database pread bytes, and block-device read bytes.  Cache hit rates are then
derived from those predicted flows instead of correcting the final combined
hit rate directly.

Rows from --test-sbs are never used to construct interpolation curves.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
from scipy.interpolate import PchipInterpolator


STAGE_ORDER = [
    "stage1_memory_rich",
    "stage2_reach_limit",
    "stage3_protect_tp",
    "stage4_backpressure",
    "stage5_tp_surge",
]

PATH_SIGNALS = (
    "blks_hit_delta",
    "blks_read_delta",
    "os_measure_bytes",
    "disk_read_bytes_delta",
)


def parse_sbs(value: str) -> set[int]:
    return {int(part.strip()) for part in value.split(",") if part.strip()}


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


def clamp_rate(value: float) -> float:
    return max(0.0, min(0.999999, value))


def combined(sb_hit: float, os_hit: float) -> float:
    return 1.0 - (1.0 - sb_hit) * (1.0 - os_hit)


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def parse_sb_dir(path: Path) -> int | None:
    name = path.name
    if not (name.startswith("sb") and name.endswith("mb")):
        return None
    try:
        return int(name[2:-2])
    except ValueError:
        return None


def load_measurements(root: Path) -> dict[str, dict[int, dict[str, str]]]:
    by_stage: dict[str, dict[int, dict[str, str]]] = defaultdict(dict)
    for path in sorted(root.glob("sb*mb/stage_measurements_continuous_actuals.csv")):
        sb_mb = parse_sb_dir(path.parent)
        if sb_mb is None:
            continue
        for row in read_csv(path):
            stage = row["mode"]
            row = dict(row)
            row["sb_mb"] = str(sb_mb)
            by_stage[stage][sb_mb] = row
    return by_stage


class LogPathInterpolator:
    """Shape-preserving interpolation of positive path rates over log2(SB)."""

    def __init__(self, points: list[tuple[int, float]], method: str) -> None:
        if len(points) < 2:
            raise ValueError("at least two anchor points are required")
        points = sorted(points)
        self.xs = [math.log2(sb_mb) for sb_mb, _ in points]
        self.ys = [math.log(max(value, 1e-12)) for _, value in points]
        self.method = method
        self.pchip = (
            PchipInterpolator(self.xs, self.ys, extrapolate=False)
            if method == "pchip" and len(points) >= 3
            else None
        )

    def predict(self, sb_mb: int) -> float:
        x = math.log2(sb_mb)
        # Extrapolation is deliberately conservative: retain the nearest
        # observed path state instead of extending a noisy edge slope.
        if x <= self.xs[0]:
            y = self.ys[0]
        elif x >= self.xs[-1]:
            y = self.ys[-1]
        elif self.pchip is not None:
            y = float(self.pchip(x))
        else:
            y = self._linear(x)
        return math.exp(y)

    def _linear(self, x: float) -> float:
        for index in range(len(self.xs) - 1):
            left_x = self.xs[index]
            right_x = self.xs[index + 1]
            if left_x <= x <= right_x:
                ratio = (x - left_x) / (right_x - left_x)
                return self.ys[index] * (1.0 - ratio) + self.ys[index + 1] * ratio
        raise AssertionError("interpolation point is outside the anchor range")


class StagePathReplay:
    def __init__(
        self,
        anchor_rows: dict[int, dict[str, str]],
        method: str,
    ) -> None:
        self.models: dict[str, LogPathInterpolator] = {}
        for signal in PATH_SIGNALS:
            points = []
            for sb_mb, row in anchor_rows.items():
                seconds = float(row["measure_seconds"])
                points.append((sb_mb, float(row[signal]) / seconds))
            self.models[signal] = LogPathInterpolator(points, method)

    def predict(self, sb_mb: int) -> dict[str, float]:
        rates = {signal: model.predict(sb_mb) for signal, model in self.models.items()}
        sb_hit = rates["blks_hit_delta"] / (
            rates["blks_hit_delta"] + rates["blks_read_delta"]
        )
        os_hit = clamp_rate(
            1.0 - rates["disk_read_bytes_delta"] / rates["os_measure_bytes"]
        )
        return {
            "path_sb": sb_hit,
            "path_os": os_hit,
            "path_combined": combined(sb_hit, os_hit),
            "path_db_events_per_s": rates["blks_hit_delta"] + rates["blks_read_delta"],
            "path_db_reads_per_s": rates["blks_read_delta"],
            "path_pread_mib_per_s": rates["os_measure_bytes"] / 1024.0 / 1024.0,
            "path_disk_mib_per_s": rates["disk_read_bytes_delta"] / 1024.0 / 1024.0,
        }


def component_mae(rows: list[dict[str, object]], prefix: str, component: str) -> float:
    return mean(
        [
            abs(float(row[f"{prefix}_{component}"]) - float(row[f"actual_{component}"]))
            for row in rows
        ]
    )


def recommendation_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    by_stage: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_stage[str(row["stage"])].append(row)

    output: list[dict[str, object]] = []
    for stage in STAGE_ORDER:
        stage_rows = by_stage.get(stage, [])
        if not stage_rows:
            continue
        actual_rank = sorted(
            stage_rows, key=lambda row: float(row["actual_combined"]), reverse=True
        )
        raw_rank = sorted(
            stage_rows, key=lambda row: float(row["raw_combined"]), reverse=True
        )
        path_rank = sorted(
            stage_rows, key=lambda row: float(row["path_combined"]), reverse=True
        )
        actual_best = actual_rank[0]
        raw_best = raw_rank[0]
        path_best = path_rank[0]
        actual_by_sb = {int(row["sb_mb"]): float(row["actual_combined"]) for row in stage_rows}
        actual_best_value = float(actual_best["actual_combined"])
        path_top2 = {int(row["sb_mb"]) for row in path_rank[:2]}
        output.append(
            {
                "stage": stage,
                "actual_best_sb_mb": int(actual_best["sb_mb"]),
                "raw_best_sb_mb": int(raw_best["sb_mb"]),
                "path_best_sb_mb": int(path_best["sb_mb"]),
                "raw_exact": int(raw_best["sb_mb"] == actual_best["sb_mb"]),
                "path_exact": int(path_best["sb_mb"] == actual_best["sb_mb"]),
                "path_top2": int(int(actual_best["sb_mb"]) in path_top2),
                "raw_regret_pp": 100.0
                * (actual_best_value - actual_by_sb[int(raw_best["sb_mb"])]),
                "path_regret_pp": 100.0
                * (actual_best_value - actual_by_sb[int(path_best["sb_mb"])]),
            }
        )
    return output


def plot_results(
    all_rows: list[dict[str, object]],
    anchor_sbs: set[int],
    test_sbs: set[int],
    path: Path,
) -> None:
    by_stage: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in all_rows:
        by_stage[str(row["stage"])].append(row)

    fig, axes = plt.subplots(3, 2, figsize=(13, 13), sharex=True)
    for index, stage in enumerate(STAGE_ORDER):
        ax = axes.flat[index]
        rows = sorted(by_stage[stage], key=lambda row: int(row["sb_mb"]))
        xs = [int(row["sb_mb"]) for row in rows]
        actual = [float(row["actual_combined"]) for row in rows]
        raw = [float(row["raw_combined"]) for row in rows]
        path_pred = [float(row["path_combined"]) for row in rows]
        ax.plot(xs, actual, "o-", label="actual", color="#202124", linewidth=2)
        ax.plot(xs, raw, "s--", label="raw trace replay", color="#c23b22")
        ax.plot(xs, path_pred, "^-", label="multi-anchor path replay", color="#1976a3")
        for sb_mb in anchor_sbs:
            ax.axvline(sb_mb, color="#777777", alpha=0.08, linewidth=1)
        ax.scatter(
            [sb for sb in xs if sb in test_sbs],
            [actual[xs.index(sb)] for sb in xs if sb in test_sbs],
            facecolors="none",
            edgecolors="#2e7d32",
            s=90,
            linewidths=1.5,
            label="held-out actual" if index == 0 else None,
        )
        ax.set_xscale("log", base=2)
        ax.set_title(stage)
        ax.set_ylabel("combined hit rate")
        ax.grid(alpha=0.2)
    axes.flat[5].axis("off")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower right", bbox_to_anchor=(0.92, 0.08))
    fig.supxlabel("shared_buffers (MB, log2 scale)")
    fig.suptitle("Raw Trace Replay vs Multi-Anchor Execution-Path Replay", fontsize=15)
    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    fig.savefig(path, dpi=180)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validation-root", required=True, type=Path)
    parser.add_argument("--base-predictions", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--anchor-sbs", default="128,512,1504,4096")
    parser.add_argument("--test-sbs", default="256,1024,2048,8192")
    parser.add_argument("--interpolation", choices=("linear", "pchip"), default="pchip")
    args = parser.parse_args()

    anchor_sbs = parse_sbs(args.anchor_sbs)
    test_sbs = parse_sbs(args.test_sbs)
    overlap = anchor_sbs & test_sbs
    if overlap:
        raise SystemExit(f"anchor and test SB sets overlap: {sorted(overlap)}")

    base_rows = read_csv(args.base_predictions)
    base_by_key = {
        (row["stage"], int(float(row["sb_mb"]))): row for row in base_rows
    }
    measurements = load_measurements(args.validation_root)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    models: dict[str, StagePathReplay] = {}
    for stage in STAGE_ORDER:
        missing = anchor_sbs - set(measurements.get(stage, {}))
        if missing:
            raise SystemExit(f"{stage} is missing anchor measurements: {sorted(missing)}")
        anchor_rows = {sb: measurements[stage][sb] for sb in anchor_sbs}
        models[stage] = StagePathReplay(anchor_rows, args.interpolation)

    output_rows: list[dict[str, object]] = []
    for stage in STAGE_ORDER:
        available = sorted((anchor_sbs | test_sbs) & set(measurements[stage]))
        for sb_mb in available:
            base = base_by_key.get((stage, sb_mb))
            if base is None:
                raise SystemExit(f"missing raw replay prediction for {stage} SB={sb_mb}")
            actual = measurements[stage][sb_mb]
            path_pred = models[stage].predict(sb_mb)
            row: dict[str, object] = {
                "split": "anchor" if sb_mb in anchor_sbs else "test",
                "stage": stage,
                "sb_mb": sb_mb,
                "actual_sb": float(actual["meas_sb_hr"]),
                "actual_os": float(actual["meas_os_hr"]),
                "actual_combined": float(actual["meas_combined"]),
                "raw_sb": float(base["pred_sb"]),
                "raw_os": float(base["pred_os"]),
                "raw_combined": float(base["pred_combined"]),
            }
            row.update(path_pred)
            output_rows.append(row)

    test_rows = [row for row in output_rows if row["split"] == "test"]
    recommendations = recommendation_rows(test_rows)
    metrics = []
    for component in ("sb", "os", "combined"):
        metrics.append(
            {
                "component": component,
                "raw_mae_pp": 100.0 * component_mae(test_rows, "raw", component),
                "path_mae_pp": 100.0 * component_mae(test_rows, "path", component),
            }
        )

    rows_path = args.out_dir / "multi_anchor_path_predictions.csv"
    rec_path = args.out_dir / "multi_anchor_path_recommendations.csv"
    metrics_path = args.out_dir / "multi_anchor_path_metrics.csv"
    plot_path = args.out_dir / "multi_anchor_path_replay_validation.png"
    write_csv(rows_path, output_rows)
    write_csv(rec_path, recommendations)
    write_csv(metrics_path, metrics)
    plot_results(output_rows, anchor_sbs, test_sbs, plot_path)

    raw_exact = sum(int(row["raw_exact"]) for row in recommendations)
    path_exact = sum(int(row["path_exact"]) for row in recommendations)
    path_top2 = sum(int(row["path_top2"]) for row in recommendations)
    raw_regret = mean([float(row["raw_regret_pp"]) for row in recommendations])
    path_regret = mean([float(row["path_regret_pp"]) for row in recommendations])
    combined_metrics = next(row for row in metrics if row["component"] == "combined")

    lines = [
        "# Multi-Anchor Execution-Path Replay Evaluation",
        "",
        f"- Anchor SBs used for fitting: `{','.join(map(str, sorted(anchor_sbs)))}`",
        f"- Held-out SBs used only for evaluation: `{','.join(map(str, sorted(test_sbs)))}`",
        f"- Interpolation: `{args.interpolation}` over log2(SB), with log path rates",
        "- Path state: DB hit/read events per second, DB pread bytes per second, and block-device read bytes per second.",
        "- The original trace replay remains the baseline and fallback; no held-out actual value is used to build an interpolation curve.",
        "- Raw per-SB trace files had been deleted, so this is path-state replay rather than page-by-page multi-trace replay.",
        "",
        "## Held-Out Accuracy",
        "",
        "| component | raw MAE pp | path replay MAE pp |",
        "|---|---:|---:|",
    ]
    for row in metrics:
        lines.append(
            f"| {row['component']} | {float(row['raw_mae_pp']):.6f} | "
            f"{float(row['path_mae_pp']):.6f} |"
        )
    lines += [
        "",
        "## Held-Out Recommendation",
        "",
        "| stage | actual best | raw best | path best | path exact | path top-2 | path regret pp |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in recommendations:
        lines.append(
            "| {stage} | {actual_best_sb_mb} | {raw_best_sb_mb} | {path_best_sb_mb} | "
            "{path_exact} | {path_top2} | {path_regret_pp:.6f} |".format(**row)
        )
    lines += [
        "",
        f"- Combined MAE: `{float(combined_metrics['raw_mae_pp']):.6f} pp` -> "
        f"`{float(combined_metrics['path_mae_pp']):.6f} pp`",
        f"- Exact best-SB matches on held-out candidates: `{raw_exact}/5` -> `{path_exact}/5`",
        f"- Path replay top-2 matches: `{path_top2}/5`",
        f"- Mean recommendation regret: `{raw_regret:.6f} pp` -> `{path_regret:.6f} pp`",
        "",
        "## Interpretation",
        "",
        "The path replay fixes most of the stage5 small-SB OS-cache underestimation and the stage4 SB-benefit overestimation. "
        "This is retrospective validation on existing runs. Because the full result table was already inspected during model development, "
        "a new unseen SB sweep is still required for a definitive predictive claim.",
        "",
        f"- Predictions: `{rows_path}`",
        f"- Recommendations: `{rec_path}`",
        f"- Metrics: `{metrics_path}`",
        f"- Plot: `{plot_path}`",
    ]
    report_path = args.out_dir / "MULTI_ANCHOR_PATH_REPLAY_EVALUATION.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(report_path)
    print(metrics_path)
    print(rec_path)
    print(plot_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
