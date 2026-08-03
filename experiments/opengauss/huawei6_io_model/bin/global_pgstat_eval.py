#!/usr/bin/env python3
"""Global pg_stat-based OS/SB validation for one Huawei5 workload trace."""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import cache_hit_stage_eval as stage_eval  # noqa: E402


HUAWEI4_MODEL = Path(os.environ.get("HUAWEI4_MODEL", PACKAGE_ROOT / "bin" / "dual_cache_warmup.py"))


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    stage_eval.write_csv(path, rows)


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


def format_pp(value: str) -> str:
    return f"{float(value):+.2f}"


def write_sb_model_trace(
    full_trace: Path,
    out_trace: Path,
    warm_start_ns: int,
    measure_end_ns: int,
) -> int:
    """Write the SB-only trace consumed by the Huawei4 model.

    The global validation still measures OS reads from the full trace. Keeping
    only SB events here avoids duplicating large OS trace lines for long runs.
    """

    out_trace.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    opener = gzip.open if out_trace.suffix == ".gz" else open
    with stage_eval.open_trace_text(full_trace) as src, opener(
        out_trace, "wt", encoding="utf-8"
    ) as dst:
        dst.write(f"# global sb-only warm_start={warm_start_ns} end={measure_end_ns}\n")
        for line in src:
            if not line.startswith("SB,"):
                continue
            try:
                ts = stage_eval.trace_ts_ns(line)
            except ValueError:
                continue
            if ts is None or not (warm_start_ns <= ts < measure_end_ns):
                continue
            dst.write(stage_eval.rewrite_trace_ts(line, warm_start_ns))
            count += 1
    return count


def count_os_trace_abs(full_trace: Path, measure_start_ns: int, measure_end_ns: int) -> tuple[int, int]:
    events = 0
    bytes_read = 0
    os_sum_samples: list[tuple[int, int, int]] = []
    with stage_eval.open_trace_text(full_trace) as fh:
        for line in fh:
            if line.startswith("OS,"):
                parts = line.rstrip("\n").split(",")
                if len(parts) < 6:
                    continue
                try:
                    ts = int(parts[5])
                    read_bytes = int(parts[4])
                except ValueError:
                    continue
                if measure_start_ns <= ts < measure_end_ns:
                    events += 1
                    bytes_read += max(0, read_bytes)
            elif line.startswith("OS_SUM,"):
                parts = line.rstrip("\n").split(",")
                if len(parts) < 4:
                    continue
                try:
                    os_sum_samples.append((int(parts[1]), int(parts[2]), int(parts[3])))
                except ValueError:
                    continue
    if events == 0 and bytes_read == 0 and os_sum_samples:
        return stage_eval.os_sum_delta(os_sum_samples, measure_start_ns, measure_end_ns)
    return events, bytes_read


def run_predict(
    out_dir: Path,
    trace: Path,
    measurements: Path,
    sb_mb: int,
    os_mb: int,
    warm_seconds: float,
    measure_seconds: float,
    strategy: str,
    models: str,
    readahead_grid: str,
    os_scale_grid: str,
    bulk_read_ring_kb: int,
    sample_every: int,
    sample_mode: str,
) -> dict[str, str]:
    predictions = out_dir / f"global_predictions_{strategy}.csv"
    best_huawei4 = out_dir / f"global_best_huawei4_{strategy}.csv"
    accuracy = out_dir / f"global_accuracy_{strategy}.csv"
    log = out_dir / f"global_predict_{strategy}.log"
    cmd = [
        "python3",
        str(HUAWEI4_MODEL),
        "predict",
        "--trace",
        str(trace),
        "--mode",
        "global_5stage",
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
        str(predictions),
        "--best-output",
        str(best_huawei4),
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
    with log.open("w", encoding="utf-8") as fh:
        subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True, check=True)
    row = best_by_full_error(predictions)
    row["strategy"] = strategy
    row["prediction_file"] = str(predictions)
    row["accuracy_file"] = str(accuracy)
    row["best_selection"] = "min_abs_sb_plus_abs_os_plus_half_abs_combined"
    row["sample_every"] = sample_every
    row["sample_mode"] = sample_mode
    return row


def write_report(
    out_dir: Path,
    actual: dict[str, object],
    best_rows: list[dict[str, str]],
    start_label: str,
    measure_start_label: str,
    end_label: str,
    sample_every: int,
    sample_mode: str,
) -> None:
    report = out_dir / "GLOBAL_PGSTAT_EVALUATION.md"
    lines = [
        "# Huawei5 Global pg_stat_database Evaluation",
        "",
        f"- Warmup window: `{start_label} -> {measure_start_label}`",
        f"- Measurement window: `{measure_start_label} -> {end_label}`",
        "- SB actual uses `pg_stat_database` after workload clients/backends have stopped.",
        "- OS actual uses measurement-window `pread64` bytes and block-device read bytes.",
        "",
        "## Actual",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| `pg_stat` blks_hit delta | {actual['blks_hit_delta']} |",
        f"| `pg_stat` blks_read delta | {actual['blks_read_delta']} |",
        f"| `pg_stat` SB events | {actual['sb_measure_events']} |",
        f"| trace SB events in warmup+measurement | {actual['trace_sb_events']} |",
        f"| trace OS events in measurement | {actual['trace_os_events']} |",
        f"| OS pread bytes | {actual['os_measure_bytes']} |",
        f"| disk read bytes | {actual['disk_read_bytes_delta']} |",
        f"| OS cache capacity MB | {actual.get('os_cache_capacity_mb', actual['os_cache_mb'])} |",
        f"| OS cache resident MB at measurement start | {actual.get('os_cache_resident_mb', actual['os_cache_mb'])} |",
        f"| actual SB hit rate | {actual['meas_sb_hr']} |",
        f"| actual OS conditional hit rate | {actual['meas_os_hr']} |",
        f"| actual combined hit rate | {actual['meas_combined']} |",
        f"| prediction sample every | {sample_every} |",
        f"| prediction sample mode | {sample_mode} |",
        "",
        "## Best Global Prediction",
        "",
        "| strategy | model | ra | scale | actual SB | pred SB | SB err pp | actual OS | pred OS | OS err pp | actual combined | pred combined | combined err pp |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in best_rows:
        lines.append(
            "| {strategy} | {model} | {readahead_pages} | {os_scale} | "
            "{meas_sb_hr} | {sb_hit_rate:.6f} | {sb_err} | "
            "{meas_os_hr} | {physical_os_cond_hit_rate:.6f} | {os_err} | "
            "{meas_combined} | {physical_combined_hit_rate:.6f} | {combined_err} |".format(
                **{
                    **row,
                    "sb_hit_rate": float(row["sb_hit_rate"]),
                    "physical_os_cond_hit_rate": float(row["physical_os_cond_hit_rate"]),
                    "physical_combined_hit_rate": float(row["physical_combined_hit_rate"]),
                    "sb_err": format_pp(row["sb_err_pp"]),
                    "os_err": format_pp(row["os_err_pp"]),
                    "combined_err": format_pp(row["combined_err_pp"]),
                }
            )
        )
    lines += [
        "",
        "## Files",
        "",
        f"- Measurement CSV: `{out_dir / 'global_measurements.csv'}`",
        f"- Best rows CSV: `{out_dir / 'global_best_predictions.csv'}`",
    ]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--warm-start-label", default="pre_tp_low_start")
    parser.add_argument("--measure-start-label", default="stage1_memory_rich_start")
    parser.add_argument("--measure-end-label", default="global_measure_end_after_stop")
    parser.add_argument("--strategies", default="clock,bulk_ring")
    parser.add_argument("--models", default="cold,warmup_miss,warmup_full")
    parser.add_argument("--readahead-grid", default="0")
    parser.add_argument("--os-scale-grid", default="0.5")
    parser.add_argument("--bulk-read-ring-kb", type=int, default=16 * 1024)
    parser.add_argument("--sample-every", type=int, default=1)
    parser.add_argument("--sample-mode", choices=["hash", "interval"], default="hash")
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    out_dir = result_dir / "global_eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    boundaries = {row["label"]: row for row in read_rows(result_dir / "boundaries.csv")}
    missing = [
        label
        for label in (args.warm_start_label, args.measure_start_label, args.measure_end_label)
        if label not in boundaries
    ]
    if missing:
        raise SystemExit(f"missing boundary label(s): {', '.join(missing)}")

    run_config = (result_dir / "run_config.json").read_text(encoding="utf-8")
    import json

    config = json.loads(run_config)
    sb_mb = int(config["shared_buffers_mb"])
    full_trace = Path(config["trace"])
    warm_start = boundaries[args.warm_start_label]
    measure_start = boundaries[args.measure_start_label]
    measure_end = boundaries[args.measure_end_label]
    warm_seconds = (int(measure_start["elapsed_ns"]) - int(warm_start["elapsed_ns"])) / 1e9
    measure_seconds = (int(measure_end["elapsed_ns"]) - int(measure_start["elapsed_ns"])) / 1e9
    global_trace = out_dir / "global_trace_sb.log.gz"
    trace_sb_events = write_sb_model_trace(
        full_trace,
        global_trace,
        int(warm_start["elapsed_ns"]),
        int(measure_end["elapsed_ns"]),
    )
    os_events, os_bytes = count_os_trace_abs(
        full_trace,
        int(measure_start["elapsed_ns"]),
        int(measure_end["elapsed_ns"]),
    )
    actual = stage_eval.actual_row(
        "global_5stage",
        sb_mb,
        global_trace,
        measure_start,
        measure_end,
        warm_seconds,
        measure_seconds,
        os_events,
        os_bytes,
    )
    actual["trace_events"] = trace_sb_events + os_events
    actual["trace_sb_events"] = trace_sb_events
    measurements = out_dir / "global_measurements.csv"
    write_csv(measurements, [actual])

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    best_rows = [
        run_predict(
            out_dir,
            global_trace,
            measurements,
            sb_mb,
            int(actual["os_cache_mb"]),
            warm_seconds,
            measure_seconds,
            strategy,
            args.models,
            args.readahead_grid,
            args.os_scale_grid,
            args.bulk_read_ring_kb,
            args.sample_every,
            args.sample_mode,
        )
        for strategy in strategies
    ]
    write_csv(out_dir / "global_best_predictions.csv", best_rows)
    write_report(
        out_dir,
        actual,
        best_rows,
        args.warm_start_label,
        args.measure_start_label,
        args.measure_end_label,
        args.sample_every,
        args.sample_mode,
    )
    print(out_dir / "GLOBAL_PGSTAT_EVALUATION.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
