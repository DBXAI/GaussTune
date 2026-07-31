#!/usr/bin/env python3
"""Compare TP-only replay signals with original-load TP performance."""

from __future__ import annotations

import csv
import json
import math
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIR = ROOT / "results" / "tp_only_all_stages_20260716"
PERF_CSV = (
    ROOT
    / "results"
    / "sb_recommendation_validation_20260711_234537"
    / "original_load_stage_tp_latency.csv"
)
VALIDATION_DIR = PERF_CSV.parent
OUT_DIR = ROOT / "results" / "tp_only_performance_alignment_20260716"
ARTIFACTS = ROOT / "artifacts"

STAGES = [
    ("stage1_memory_rich", "S1"),
    ("stage2_reach_limit", "S2"),
    ("stage3_protect_tp", "S3"),
    ("stage4_backpressure", "S4"),
    ("stage5_tp_surge", "S5"),
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    x_var = sum((x - x_mean) ** 2 for x in xs)
    y_var = sum((y - y_mean) ** 2 for y in ys)
    denominator = math.sqrt(x_var * y_var)
    return numerator / denominator if denominator else None


def rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    pos = 0
    while pos < len(order):
        end = pos + 1
        while end < len(order) and values[order[end]] == values[order[pos]]:
            end += 1
        average_rank = (pos + end - 1) / 2 + 1
        for idx in order[pos:end]:
            ranks[idx] = average_rank
        pos = end
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return pearson(rank(xs), rank(ys))


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def stage5_high_tps_by_sb() -> dict[int, float]:
    timestamp_format = "%Y-%m-%d %H:%M:%S"
    throughput_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}.*Throughput: ([0-9.]+) txn/sec"
    )
    values_by_sb: dict[int, float] = {}
    for run_dir in sorted(VALIDATION_DIR.glob("sb*mb")):
        match = re.fullmatch(r"sb(\d+)mb", run_dir.name)
        boundaries_path = run_dir / "boundaries.csv"
        log_path = run_dir / "tpcc_high.log"
        if not match or not boundaries_path.exists() or not log_path.exists():
            continue
        boundaries = {row["label"]: row["wall_time"] for row in read_csv(boundaries_path)}
        if "stage5_tp_surge_start" not in boundaries or "stage5_tp_surge_end" not in boundaries:
            continue
        start = datetime.strptime(boundaries["stage5_tp_surge_start"], timestamp_format)
        end = datetime.strptime(boundaries["stage5_tp_surge_end"], timestamp_format)
        samples = []
        with log_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                throughput_match = throughput_pattern.search(line)
                if not throughput_match:
                    continue
                timestamp = datetime.strptime(throughput_match.group(1), timestamp_format)
                if start < timestamp < end:
                    samples.append(float(throughput_match.group(2)))
        if samples:
            values_by_sb[int(match.group(1))] = sum(samples) / len(samples)
    return values_by_sb


def build_chart(point_rows: list[dict], summary_rows: list[dict], path: Path) -> None:
    by_stage: dict[str, list[dict]] = defaultdict(list)
    summary = {row["stage"]: row for row in summary_rows}
    for row in point_rows:
        by_stage[row["stage"]].append(row)

    colors = ["#138A86", "#2775B6", "#48A868", "#E38A27", "#D85140"]
    fig, axes = plt.subplots(2, 3, figsize=(13.0, 7.0))
    for ax, (stage, short), color in zip(axes.flat, STAGES, colors):
        rows = sorted(by_stage[stage], key=lambda row: int(row["sb_mb"]))
        sbs = [int(row["sb_mb"]) for row in rows]
        tp_sb = [float(row["tp_sb_hit_rate"]) * 100 for row in rows]
        ax.plot(sbs, tp_sb, color=color, marker="o", linewidth=2.2, label="TP SB hit")
        ax.set_xscale("log", base=2)
        ax.set_xticks(sbs, [str(value) for value in sbs], rotation=35)
        ax.set_ylabel("TP SB hit (%)", color=color)
        ax.tick_params(axis="y", labelcolor=color)
        ax.grid(alpha=0.18)

        right = ax.twinx()
        if stage == "stage5_tp_surge":
            performance = [float(row["total_tp_tps"]) for row in rows]
            right.plot(sbs, performance, color="#202A35", marker="s", linestyle="--", label="Actual total TP TPS")
            right.set_ylabel("actual total TP TPS")
            target_label = "TPS plateau"
        else:
            performance = [float(row["latency_p95_ms"]) for row in rows]
            right.plot(sbs, performance, color="#202A35", marker="s", linestyle="--", label="Actual p95")
            right.set_ylabel("actual TP p95 (ms)")
            target_label = "lowest p95"

        predicted = int(summary[stage]["predicted_tp_sb_knee_mb"])
        actual = int(summary[stage]["actual_target_sb_mb"])
        ax.axvline(predicted, color=color, linestyle="--", linewidth=1.5, label="TP-SB knee")
        ax.axvline(actual, color="#202A35", linestyle=":", linewidth=1.5, label=target_label)
        ax.set_title(f"{short}: predicted {predicted}MB / actual {actual}MB", fontweight="bold")

        handles, labels = ax.get_legend_handles_labels()
        right_handles, right_labels = right.get_legend_handles_labels()
        ax.legend(handles + right_handles, labels + right_labels, frameon=False, fontsize=7, loc="best")

    axes.flat[-1].axis("off")
    fig.suptitle(
        "TP-only replay: AP still affects cache state, only TP accesses are scored",
        fontsize=15,
        fontweight="bold",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(path, dpi=190)
    plt.close(fig)


def build_s5_chart(point_rows: list[dict], path: Path) -> None:
    rows = sorted(
        (row for row in point_rows if row["stage"] == "stage5_tp_surge"),
        key=lambda row: int(row["sb_mb"]),
    )
    labels = [str(row["sb_mb"]) for row in rows]
    positions = list(range(len(rows)))
    tp_sb = [float(row["tp_sb_hit_rate"]) * 100 for row in rows]
    total_tps = [float(row["total_tp_tps"]) for row in rows]
    plateau_index = labels.index("1024")

    fig, ax_hit = plt.subplots(figsize=(11.2, 5.8))
    ax_tps = ax_hit.twinx()
    ax_hit.axvspan(plateau_index - 0.18, len(rows) - 0.5, color="#EAF5EE", alpha=0.8, zorder=0)
    hit_line = ax_hit.plot(
        positions,
        tp_sb,
        color="#D85140",
        marker="o",
        markersize=8,
        linewidth=2.8,
        label="Predicted TP-SB hit rate",
        zorder=3,
    )[0]
    tps_line = ax_tps.plot(
        positions,
        total_tps,
        color="#202A35",
        marker="s",
        markersize=7,
        linewidth=2.5,
        linestyle="--",
        label="Actual total TP TPS",
        zorder=3,
    )[0]
    ax_hit.axvline(
        plateau_index,
        color="#138A86",
        linewidth=2.0,
        linestyle=":",
        label="Predicted and actual optimum: 1024MB",
        zorder=2,
    )

    for x, value in zip(positions, tp_sb):
        ax_hit.annotate(
            f"{value:.2f}%",
            (x, value),
            xytext=(0, 10),
            textcoords="offset points",
            ha="center",
            fontsize=9,
            color="#D85140",
        )
    for x, value in zip(positions, total_tps):
        ax_tps.annotate(
            f"{value:.2f}",
            (x, value),
            xytext=(0, -18),
            textcoords="offset points",
            ha="center",
            fontsize=9,
            color="#202A35",
        )

    ax_hit.set_xticks(positions, labels)
    ax_hit.set_xlabel("shared_buffers (MB)", fontsize=12)
    ax_hit.set_ylabel("predicted TP-SB hit rate (%)", color="#D85140", fontsize=12)
    ax_tps.set_ylabel("actual total TP TPS", color="#202A35", fontsize=12)
    ax_hit.tick_params(axis="y", labelcolor="#D85140")
    ax_tps.tick_params(axis="y", labelcolor="#202A35")
    ax_hit.set_ylim(82, 102)
    ax_tps.set_ylim(115, 230)
    ax_hit.grid(axis="both", alpha=0.2)
    ax_hit.set_title(
        "Stage 5: TP-only replay signal versus actual total TP throughput",
        fontsize=16,
        fontweight="bold",
        pad=16,
    )
    ax_hit.text(
        plateau_index + 0.12,
        83.2,
        "TPS plateau",
        color="#138A86",
        fontsize=10,
        fontweight="bold",
    )
    handles = [hit_line, tps_line, ax_hit.lines[-1]]
    ax_hit.legend(handles=handles, frameon=False, loc="center right", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=200, bbox_inches="tight")
    fig.savefig(path.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    perf_by_stage: dict[str, dict[int, dict[str, str]]] = defaultdict(dict)
    for row in read_csv(PERF_CSV):
        perf_by_stage[row["stage"]][int(row["sb_mb"])] = row
    high_tps = stage5_high_tps_by_sb()

    point_rows: list[dict] = []
    summary_rows: list[dict] = []
    for stage, short in STAGES:
        replay_path = REPLAY_DIR / f"{stage}_tp_only_predictions.csv"
        metrics_path = REPLAY_DIR / f"{stage}_tp_only_metrics.json"
        replay_rows = read_csv(replay_path)
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        perf_rows = perf_by_stage[stage]

        for replay in replay_rows:
            sb_mb = int(replay["sb_mb"])
            perf = perf_rows[sb_mb]
            low_tps = float(perf["tps"])
            stage5_high_tps = high_tps.get(sb_mb, 0.0) if stage == "stage5_tp_surge" else 0.0
            point_rows.append(
                {
                    "stage": stage,
                    "stage_short": short,
                    "sb_mb": sb_mb,
                    "tp_sb_hit_rate": replay["tp_sb_hit_rate"],
                    "tp_os_cond_hit_rate": replay["tp_os_cond_hit_rate"],
                    "tp_combined_hit_rate": replay["tp_combined_hit_rate"],
                    "tps": perf["tps"],
                    "high_tp_tps": stage5_high_tps,
                    "total_tp_tps": low_tps + stage5_high_tps,
                    "latency_p95_ms": perf["latency_p95_ms"],
                }
            )

        stage_points = [row for row in point_rows if row["stage"] == stage]
        stage_points.sort(key=lambda row: int(row["sb_mb"]))
        knee = int(metrics["tp_sb_99pct_knee_mb"])
        p95_best = min(stage_points, key=lambda row: float(row["latency_p95_ms"]))
        if stage == "stage5_tp_surge":
            max_tps = max(float(row["total_tp_tps"]) for row in stage_points)
            target = next(row for row in stage_points if float(row["total_tp_tps"]) >= max_tps * 0.99)
            target_basis = "first_sb_at_99pct_max_total_tp_tps"
        else:
            target = p95_best
            target_basis = "lowest_p95_tps_rate_limited"

        knee_point = next(row for row in stage_points if int(row["sb_mb"]) == knee)
        tp_sb = [float(row["tp_sb_hit_rate"]) for row in stage_points]
        tp_combined = [float(row["tp_combined_hit_rate"]) for row in stage_points]
        tps = [float(row["total_tp_tps"]) for row in stage_points]
        p95 = [float(row["latency_p95_ms"]) for row in stage_points]
        p95_best_value = float(p95_best["latency_p95_ms"])
        knee_p95 = float(knee_point["latency_p95_ms"])

        summary_rows.append(
            {
                "stage": stage,
                "stage_short": short,
                "predicted_tp_sb_knee_mb": knee,
                "actual_target_sb_mb": int(target["sb_mb"]),
                "actual_target_basis": target_basis,
                "exact_match": "yes" if knee == int(target["sb_mb"]) else "no",
                "configuration_ratio": knee / int(target["sb_mb"]),
                "p95_best_sb_mb": int(p95_best["sb_mb"]),
                "p95_best_ms": p95_best_value,
                "p95_at_predicted_knee_ms": knee_p95,
                "p95_regret_pct": (knee_p95 / p95_best_value - 1) * 100,
                "tps_at_predicted_knee": float(knee_point["total_tp_tps"]),
                "tp_sb_vs_tps_pearson": pearson(tp_sb, tps),
                "tp_sb_vs_tps_spearman": spearman(tp_sb, tps),
                "tp_sb_vs_p95_pearson": pearson(tp_sb, p95),
                "tp_sb_vs_p95_spearman": spearman(tp_sb, p95),
                "tp_combined_range_pp": (max(tp_combined) - min(tp_combined)) * 100,
            }
        )

    point_fields = [
        "stage",
        "stage_short",
        "sb_mb",
        "tp_sb_hit_rate",
        "tp_os_cond_hit_rate",
        "tp_combined_hit_rate",
        "tps",
        "high_tp_tps",
        "total_tp_tps",
        "latency_p95_ms",
    ]
    summary_fields = list(summary_rows[0].keys())
    write_csv(OUT_DIR / "tp_only_performance_points.csv", point_rows, point_fields)
    write_csv(OUT_DIR / "tp_only_performance_summary.csv", summary_rows, summary_fields)

    chart_path = ARTIFACTS / "tp_only_replay_performance_alignment_20260716.png"
    build_chart(point_rows, summary_rows, chart_path)
    s5_chart_path = ARTIFACTS / "s5_tp_sb_hit_vs_total_tps_20260716.png"
    build_s5_chart(point_rows, s5_chart_path)

    exact_count = sum(row["exact_match"] == "yes" for row in summary_rows)
    result = {
        "exact_target_matches": exact_count,
        "stage_count": len(summary_rows),
        "s5_tp_sb_vs_tps_pearson": next(
            row["tp_sb_vs_tps_pearson"] for row in summary_rows if row["stage"] == "stage5_tp_surge"
        ),
        "s5_predicted_tps_plateau_mb": next(
            row["predicted_tp_sb_knee_mb"] for row in summary_rows if row["stage"] == "stage5_tp_surge"
        ),
        "s5_actual_tps_plateau_mb": next(
            row["actual_target_sb_mb"] for row in summary_rows if row["stage"] == "stage5_tp_surge"
        ),
        "tp_combined_is_flat": all(float(row["tp_combined_range_pp"]) < 1e-9 for row in summary_rows),
        "scope_note": "S1-S4 TPS is rate-limited near 40; their target uses minimum p95, not a TPS maximum.",
    }
    (OUT_DIR / "metrics.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(OUT_DIR / "tp_only_performance_summary.csv")
    print(chart_path)
    print(s5_chart_path)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
