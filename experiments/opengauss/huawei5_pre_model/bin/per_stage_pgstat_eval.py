#!/usr/bin/env python3
"""Per-stage pg_stat-based OS/SB validation + effect plot for Huawei5.

This script reuses the per-stage prediction pipeline that is normally driven
by ``cache_hit_stage_eval.py --stage-predictions`` (which also runs the
workload). Here we only consume an already-recorded run:

- read ``boundaries.csv`` and ``run_config.json`` from a result directory
- split the gzip trace once into per-stage traces (warmup + measurement)
- for each of the five stages, compute the actual SB/OS hit rates and invoke
  the Huawei4 model on the per-stage trace
- write per-stage CSVs and an effect plot that matches the global plot style

Outputs (under ``<result_dir>/stages_eval``):
- ``<stage>/trace.log``
- ``<stage>/predictions_<strategy>.csv``
- ``<stage>/best_predictions_<strategy>.csv``
- ``<stage>/accuracy_<strategy>.csv``
- ``<stage>/measurements.csv``
- ``<stage>/<stage>_prediction_effect.png`` + ``.svg``
- ``PER_STAGE_PGSTAT_EVALUATION.md``
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import cache_hit_stage_eval as stage_eval  # noqa: E402
from cache_hit_stage_eval import (  # noqa: E402
    actual_row,
    write_csv,
)


HUAWEI4_MODEL = Path(os.environ.get("HUAWEI4_MODEL", PACKAGE_ROOT / "bin" / "dual_cache_warmup.py"))


STAGE_DEFS = [
    ("stage1_memory_rich", "pre_tp_low_start", "stage1_memory_rich_start", "stage1_memory_rich_end"),
    ("stage2_reach_limit", "stage1_memory_rich_start", "stage2_reach_limit_start", "stage2_reach_limit_end"),
    ("stage3_protect_tp", "stage2_reach_limit_start", "stage3_protect_tp_start", "stage3_protect_tp_end"),
    ("stage4_backpressure", "stage3_protect_tp_start", "stage4_backpressure_start", "stage4_backpressure_end"),
    ("stage5_tp_surge", "stage4_backpressure_start", "stage5_tp_surge_start", "stage5_tp_surge_end"),
]


def open_trace_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", errors="replace", encoding="utf-8")
    return path.open("r", errors="replace", encoding="utf-8")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def best_by_full_error(predictions: Path) -> dict[str, str]:
    rows = read_rows(predictions)
    if not rows:
        raise RuntimeError(f"empty prediction file: {predictions}")

    def score(row: dict[str, str]) -> float:
        sb = abs(float(row.get("sb_err_pp", "0") or 0.0))
        os = abs(float(row.get("os_err_pp", "0") or 0.0))
        combined = abs(float(row.get("combined_err_pp", "0") or 0.0))
        return sb + os + 0.5 * combined

    return min(rows, key=score)


def split_all_stages_once(
    trace_path: Path,
    out_dir: Path,
    stage_specs: list[tuple[str, int, int, int, int]],
) -> dict[str, int]:
    """Read the full gzip trace once and write per-stage split traces.

    ``stage_specs`` is a list of (stage, warm_start_ns, warm_end_ns,
    measure_start_ns, measure_end_ns). The function writes
    ``<out_dir>/<stage>/trace.log`` for each stage and returns the event count
    written per stage.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    writers: dict[str, list] = {}
    for stage, ws, we, ms, me in stage_specs:
        sd = out_dir / stage
        sd.mkdir(parents=True, exist_ok=True)
        fh = (sd / "trace.log").open("w", encoding="utf-8")
        fh.write(f"# split warm=[{ws},{we}) measure=[{ms},{me})\n")
        writers[stage] = [fh, ws, we, ms, me, 0]

    counts: dict[str, int] = {s: 0 for s, *_ in stage_specs}
    try:
        with open_trace_text(trace_path) as src:
            for line in src:
                if not (line.startswith("SB,") or line.startswith("OS,") or line.startswith("OS_SUM,")):
                    continue
                try:
                    ts = stage_eval.trace_ts_ns(line)
                except ValueError:
                    continue
                if ts is None:
                    continue
                for stage, entry in writers.items():
                    fh, ws, we, ms, me, c = entry
                    if (ws <= ts < we) or (ms <= ts < me):
                        parts = line.rstrip("\n").split(",")
                        if parts[0] == "SB":
                            parts[4] = str(max(0, int(parts[4]) - ws))
                        elif parts[0] == "OS":
                            parts[5] = str(max(0, int(parts[5]) - ws))
                        elif parts[0] == "OS_SUM":
                            parts[1] = str(max(0, int(parts[1]) - ws))
                        fh.write(",".join(parts) + "\n")
                        entry[5] = c + 1
    finally:
        for entry in writers.values():
            entry[0].close()
    for stage, entry in writers.items():
        counts[stage] = entry[5]
    return counts


def count_measurement_trace_os(trace_path: Path, warm_seconds: float, measure_seconds: float) -> tuple[int, int]:
    first_ts: int | None = None
    os_events = 0
    os_bytes = 0
    os_sum_samples: list[tuple[int, int, int]] = []
    warm_ns = int(warm_seconds * 1_000_000_000)
    end_ns = int((warm_seconds + measure_seconds) * 1_000_000_000)
    with open_trace_text(trace_path) as fh:
        for line in fh:
            if not (line.startswith("SB,") or line.startswith("OS,") or line.startswith("OS_SUM,")):
                continue
            try:
                ts = stage_eval.trace_ts_ns(line)
            except ValueError:
                continue
            if ts is None:
                continue
            if first_ts is None:
                first_ts = ts
            elapsed = ts - first_ts
            if not (warm_ns <= elapsed < end_ns):
                continue
            if line.startswith("OS,"):
                parts = line.rstrip("\n").split(",")
                if len(parts) >= 5:
                    try:
                        os_bytes += max(0, int(parts[4]))
                        os_events += 1
                    except ValueError:
                        continue
            elif line.startswith("OS_SUM,"):
                parts = line.rstrip("\n").split(",")
                if len(parts) >= 4:
                    try:
                        os_sum_samples.append((elapsed, int(parts[2]), int(parts[3])))
                    except ValueError:
                        continue
    if os_events == 0 and os_bytes == 0 and os_sum_samples:
        return stage_eval.os_sum_delta(os_sum_samples, warm_ns, end_ns)
    return os_events, os_bytes


def run_predict(
    stage_dir: Path,
    stage: str,
    strategy: str,
    trace_path: Path,
    measurements: Path,
    sb_mb: int,
    os_mb: int,
    warm_seconds: float,
    measure_seconds: float,
    models: str,
    readahead_grid: str,
    os_scale_grid: str,
    bulk_read_ring_kb: int,
    sample_every: int,
    sample_mode: str,
) -> dict[str, str]:
    output = stage_dir / f"predictions_{strategy}.csv"
    best = stage_dir / f"best_predictions_{strategy}.csv"
    accuracy = stage_dir / f"accuracy_{strategy}.csv"
    cmd = [
        "python3",
        str(HUAWEI4_MODEL),
        "predict",
        "--trace",
        str(trace_path),
        "--mode",
        stage,
        "--warmup-seconds",
        f"{warm_seconds:.3f}",
        "--measure-seconds",
        f"{measure_seconds:.3f}",
        "--sb-sizes",
        str(sb_mb),
        "--os-sizes",
        str(os_mb),
        "--models",
        models,
        "--measurements",
        str(measurements),
        "--pairs-from-measurements",
        "--output",
        str(output),
        "--best-output",
        str(best),
        "--accuracy-output",
        str(accuracy),
        "--tune",
        "--readahead-grid",
        readahead_grid,
        "--os-scale-grid",
        os_scale_grid,
        "--sb-strategy",
        strategy,
    ]
    if sample_every > 1:
        cmd += ["--sample-every", str(sample_every), "--sample-mode", sample_mode]
    if strategy == "bulk_ring":
        cmd += ["--bulk-read-ring-kb", str(bulk_read_ring_kb)]
    log_path = stage_dir / f"predict_{strategy}.log"
    with log_path.open("w", encoding="utf-8") as fh:
        subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True, check=True)
    row = best_by_full_error(output)
    row["strategy"] = strategy
    row["stage"] = stage
    row["prediction_file"] = str(output)
    row["accuracy_file"] = str(accuracy)
    return row


def write_stage_report(
    out_dir: Path,
    summary_rows: list[dict[str, object]],
    sample_every: int,
    sample_mode: str,
) -> None:
    report = out_dir / "PER_STAGE_PGSTAT_EVALUATION.md"
    lines = [
        "# Huawei5 Per-Stage pg_stat_database Evaluation",
        "",
        f"- Sample every: `{sample_every}`",
        f"- Sample mode: `{sample_mode}`",
        "- SB actual uses `pg_stat_database` deltas at stage boundaries.",
        "- OS actual uses measurement-window `pread64` bytes and block-device read bytes.",
        "- Each stage uses the previous stage's start as the warmup boundary so the model sees the prior workload as warmup traffic.",
        "",
        "## Best Per-Stage Predictions",
        "",
        "| Stage | Strategy | Model | RA | Scale | Actual SB | Pred SB | SB err pp | Actual OS | Pred OS | OS err pp | Actual combined | Pred combined | Combined err pp |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary_rows:
        display = dict(row)
        display.setdefault("stage", display.get("mode", ""))
        lines.append(
            "| {stage} | {strategy} | {model} | {readahead_pages} | {os_scale} | "
            "{meas_sb_hr:.6f} | {sb_hit_rate:.6f} | {sb_err_pp:+.2f} | "
            "{meas_os_hr:.6f} | {pred_os:.6f} | {os_err_pp:+.2f} | "
            "{meas_combined:.6f} | {combined_hit_rate:.6f} | {combined_err_pp:+.2f} |".format(
                **display
            )
        )
    lines += [
        "",
        "## Files",
        "",
        f"- Per-stage CSVs: `{out_dir}/<stage>/predictions_*.csv`, `best_predictions_*.csv`",
        f"- Per-stage plots: `{out_dir}/<stage>/<stage>_prediction_effect.png` + `.svg`",
    ]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_stage_plot(
    stage_dir: Path,
    stage: str,
    strategy: str,
    best_row: dict[str, str],
    all_rows: list[dict[str, str]],
    sample_every: int,
    sample_mode: str,
) -> None:
    """Render the per-stage effect plot matching the global style."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    actual_sb = float(best_row["meas_sb_hr"])
    actual_os = float(best_row["meas_os_hr"])
    actual_combined = float(best_row["meas_combined"])
    pred_sb = float(best_row["sb_hit_rate"])
    pred_os = float(best_row["physical_os_cond_hit_rate"])
    pred_combined = float(best_row["physical_combined_hit_rate"])
    sb_err = float(best_row["sb_err_pp"])
    os_err = float(best_row["os_err_pp"])
    combined_err = float(best_row["combined_err_pp"])

    sb_mb = int(best_row.get("sb_mb", 0))
    os_mb = int(best_row.get("os_mb", 0))
    os_scale = best_row.get("os_scale", "")
    ra = best_row.get("readahead_pages", "")
    model_name = best_row.get("model", "")
    sub = (
        f"Best: {strategy} + {model_name} | SB={sb_mb}MB | OS={os_mb}MB | "
        f"scale={os_scale} | RA={ra} | sample={sample_every}.{sample_mode} | "
        f"combined error={combined_err:+.2f} pp"
    )

    fig = plt.figure(figsize=(11, 7.2), facecolor="#f4f6f8")
    gs = fig.add_gridspec(2, 2, height_ratios=[1.05, 0.95], hspace=0.45, wspace=0.25)

    fig.suptitle(
        f"Huawei5 Per-Stage Prediction Result — {stage}",
        fontsize=16, fontweight="bold", y=0.97,
    )
    fig.text(0.5, 0.915, sub, ha="center", fontsize=10, color="#334")

    ax_top = fig.add_subplot(gs[0, :])
    categories = ["SB", "OS", "Combined"]
    actuals = [actual_sb * 100, actual_os * 100, actual_combined * 100]
    preds = [pred_sb * 100, pred_os * 100, pred_combined * 100]
    x = np.arange(len(categories))
    width = 0.35
    bars_a = ax_top.bar(x - width / 2, actuals, width, color="#2c3e50", label="Actual")
    bars_p = ax_top.bar(x + width / 2, preds, width, color="#27ae60", label="Predicted")
    for bar in list(bars_a) + list(bars_p):
        h = bar.get_height()
        ax_top.annotate(
            f"{h:.2f}%", xy=(bar.get_x() + bar.get_width() / 2, h),
            xytext=(0, 3), textcoords="offset points", ha="center", fontsize=10
        )
    ax_top.set_xticks(x)
    ax_top.set_xticklabels(categories, fontsize=11)
    ax_top.set_ylabel("Hit rate", fontsize=11)
    ax_top.set_title("Actual vs Predicted Hit Rates", fontsize=12, fontweight="bold")
    ax_top.set_ylim(0, 105)
    ax_top.set_yticks(np.arange(0, 101, 20))
    ax_top.set_yticklabels([f"{v}%" for v in range(0, 101, 20)])
    ax_top.legend(loc="upper right", frameon=False)
    ax_top.grid(axis="y", linestyle=":", alpha=0.4)
    ax_top.set_axisbelow(True)

    ax_err = fig.add_subplot(gs[1, 0])
    err_values = [sb_err, os_err, combined_err]
    colors_err = ["#5b8def" if v < 0 else "#e67e22" for v in err_values]
    bars_err = ax_err.bar(
        categories, err_values, color=colors_err, edgecolor="#222", linewidth=0.4, width=0.55
    )
    for bar, v in zip(bars_err, err_values):
        ax_err.annotate(
            f"{v:+.2f} pp", xy=(bar.get_x() + bar.get_width() / 2, v),
            xytext=(0, 3 if v >= 0 else -10), textcoords="offset points",
            ha="center", fontsize=10
        )
    ax_err.axhline(0, color="#222", linewidth=0.8)
    ax_err.set_ylabel("Prediction error (pp)", fontsize=11)
    ax_err.set_title("Best Model Error", fontsize=12, fontweight="bold")
    ax_err.set_ylim(-5, 5)
    ax_err.grid(axis="y", linestyle=":", alpha=0.4)
    ax_err.set_axisbelow(True)

    ax_cand = fig.add_subplot(gs[1, 1])
    by_model: dict[str, dict[str, str]] = {}
    for row in all_rows:
        m = row.get("model", "")
        by_model.setdefault(m, row)
    candidate_order = [m for m in ("cold", "warmup_miss", "warmup_full") if m in by_model]
    cand_x = np.arange(len(candidate_order))
    cand_width = 0.35
    cand_combined = [float(by_model[m]["combined_err_pp"]) for m in candidate_order]
    cand_os = [float(by_model[m]["os_err_pp"]) for m in candidate_order]
    bars_cb = ax_cand.bar(
        cand_x - cand_width / 2, cand_combined, cand_width,
        color="#27ae60", label="Combined", edgecolor="#222", linewidth=0.4
    )
    bars_co = ax_cand.bar(
        cand_x + cand_width / 2, cand_os, cand_width,
        color="#e67e22", label="OS", edgecolor="#222", linewidth=0.4
    )
    for bar in list(bars_cb) + list(bars_co):
        h = bar.get_height()
        ax_cand.annotate(
            f"{h:+.2f}", xy=(bar.get_x() + bar.get_width() / 2, h),
            xytext=(0, 3), textcoords="offset points", ha="center", fontsize=9
        )
    ax_cand.set_xticks(cand_x)
    ax_cand.set_xticklabels(candidate_order, fontsize=10, rotation=15)
    ax_cand.set_ylabel("Error (pp)", fontsize=11)
    ax_cand.set_title("Candidate Error Check", fontsize=12, fontweight="bold")
    ax_cand.legend(loc="upper left", frameon=False, fontsize=9)
    ax_cand.grid(axis="y", linestyle=":", alpha=0.4)
    ax_cand.set_axisbelow(True)

    fig.text(
        0.5, 0.015,
        "Source: per-stage best_predictions_<strategy>.csv and predictions_<strategy>.csv. "
        "OS uses physical_os_cond_hit_rate.",
        ha="center", fontsize=8, color="#666",
    )

    png_path = stage_dir / f"{stage}_prediction_effect.png"
    svg_path = stage_dir / f"{stage}_prediction_effect.svg"
    fig.savefig(png_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    fig.savefig(svg_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--strategies", default="clock,bulk_ring")
    parser.add_argument("--models", default="cold,warmup_miss,warmup_full")
    parser.add_argument("--readahead-grid", default="0")
    parser.add_argument("--os-scale-grid", default="0.5,0.75,1.0")
    parser.add_argument("--bulk-read-ring-kb", type=int, default=16 * 1024)
    parser.add_argument("--sample-every", type=int, default=64)
    parser.add_argument("--sample-mode", choices=["hash", "interval"], default="hash")
    parser.add_argument("--stages", default=",".join(s for s, _, _, _ in STAGE_DEFS))
    parser.add_argument("--skip-split", action="store_true",
                        help="Skip trace splitting (reuse existing per-stage trace.log files)")
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    out_dir = result_dir / "stages_eval"
    out_dir.mkdir(parents=True, exist_ok=True)

    boundaries = {row["label"]: row for row in read_rows(result_dir / "boundaries.csv")}
    missing = []
    for _stage, warm_label, start_label, end_label in STAGE_DEFS:
        for label in (warm_label, start_label, end_label):
            if label not in boundaries:
                missing.append(label)
    if missing:
        raise SystemExit(f"missing boundary label(s): {', '.join(sorted(set(missing)))}")

    config = json.loads((result_dir / "run_config.json").read_text(encoding="utf-8"))
    sb_mb = int(config["shared_buffers_mb"])
    trace_path = Path(config["trace"])
    if not trace_path.exists():
        raise SystemExit(f"trace file not found: {trace_path}")

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    stage_def_map = {s: (s, ws, ms, me) for s, ws, ms, me in STAGE_DEFS}
    selected = [stage_def_map[s] for s in stages if s in stage_def_map]

    stage_specs: list[tuple[str, int, int, int, int]] = []
    for stage, warm_start_label, measure_start_label, measure_end_label in selected:
        ws = int(boundaries[warm_start_label]["elapsed_ns"])
        we = int(boundaries[measure_start_label]["elapsed_ns"])
        ms = int(boundaries[measure_start_label]["elapsed_ns"])
        me = int(boundaries[measure_end_label]["elapsed_ns"])
        stage_specs.append((stage, ws, we, ms, me))

    if not args.skip_split:
        print(f"[stage-eval] splitting trace once for {len(stage_specs)} stages...", flush=True)
        counts = split_all_stages_once(trace_path, out_dir, stage_specs)
        for stage, count in counts.items():
            print(f"[stage-eval]   {stage}: {count:,} events", flush=True)

    summary: list[dict[str, object]] = []
    for stage, warm_start_label, measure_start_label, measure_end_label in selected:
        warm_start = boundaries[warm_start_label]
        measure_start = boundaries[measure_start_label]
        measure_end = boundaries[measure_end_label]
        warm_seconds = (int(measure_start["elapsed_ns"]) - int(warm_start["elapsed_ns"])) / 1e9
        measure_seconds = (int(measure_end["elapsed_ns"]) - int(measure_start["elapsed_ns"])) / 1e9
        stage_dir = out_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        split_trace = stage_dir / "trace.log"

        trace_os_events, trace_os_bytes = count_measurement_trace_os(
            split_trace, warm_seconds, measure_seconds
        )
        actual = actual_row(
            stage,
            sb_mb,
            split_trace,
            measure_start,
            measure_end,
            warm_seconds,
            measure_seconds,
            trace_os_events,
            trace_os_bytes,
        )
        measurements = stage_dir / "measurements.csv"
        write_csv(measurements, [actual])

        for strategy in strategies:
            best = run_predict(
                stage_dir,
                stage,
                strategy,
                split_trace,
                measurements,
                sb_mb,
                int(actual["os_cache_mb"]),
                warm_seconds,
                measure_seconds,
                args.models,
                args.readahead_grid,
                args.os_scale_grid,
                args.bulk_read_ring_kb,
                args.sample_every,
                args.sample_mode,
            )
            row = dict(actual)
            row.update(
                {
                    "strategy": strategy,
                    "model": best["model"],
                    "readahead_pages": best["readahead_pages"],
                    "os_scale": best["os_scale"],
                    "sb_hit_rate": float(best["sb_hit_rate"]),
                    "pred_os": float(best["physical_os_cond_hit_rate"]),
                    "combined_hit_rate": float(best["physical_combined_hit_rate"]),
                    "sb_err_pp": float(best["sb_err_pp"]),
                    "os_err_pp": float(best["os_err_pp"]),
                    "combined_err_pp": float(best["combined_err_pp"]),
                    "prediction_file": best["prediction_file"],
                    "accuracy_file": best["accuracy_file"],
                }
            )
            row["meas_sb_hr"] = float(row["meas_sb_hr"])
            row["meas_os_hr"] = float(row["meas_os_hr"])
            row["meas_combined"] = float(row["meas_combined"])
            summary.append(row)

            predictions_csv = stage_dir / f"predictions_{strategy}.csv"
            all_rows = read_rows(predictions_csv)
            render_stage_plot(
                stage_dir,
                stage,
                strategy,
                best,
                all_rows,
                args.sample_every,
                args.sample_mode,
            )
            print(
                f"[stage-eval] {stage}/{strategy}: combined_err={best['combined_err_pp']} pp "
                f"-> {stage_dir / f'{stage}_prediction_effect.png'}",
                flush=True,
            )

    write_stage_report(out_dir, summary, args.sample_every, args.sample_mode)
    print(out_dir / "PER_STAGE_PGSTAT_EVALUATION.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
