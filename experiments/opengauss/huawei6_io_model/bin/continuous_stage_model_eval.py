#!/usr/bin/env python3
"""Continuous five-stage Huawei5 cache-model evaluation.

This script replays one full trace once per strategy, keeps the modeled SB/OS
cache state across stage boundaries, and only resets counters by stage. It is
intended to replace the earlier per-stage split prediction where every stage
started from an empty SB simulator.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
from array import array
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent
HUAWEI4 = Path(os.environ.get("HUAWEI4_DIR", PACKAGE_ROOT / "bin"))
sys.path.insert(0, str(HUAWEI4))

import dual_cache_warmup as model  # noqa: E402


PAGE_SIZE_KB = 8
PAGE_SIZE_MB = PAGE_SIZE_KB / 1024.0
DEFAULT_READAHEAD_GRID = "0,4,16,64,128"
DEFAULT_OS_SCALE_GRID = "0.5,0.75,1,1.25,1.5,2"


STAGE_DEFS = [
    ("stage1_memory_rich", "stage1_memory_rich_start", "stage1_memory_rich_end"),
    ("stage2_reach_limit", "stage2_reach_limit_start", "stage2_reach_limit_end"),
    ("stage3_protect_tp", "stage3_protect_tp_start", "stage3_protect_tp_end"),
    ("stage4_backpressure", "stage4_backpressure_start", "stage4_backpressure_end"),
    ("stage5_tp_surge", "stage5_tp_surge_start", "stage5_tp_surge_end"),
]


def parse_list(text: str, cast):
    return [cast(x.strip()) for x in str(text).replace(";", ",").split(",") if x.strip()]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def open_trace_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", errors="replace", encoding="utf-8")
    return path.open("r", errors="replace", encoding="utf-8")


def stage_for_ts(ts: int, stage_ranges: dict[str, tuple[int, int]]) -> str | None:
    for stage, (start, end) in stage_ranges.items():
        if start <= ts < end:
            return stage
    return None


def os_sum_delta(samples: list[tuple[int, int, int]], start_ns: int, end_ns: int) -> tuple[int, int]:
    start_count = 0
    start_bytes = 0
    end_count = 0
    end_bytes = 0
    for ts, count, bytes_read in samples:
        if ts <= start_ns:
            start_count = count
            start_bytes = bytes_read
        if ts <= end_ns:
            end_count = count
            end_bytes = bytes_read
        else:
            break
    return max(0, end_count - start_count), max(0, end_bytes - start_bytes)


def load_boundaries(result_dir: Path) -> tuple[dict[str, dict[str, str]], dict[str, tuple[int, int]]]:
    rows = read_csv(result_dir / "boundaries.csv")
    by_label = {row["label"]: row for row in rows}
    ranges = {}
    for stage, start_label, end_label in STAGE_DEFS:
        ranges[stage] = (int(by_label[start_label]["elapsed_ns"]), int(by_label[end_label]["elapsed_ns"]))
    return by_label, ranges


def load_actuals(result_dir: Path) -> dict[str, dict[str, str]]:
    path = result_dir / "stage_measurements_corrected.csv"
    if path.exists():
        rows = read_csv(path)
    else:
        stage_measurements = [result_dir / "stages" / stage / "measurements.csv" for stage, _, _ in STAGE_DEFS]
        if all(path.exists() for path in stage_measurements):
            rows = []
            for path in stage_measurements:
                rows.extend(read_csv(path))
        else:
            rows = compute_actuals_from_full_trace(result_dir)
            write_csv(result_dir / "stage_measurements_continuous_actuals.csv", rows)
    return {row["mode"]: row for row in rows}


def count_stage_os_bytes(trace: Path, stage_ranges: dict[str, tuple[int, int]]) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"events": 0, "bytes": 0})
    os_sum_samples: list[tuple[int, int, int]] = []
    with open_trace_text(trace) as fh:
        for line in fh:
            if line.startswith("OS,"):
                parts = line.rstrip("\n").split(",")
                if len(parts) < 6:
                    continue
                try:
                    read_bytes = max(0, int(parts[4]))
                    ts = int(parts[5])
                except ValueError:
                    continue
                stage = stage_for_ts(ts, stage_ranges)
                if stage is None:
                    continue
                stats[stage]["events"] += 1
                stats[stage]["bytes"] += read_bytes
            elif line.startswith("OS_SUM,"):
                parts = line.rstrip("\n").split(",")
                if len(parts) < 4:
                    continue
                try:
                    os_sum_samples.append((int(parts[1]), int(parts[2]), int(parts[3])))
                except ValueError:
                    continue
    if not any(row["events"] or row["bytes"] for row in stats.values()) and os_sum_samples:
        for stage, (start, end) in stage_ranges.items():
            events, bytes_read = os_sum_delta(os_sum_samples, start, end)
            stats[stage]["events"] = events
            stats[stage]["bytes"] = bytes_read
    return stats


def compute_actuals_from_full_trace(result_dir: Path) -> list[dict[str, str]]:
    config = json.loads((result_dir / "run_config.json").read_text(encoding="utf-8"))
    trace = Path(config["trace"])
    boundaries, stage_ranges = load_boundaries(result_dir)
    os_stats = count_stage_os_bytes(trace, stage_ranges)
    rows: list[dict[str, str]] = []

    for stage, start_label, end_label in STAGE_DEFS:
        start = boundaries[start_label]
        end = boundaries[end_label]
        hit_delta = int(end["blks_hit"]) - int(start["blks_hit"])
        read_delta = int(end["blks_read"]) - int(start["blks_read"])
        total = hit_delta + read_delta
        sb_hr = hit_delta / total if total > 0 else 0.0
        disk_reads = max(0, int(end["disk_reads"]) - int(start["disk_reads"]))
        disk_sectors = max(
            0, int(end["disk_read_sectors"]) - int(start["disk_read_sectors"])
        )
        disk_bytes = disk_sectors * 512
        os_events = os_stats[stage]["events"]
        os_bytes = os_stats[stage]["bytes"]
        os_hr = 1.0
        if os_bytes > 0:
            os_hr = max(0.0, 1.0 - min(1.0, disk_bytes / os_bytes))
        combined = sb_hr + (1.0 - sb_hr) * os_hr
        rows.append(
            {
                "mode": stage,
                "sb_mb": str(config["shared_buffers_mb"]),
                "os_cache_mb": start["os_cache_mb"],
                "os_actual_cache_mb": start["os_cache_mb"],
                "warmup_seconds": "",
                "measure_seconds": f"{(int(end['elapsed_ns']) - int(start['elapsed_ns'])) / 1e9:.3f}",
                "sb_measure_events": str(total),
                "sb_metric": "pg_stat_database",
                "os_measure_events": str(os_events),
                "os_measure_bytes": str(os_bytes),
                "disk_reads_delta": str(disk_reads),
                "disk_read_requests_delta": str(disk_reads),
                "disk_read_sectors_delta": str(disk_sectors),
                "disk_read_bytes_delta": str(disk_bytes),
                "disk_metric": "bytes",
                "meas_sb_hr": f"{sb_hr:.6f}",
                "meas_os_hr": f"{os_hr:.6f}",
                "meas_combined": f"{combined:.6f}",
                "trace_file": str(trace),
                "blks_hit_delta": str(hit_delta),
                "blks_read_delta": str(read_delta),
            }
        )
    return rows


def load_full_sb_pages(trace: Path, sample_every: int = 1) -> tuple[array, list[tuple[int, int]]]:
    pages = array("Q")
    events: list[tuple[int, int]] = []
    with open_trace_text(trace) as fh:
        for line in fh:
            if not line.startswith("SB,"):
                continue
            parts = line.rstrip("\n").split(",")
            if len(parts) < 5:
                continue
            try:
                page_id = model.encode_page(int(parts[2]), int(parts[3]))
                ts = int(parts[4])
            except ValueError:
                continue
            if sample_every > 1 and model.page_hash(page_id) % sample_every != 0:
                continue
            pages.append(page_id)
            events.append((ts, page_id))
    return pages, events


def make_sb_sim(strategy: str, sb_pages: int, bulk_read_ring_kb: int):
    if strategy == "clock":
        return model.ClockSweepSimulator(sb_pages), ""
    if strategy == "bulk_ring":
        ring_pages = min(sb_pages, max(1, int(bulk_read_ring_kb / PAGE_SIZE_KB)))
        return model.BulkReadRingSimulator(ring_pages), ring_pages
    raise ValueError(f"unknown strategy: {strategy}")


def replay_sb(
    sb_events: list[tuple[int, int]],
    stage_ranges: dict[str, tuple[int, int]],
    sb_pages: int,
    strategy: str,
    bulk_read_ring_kb: int,
) -> tuple[dict[str, dict[str, int]], list[tuple[int, int, int, str | None]], int | str]:
    sim, ring_pages = make_sb_sim(strategy, sb_pages, bulk_read_ring_kb)
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"accesses": 0, "hits": 0, "misses": 0})
    misses: list[tuple[int, int, int, str | None]] = []

    for ts, page_id in sb_events:
        stage = stage_for_ts(ts, stage_ranges)
        hit, evicted = sim.access(page_id)
        if stage is not None:
            stats[stage]["accesses"] += 1
            if hit:
                stats[stage]["hits"] += 1
            else:
                stats[stage]["misses"] += 1
        if not hit:
            misses.append((ts, page_id, evicted, stage))

    return stats, misses, ring_pages


def make_os_cache(os_pages: int, readahead: int, readahead_lookup):
    return model.TwoListOSCache(
        os_pages,
        readahead_pages=readahead,
        tracked_filter=None,
        readahead_lookup=readahead_lookup,
    )


def resize_os_cache(cache, os_pages: int) -> None:
    cache.max_pages = max(0, int(os_pages))
    evict = getattr(cache, "_evict_if_needed", None)
    if evict is not None:
        evict()


def replay_os(
    miss_events: list[tuple[int, int, int, str | None]],
    stage_os_pages: dict[str, int],
    readahead: int,
    readahead_lookup,
    insert_evicted: bool,
) -> dict[str, dict[str, int]]:
    first_stage = STAGE_DEFS[0][0]
    cache = make_os_cache(stage_os_pages[first_stage], readahead, readahead_lookup)
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"hits": 0, "misses": 0, "disk_pages": 0}
    )
    current_stage = first_stage

    for _ts, page_id, evicted, stage in miss_events:
        if stage is not None and stage != current_stage:
            resize_os_cache(cache, stage_os_pages[stage])
            current_stage = stage
        count = stage is not None
        old_hits = cache.hits
        old_misses = cache.misses
        old_disk = cache.disk_pages
        if insert_evicted:
            cache.add_from_sb_eviction(evicted)
        cache.access(page_id, count=count)
        if count and stage is not None:
            stats[stage]["hits"] += cache.hits - old_hits
            stats[stage]["misses"] += cache.misses - old_misses
            stats[stage]["disk_pages"] += cache.disk_pages - old_disk

    return stats


def pct(value: float) -> str:
    return f"{value:.6f}"


def actual_float(actuals: dict[str, dict[str, str]], stage: str, key: str) -> float:
    return float(actuals[stage][key])


def build_predictions(
    result_dir: Path,
    strategies: list[str],
    readahead_grid: list[int],
    os_scale_grid: list[float],
    bulk_read_ring_kb: int,
    insert_evicted: bool,
    sample_every: int = 1,
    scale_bulk_ring_for_sampling: bool = False,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    config = json.loads((result_dir / "run_config.json").read_text(encoding="utf-8"))
    trace = Path(config["trace"])
    sb_mb = int(config["shared_buffers_mb"])
    sb_pages = int(sb_mb / PAGE_SIZE_MB)
    if sample_every > 1:
        sb_pages = max(1, sb_pages // sample_every)
    _boundaries, stage_ranges = load_boundaries(result_dir)
    actuals = load_actuals(result_dir)
    os_mb_by_stage = {stage: int(float(row["os_cache_mb"])) for stage, row in actuals.items()}

    print(f"[continuous] loading SB events (sample_every={sample_every})...", flush=True)
    pages, sb_events = load_full_sb_pages(trace, sample_every)
    print(f"[continuous] loaded {len(sb_events):,} sampled SB events", flush=True)
    readahead_index = model.ReadaheadIndex(pages)
    rows: list[dict[str, object]] = []
    best_rows: list[dict[str, object]] = []
    effective_bulk_read_ring_kb = bulk_read_ring_kb
    if scale_bulk_ring_for_sampling and sample_every > 1:
        effective_bulk_read_ring_kb = max(
            PAGE_SIZE_KB, int(bulk_read_ring_kb / sample_every)
        )
        print(
            f"[continuous] scaled bulk ring: {bulk_read_ring_kb}KB -> "
            f"{effective_bulk_read_ring_kb}KB",
            flush=True,
        )

    for strategy in strategies:
        sb_stats, miss_events, ring_pages = replay_sb(
            sb_events, stage_ranges, sb_pages, strategy, effective_bulk_read_ring_kb
        )
        print(f"[continuous] SB replay done for {strategy}: {len(miss_events):,} miss events", flush=True)
        for readahead in readahead_grid:
            for os_scale in os_scale_grid:
                stage_os_pages = {
                    stage: max(1, int((os_mb / PAGE_SIZE_MB / max(1, sample_every)) * os_scale))
                    for stage, os_mb in os_mb_by_stage.items()
                }
                os_stats = replay_os(
                    miss_events,
                    stage_os_pages,
                    readahead,
                    readahead_index.pages_after,
                    insert_evicted=insert_evicted,
                )
                for stage, _start_label, _end_label in STAGE_DEFS:
                    sb = sb_stats[stage]
                    os_stat = os_stats[stage]
                    sb_hr = sb["hits"] / sb["accesses"] if sb["accesses"] else 0.0
                    os_total = os_stat["hits"] + os_stat["misses"]
                    os_hr = os_stat["hits"] / os_total if os_total else 1.0
                    physical_os = 1.0
                    if sb["misses"] > 0:
                        physical_os = 1.0 - min(1.0, os_stat["disk_pages"] / sb["misses"])
                    combined = sb_hr + (1.0 - sb_hr) * physical_os
                    actual_sb = actual_float(actuals, stage, "meas_sb_hr")
                    actual_os = actual_float(actuals, stage, "meas_os_hr")
                    actual_combined = actual_float(actuals, stage, "meas_combined")
                    sb_err = (sb_hr - actual_sb) * 100.0
                    os_err = (physical_os - actual_os) * 100.0
                    combined_err = (combined - actual_combined) * 100.0
                    rows.append(
                        {
                            "mode": stage,
                            "strategy": strategy,
                            "model": "continuous_miss",
                            "sb_mb": sb_mb,
                            "os_mb": os_mb_by_stage[stage],
                            "os_scale": f"{os_scale:.4g}",
                            "os_effective_pages": stage_os_pages[stage],
                            "readahead_pages": readahead,
                            "bulk_read_ring_pages": ring_pages,
                            "sb_measure_events": sb["accesses"],
                            "sb_measure_misses": sb["misses"],
                            "os_hits": os_stat["hits"],
                            "os_misses": os_stat["misses"],
                            "disk_pages": os_stat["disk_pages"],
                            "sb_hit_rate": pct(sb_hr),
                            "os_cond_hit_rate": pct(os_hr),
                            "physical_os_cond_hit_rate": pct(physical_os),
                            "physical_combined_hit_rate": pct(combined),
                            "meas_sb_hr": pct(actual_sb),
                            "meas_os_hr": pct(actual_os),
                            "meas_combined": pct(actual_combined),
                            "sb_err_pp": f"{sb_err:.6f}",
                            "os_err_pp": f"{os_err:.6f}",
                            "combined_err_pp": f"{combined_err:.6f}",
                            "score_sb_os_combined": f"{abs(sb_err) + abs(os_err) + 0.5 * abs(combined_err):.6f}",
                            "score_os_combined": f"{abs(os_err) + 0.5 * abs(combined_err):.6f}",
                        }
                    )

    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["mode"]), str(row["strategy"]))].append(row)
    for key in sorted(grouped):
        best_rows.append(
            min(grouped[key], key=lambda row: float(row["score_sb_os_combined"]))
        )
    return rows, best_rows


def write_report(result_dir: Path, best_rows: list[dict[str, object]]) -> None:
    out = result_dir / "CONTINUOUS_OS_SB_EVALUATION.md"
    lines = [
        "# Huawei5 Continuous 5-Stage Cache-Model Evaluation",
        "",
        f"- Result directory: `{result_dir}`",
        "- The model is replayed once from the full trace start to the full trace end.",
        "- SB and OS cache simulator state is preserved across all five stages.",
        "- Counters are sliced by stage boundaries; cache state is not reset at a boundary.",
        "- Best rows use `abs(SB err) + abs(OS err) + 0.5 * abs(combined err)`.",
        "",
        "## Best Continuous Predictions",
        "",
        "| stage | strategy | ra | scale | actual SB | pred SB | SB err pp | actual OS | pred OS | OS err pp | actual combined | pred combined | combined err pp |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in best_rows:
        lines.append(
            "| {mode} | {strategy} | {readahead_pages} | {os_scale} | "
            "{meas_sb_hr} | {sb_hit_rate} | {sb_err:.2f} | "
            "{meas_os_hr} | {physical_os_cond_hit_rate} | {os_err:.2f} | "
            "{meas_combined} | {physical_combined_hit_rate} | {combined_err:.2f} |".format(
                **row,
                sb_err=float(row["sb_err_pp"]),
                os_err=float(row["os_err_pp"]),
                combined_err=float(row["combined_err_pp"]),
            )
        )

    lines.append("")
    for strategy in sorted({str(row["strategy"]) for row in best_rows}):
        rows = [row for row in best_rows if row["strategy"] == strategy]
        sb_mae = sum(abs(float(row["sb_err_pp"])) for row in rows) / len(rows)
        os_mae = sum(abs(float(row["os_err_pp"])) for row in rows) / len(rows)
        comb_mae = sum(abs(float(row["combined_err_pp"])) for row in rows) / len(rows)
        lines.append(
            f"- `{strategy}` continuous MAE: SB {sb_mae:.2f} pp, "
            f"OS {os_mae:.2f} pp, combined {comb_mae:.2f} pp."
        )

    lines += [
        "",
        "## Notes",
        "",
        "- This fixes the earlier validation bug where each stage started from an empty SB simulator.",
        "- The remaining SB error against `pg_stat_database` can still be large if the real database had hot shared buffers before tracing began.",
        "- A strict validation run should either restart openGauss before tracing or seed the simulator from a real initial shared-buffer snapshot.",
        "",
        "## Files",
        "",
        f"- Full prediction CSV: `{result_dir / 'continuous_predictions.csv'}`",
        f"- Best rows CSV: `{result_dir / 'continuous_best_predictions.csv'}`",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_continuous_stage_plots(
    result_dir: Path,
    best_rows: list[dict[str, object]],
    all_rows: list[dict[str, object]],
    sample_every: int,
) -> None:
    """Render per-stage effect plots from continuous predictions."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    plot_dir = result_dir / "continuous_plots"
    plot_dir.mkdir(parents=True, exist_ok=True)

    best_by_stage: dict[str, dict[str, object]] = {}
    for row in best_rows:
        best_by_stage[str(row["mode"])] = row

    for stage, best in best_by_stage.items():
        actual_sb = float(best["meas_sb_hr"])
        actual_os = float(best["meas_os_hr"])
        actual_combined = float(best["meas_combined"])
        pred_sb = float(best["sb_hit_rate"])
        pred_os = float(best["physical_os_cond_hit_rate"])
        pred_combined = float(best["physical_combined_hit_rate"])
        sb_err = float(best["sb_err_pp"])
        os_err = float(best["os_err_pp"])
        combined_err = float(best["combined_err_pp"])

        strategy = str(best.get("strategy", ""))
        ra = best.get("readahead_pages", "")
        os_scale = best.get("os_scale", "")
        sub = (
            f"Continuous | {strategy} | RA={ra} | scale={os_scale} | "
            f"sample={sample_every} | combined error={combined_err:+.2f} pp"
        )

        fig = plt.figure(figsize=(11, 7.2), facecolor="#f4f6f8")
        gs = fig.add_gridspec(2, 2, height_ratios=[1.05, 0.95], hspace=0.45, wspace=0.25)
        fig.suptitle(
            f"Huawei5 Continuous Per-Stage Prediction — {stage}",
            fontsize=16, fontweight="bold", y=0.97,
        )
        fig.text(0.5, 0.915, sub, ha="center", fontsize=10, color="#334")

        # Top: Actual vs Predicted
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
            ax_top.annotate(f"{h:.2f}%", xy=(bar.get_x() + bar.get_width() / 2, h),
                            xytext=(0, 3), textcoords="offset points", ha="center", fontsize=10)
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

        # Bottom left: Best Model Error
        ax_err = fig.add_subplot(gs[1, 0])
        err_values = [sb_err, os_err, combined_err]
        colors_err = ["#5b8def" if v < 0 else "#e67e22" for v in err_values]
        bars_err = ax_err.bar(categories, err_values, color=colors_err, edgecolor="#222", linewidth=0.4, width=0.55)
        for bar, v in zip(bars_err, err_values):
            ax_err.annotate(f"{v:+.2f} pp", xy=(bar.get_x() + bar.get_width() / 2, v),
                            xytext=(0, 3 if v >= 0 else -10), textcoords="offset points", ha="center", fontsize=10)
        ax_err.axhline(0, color="#222", linewidth=0.8)
        ax_err.set_ylabel("Prediction error (pp)", fontsize=11)
        ax_err.set_title("Best Config Error", fontsize=12, fontweight="bold")
        ax_err.set_ylim(-70, 70)
        ax_err.grid(axis="y", linestyle=":", alpha=0.4)
        ax_err.set_axisbelow(True)

        # Bottom right: Candidate Error Check (by os_scale)
        ax_cand = fig.add_subplot(gs[1, 1])
        stage_rows = [r for r in all_rows if str(r["mode"]) == stage and str(r["strategy"]) == strategy]
        best_ra = int(best["readahead_pages"])
        ra_rows = [r for r in stage_rows if int(r["readahead_pages"]) == best_ra]
        ra_rows.sort(key=lambda r: float(r["os_scale"]))
        if ra_rows:
            scales = [f"s={r['os_scale']}" for r in ra_rows]
            cand_combined = [float(r["combined_err_pp"]) for r in ra_rows]
            cand_os = [float(r["os_err_pp"]) for r in ra_rows]
            cand_x = np.arange(len(scales))
            cw = 0.35
            ax_cand.bar(cand_x - cw / 2, cand_combined, cw, color="#27ae60", label="Combined", edgecolor="#222", linewidth=0.4)
            ax_cand.bar(cand_x + cw / 2, cand_os, cw, color="#e67e22", label="OS", edgecolor="#222", linewidth=0.4)
            ax_cand.set_xticks(cand_x)
            ax_cand.set_xticklabels(scales, fontsize=8, rotation=30)
        ax_cand.set_ylabel("Error (pp)", fontsize=11)
        ax_cand.set_title("Candidate Error by OS Scale", fontsize=12, fontweight="bold")
        ax_cand.legend(loc="upper left", frameon=False, fontsize=9)
        ax_cand.grid(axis="y", linestyle=":", alpha=0.4)
        ax_cand.set_axisbelow(True)
        ax_cand.axhline(0, color="#222", linewidth=0.5)

        fig.text(0.5, 0.015,
                 "Continuous model: SB/OS cache state preserved across all 5 stages. "
                 "Counters sliced by stage boundaries.",
                 ha="center", fontsize=8, color="#666")

        png = plot_dir / f"{stage}_continuous_effect.png"
        svg = plot_dir / f"{stage}_continuous_effect.svg"
        fig.savefig(png, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
        fig.savefig(svg, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        print(f"[continuous] plot: {png}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--strategies", default="clock,bulk_ring")
    parser.add_argument("--readahead-grid", default=DEFAULT_READAHEAD_GRID)
    parser.add_argument("--os-scale-grid", default=DEFAULT_OS_SCALE_GRID)
    parser.add_argument("--bulk-read-ring-kb", type=int, default=16 * 1024)
    parser.add_argument("--no-insert-evicted", action="store_true")
    parser.add_argument("--sample-every", type=int, default=1,
                        help="Hash-based sampling: only load 1/N of SB events (reduces memory)")
    parser.add_argument("--scale-bulk-ring-for-sampling", action="store_true",
                        help="Also divide bulk-read ring size by --sample-every (diagnostic only)")
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    readahead_grid = parse_list(args.readahead_grid, int)
    os_scale_grid = parse_list(args.os_scale_grid, float)
    rows, best_rows = build_predictions(
        result_dir,
        strategies,
        readahead_grid,
        os_scale_grid,
        args.bulk_read_ring_kb,
        insert_evicted=not args.no_insert_evicted,
        sample_every=args.sample_every,
        scale_bulk_ring_for_sampling=args.scale_bulk_ring_for_sampling,
    )
    write_csv(result_dir / "continuous_predictions.csv", rows)
    write_csv(result_dir / "continuous_best_predictions.csv", best_rows)
    write_report(result_dir, best_rows)
    render_continuous_stage_plots(result_dir, best_rows, rows, args.sample_every)
    print(result_dir / "CONTINUOUS_OS_SB_EVALUATION.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
