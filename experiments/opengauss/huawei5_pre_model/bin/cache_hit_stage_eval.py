#!/usr/bin/env python3
"""Per-stage OS/SB hit-ratio validation for the Huawei5 five-stage workload."""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import tpc5stage  # noqa: E402


HUAWEI4_MODEL = Path(os.environ.get("HUAWEI4_MODEL", PACKAGE_ROOT / "bin" / "dual_cache_warmup.py"))
TRACE_BOTH = Path(os.environ.get("TRACE_BOTH", PACKAGE_ROOT / "bpftrace" / "trace_both.bt"))
PAGE_SIZE = 8192


def sh_out(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True).strip()


def data_device() -> str:
    data_dir = os.environ.get("OPENGAUSS_DATA_DIR", "/opt/openGauss/data")
    return Path(sh_out(["df", data_dir]).splitlines()[1].split()[0]).name


def gauss_pid() -> int:
    return int(sh_out(["pgrep", "-x", "gaussdb"]).splitlines()[0])


def meminfo_kb() -> dict[str, int]:
    values: dict[str, int] = {}
    wanted = {
        "Active(file)",
        "Inactive(file)",
        "MemAvailable",
        "MemFree",
        "Cached",
        "Buffers",
        "SReclaimable",
        "Shmem",
    }
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            key, rest = line.split(":", 1)
            if key in wanted:
                values[key] = int(rest.split()[0])
    return values


def file_cache_mb() -> int:
    info = meminfo_kb()
    active = info.get("Active(file)", 0)
    inactive = info.get("Inactive(file)", 0)
    return int((active + inactive) / 1024)


def os_cache_capacity_mb() -> int:
    """Approximate max page-cache capacity, not current resident file cache.

    After dropping caches, Active(file)+Inactive(file) can be tiny, but Linux
    can grow the page cache into MemAvailable during the workload. The model's
    OS size is a capacity, so use an available-memory based upper bound.
    """

    info = meminfo_kb()
    resident = info.get("Active(file)", 0) + info.get("Inactive(file)", 0)
    available = info.get("MemAvailable", 0)
    return int(max(resident, available) / 1024)


def shared_buffers_mb() -> int:
    out = tpc5stage.gsql_output("SHOW shared_buffers;\n")
    value = out.strip().splitlines()[-1].strip().upper().replace(" ", "")
    number = "".join(ch for ch in value if ch.isdigit() or ch == ".")
    unit = value[len(number):]
    if not number:
        raise RuntimeError(f"could not parse shared_buffers: {out!r}")
    mb = float(number)
    if unit in ("GB", "G"):
        mb *= 1024
    elif unit in ("KB", "K"):
        mb /= 1024
    elif unit in ("B",):
        mb /= 1024 * 1024
    return int(round(mb))


def read_db_stats() -> tuple[int, int]:
    out = tpc5stage.gsql_output(
        """
SELECT COALESCE(sum(blks_hit), 0)::bigint || ' ' ||
       COALESCE(sum(blks_read), 0)::bigint
FROM pg_stat_database
WHERE datname IN ('h5_tpcc', 'h5_tpch');
"""
    )
    hit, read = out.split()
    return int(hit), int(read)


def reset_db_stats() -> None:
    tpc5stage.gsql("SELECT pg_stat_reset();\n")


def stabilize_before_run(drop_os_cache: bool) -> None:
    print("[stage-eval] stabilize database state: terminate residual clients, checkpoint, sync", flush=True)
    tpc5stage.terminate_residual_workload_backends()
    tpc5stage.gsql("CHECKPOINT;\n")
    subprocess.run(["sync"], check=True)
    if drop_os_cache:
        print("[stage-eval] drop OS page cache before workload", flush=True)
        Path("/proc/sys/vm/drop_caches").write_text("3\n", encoding="utf-8")


def workload_backend_count() -> int:
    out = tpc5stage.gsql_output(
        """
SELECT count(*)
FROM pg_stat_activity
WHERE application_name LIKE 'tpcc%'
   OR application_name LIKE 'tpch%'
   OR application_name = 'tpch_ap';
"""
    )
    return int(out or "0")


def wait_workload_backends_gone(timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if workload_backend_count() == 0:
            return True
        time.sleep(0.5)
    return workload_backend_count() == 0


def read_disk_stats(dev: str) -> tuple[int, int]:
    with open("/proc/diskstats", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 6 and parts[2] == dev:
                return int(parts[3]), int(parts[5])
    raise RuntimeError(f"device not found in /proc/diskstats: {dev}")


def boundary(label: str, bpf_start_ns: int, dev: str) -> dict[str, object]:
    hit, read = read_db_stats()
    disk_reads, sectors = read_disk_stats(dev)
    resident_mb = file_cache_mb()
    capacity_mb = os_cache_capacity_mb()
    return {
        "label": label,
        "wall_time": time.strftime("%F %T"),
        "elapsed_ns": time.monotonic_ns() - bpf_start_ns,
        "blks_hit": hit,
        "blks_read": read,
        "disk_reads": disk_reads,
        "disk_read_sectors": sectors,
        "os_cache_mb": capacity_mb,
        "os_cache_capacity_mb": capacity_mb,
        "os_cache_resident_mb": resident_mb,
    }


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


def trace_ts_ns(line: str) -> int | None:
    if line.startswith("SB,"):
        parts = line.rstrip("\n").split(",")
        if len(parts) >= 5:
            return int(parts[4])
    elif line.startswith("OS,"):
        parts = line.rstrip("\n").split(",")
        if len(parts) >= 6:
            return int(parts[5])
    return None


def rewrite_trace_ts(line: str, offset_ns: int) -> str:
    parts = line.rstrip("\n").split(",")
    if parts[0] == "SB":
        parts[4] = str(max(0, int(parts[4]) - offset_ns))
    elif parts[0] == "OS":
        parts[5] = str(max(0, int(parts[5]) - offset_ns))
    return ",".join(parts) + "\n"


def split_stage_trace(
    full_trace: Path,
    out_trace: Path,
    warm_start: int,
    warm_end: int,
    measure_start: int,
    measure_end: int,
) -> int:
    out_trace.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with full_trace.open("r", errors="replace", encoding="utf-8") as src, out_trace.open(
        "w", encoding="utf-8"
    ) as dst:
        dst.write(
            f"# split warm=[{warm_start},{warm_end}) measure=[{measure_start},{measure_end})\n"
        )
        for line in src:
            if not (line.startswith("SB,") or line.startswith("OS,")):
                continue
            try:
                ts = trace_ts_ns(line)
            except ValueError:
                continue
            if ts is None:
                continue
            if (warm_start <= ts < warm_end) or (measure_start <= ts < measure_end):
                dst.write(rewrite_trace_ts(line, warm_start))
                count += 1
    return count


def count_measurement_trace_os(trace_path: Path, warm_seconds: float, measure_seconds: float) -> tuple[int, int]:
    first_ts: int | None = None
    os_events = 0
    os_bytes = 0
    warm_ns = int(warm_seconds * 1_000_000_000)
    end_ns = int((warm_seconds + measure_seconds) * 1_000_000_000)
    with trace_path.open("r", errors="replace", encoding="utf-8") as fh:
        for line in fh:
            if not (line.startswith("SB,") or line.startswith("OS,")):
                continue
            try:
                ts = trace_ts_ns(line)
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
    return os_events, os_bytes


def actual_row(
    stage: str,
    sb_mb: int,
    trace_path: Path,
    start: dict[str, object],
    end: dict[str, object],
    warm_seconds: float,
    measure_seconds: float,
    trace_os_events: int,
    trace_os_bytes: int,
) -> dict[str, object]:
    hit_delta = int(end["blks_hit"]) - int(start["blks_hit"])
    read_delta = int(end["blks_read"]) - int(start["blks_read"])
    disk_delta = int(end["disk_reads"]) - int(start["disk_reads"])
    sector_delta = int(end["disk_read_sectors"]) - int(start["disk_read_sectors"])
    disk_bytes = max(0, sector_delta) * 512
    logical_bytes = max(0, trace_os_bytes)
    total = hit_delta + read_delta
    sb_hr = hit_delta / total if total > 0 else 0.0
    os_hr = 1.0
    if logical_bytes > 0:
        os_hr = max(0.0, 1.0 - min(1.0, disk_bytes / logical_bytes))
    combined = sb_hr + (1.0 - sb_hr) * os_hr
    return {
        "mode": stage,
        "sb_mb": sb_mb,
        "os_cache_mb": int(start["os_cache_mb"]),
        "os_actual_cache_mb": int(start["os_cache_mb"]),
        "os_cache_capacity_mb": int(start.get("os_cache_capacity_mb", start["os_cache_mb"])),
        "os_cache_resident_mb": int(start.get("os_cache_resident_mb", start["os_cache_mb"])),
        "warmup_seconds": f"{warm_seconds:.3f}",
        "measure_seconds": f"{measure_seconds:.3f}",
        "sb_measure_events": total,
        "sb_direct_hit_events": "",
        "sb_direct_seen_events": "",
        "sb_metric": "pg_stat_database",
        "os_measure_events": trace_os_events,
        "os_measure_bytes": logical_bytes,
        "disk_reads_delta": max(0, disk_delta),
        "disk_read_requests_delta": max(0, disk_delta),
        "disk_read_sectors_delta": max(0, sector_delta),
        "disk_read_bytes_delta": disk_bytes,
        "disk_metric": "bytes",
        "meas_sb_hr": f"{sb_hr:.6f}",
        "meas_os_hr_legacy_requests": "",
        "meas_combined_legacy_requests": "",
        "meas_os_hr": f"{os_hr:.6f}",
        "meas_combined": f"{combined:.6f}",
        "trace_file": str(trace_path),
        "blks_hit_delta": hit_delta,
        "blks_read_delta": read_delta,
        "trace_os_events": trace_os_events,
        "trace_os_bytes": trace_os_bytes,
    }


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
) -> dict[str, str]:
    output = stage_dir / f"predictions_{strategy}.csv"
    best = stage_dir / f"best_predictions_{strategy}.csv"
    accuracy = stage_dir / f"accuracy_{strategy}.csv"
    plot_dir = stage_dir / f"plots_{strategy}"
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
        "cold,warmup_miss,warmup_full",
        "--measurements",
        str(measurements),
        "--pairs-from-measurements",
        "--output",
        str(output),
        "--best-output",
        str(best),
        "--accuracy-output",
        str(accuracy),
        "--plot-dir",
        str(plot_dir),
        "--tune",
        "--readahead-grid",
        "0,4,16,64,128",
        "--os-scale-grid",
        "0.5,0.75,1,1.25,1.5,2",
        "--sb-strategy",
        strategy,
    ]
    if strategy == "bulk_ring":
        cmd += ["--bulk-read-ring-kb", "16384"]
    log_path = stage_dir / f"predict_{strategy}.log"
    with log_path.open("w", encoding="utf-8") as fh:
        subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True, check=True)
    with best.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise RuntimeError(f"no best prediction rows for {stage}/{strategy}")
    row = rows[0]
    row["strategy"] = strategy
    row["stage"] = stage
    row["prediction_file"] = str(output)
    row["accuracy_file"] = str(accuracy)
    return row


def pct_err(row: dict[str, str], metric: str) -> float:
    return (float(row[metric]) - float(row[f"meas_{metric.split('_')[0]}_hr"])) * 100.0


def write_report(out_dir: Path, summary_rows: list[dict[str, object]], args: argparse.Namespace) -> None:
    md = out_dir / "OS_SB_BY_STAGE_EVALUATION.md"
    lines = [
        "# Huawei5 OS/SB Hit-Ratio By-Stage Evaluation",
        "",
        f"Run directory: `{out_dir}`",
        "",
        "## Workload",
        "",
        f"- TPC-C warehouses: {args.tpcc_warehouses}",
        f"- TPC-H scale: SF{args.tpch_scale:g}",
        f"- Fixed seed: `{args.seed}`",
        f"- Stage duration: {args.stage_seconds}s",
        f"- `shared_buffers`: {summary_rows[0]['sb_mb']}MB",
        "",
        "## Summary",
        "",
        "| Stage | Strategy | Actual SB | Pred SB | SB err pp | Actual OS | Pred OS | OS err pp | Actual combined | Pred combined | Combined err pp |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary_rows:
        display = dict(row)
        display["stage"] = display.get("stage") or display.get("mode")
        lines.append(
            "| {stage} | {strategy} | {meas_sb_hr:.6f} | {sb_hit_rate:.6f} | {sb_err_pp:.2f} | "
            "{meas_os_hr:.6f} | {pred_os:.6f} | {os_err_pp:.2f} | "
            "{meas_combined:.6f} | {combined_hit_rate:.6f} | {combined_err_pp:.2f} |".format(
                **display
            )
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "- This report evaluates each stage separately instead of merging stages 2-5.",
        "- Actual SB hit rate uses `pg_stat_database.blks_hit/blks_read` deltas.",
        "- Actual OS conditional hit rate uses block-device read bytes divided by bpftrace `pread64` bytes in the measurement window.",
        "- Direct SB hit flags from bpftrace are not used as ground truth on this openGauss build; `pg_stat_database` is the safer SB metric here.",
        "- `combined` can look better than the underlying SB/OS metrics when SB and OS errors cancel.",
    ]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    tpc5stage.add_common(parser)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--strategies", default="clock,bulk_ring")
    parser.add_argument("--no-reset-stats", action="store_true")
    parser.add_argument("--stats-flush-wait-seconds", type=float, default=2.0)
    parser.add_argument("--backend-wait-seconds", type=float, default=30.0)
    parser.add_argument("--stage-predictions", action="store_true")
    parser.add_argument("--global-readahead-grid", default="0")
    parser.add_argument("--global-os-scale-grid", default="0.5,0.75,1.0")
    parser.add_argument("--stabilize-before-run", action="store_true")
    parser.add_argument("--drop-os-cache-before-run", action="store_true")
    args = parser.parse_args()
    args.total_seconds = args.stage_seconds * 5 + 60
    if args.stable_workload:
        args.stabilize_before_run = True

    if not TRACE_BOTH.exists():
        raise SystemExit(f"missing bpftrace script: {TRACE_BOTH}")
    if not HUAWEI4_MODEL.exists():
        raise SystemExit(f"missing Huawei4 model script: {HUAWEI4_MODEL}")

    run_id = time.strftime("cacheeval_stage_%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else tpc5stage.RESULTS / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stages_dir = out_dir / "stages"
    stages_dir.mkdir(exist_ok=True)

    paths = tpc5stage.render_configs(args)
    dev = data_device()
    pid = gauss_pid()
    sb_mb = shared_buffers_mb()
    trace_path = out_dir / "trace_full.log"
    run_log = out_dir / "workload.log"
    boundaries: list[dict[str, object]] = []
    live: list[tpc5stage.ProcSpec] = []
    bpf_proc: subprocess.Popen | None = None
    trace_fh = None

    print(f"[stage-eval] out={out_dir} pid={pid} dev={dev} sb={sb_mb}MB", flush=True)
    try:
        if args.stabilize_before_run:
            stabilize_before_run(args.drop_os_cache_before_run)
        if not args.no_reset_stats:
            reset_db_stats()
        trace_fh = trace_path.open("w", encoding="utf-8")
        bpf_start_ns = time.monotonic_ns()
        bpf_proc = subprocess.Popen(
            ["bpftrace", str(TRACE_BOTH), str(pid)],
            stdout=trace_fh,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(2)

        boundaries.append(boundary("pre_tp_low_start", bpf_start_ns, dev))
        tp_low = tpc5stage.start(
            "tpcc_low",
            tpc5stage.benchbase_cmd("tpcc", paths["tpcc_low"], create=False, load=False, execute=True),
            out_dir / "tpcc_low.log",
        )
        live.append(tp_low)
        time.sleep(5)

        stage_specs = [
            ("stage1_memory_rich", "ap_s1"),
            ("stage2_reach_limit", "ap_s2"),
            ("stage3_protect_tp", "ap_s3"),
            ("stage4_backpressure", "ap_s4"),
        ]
        for stage, key in stage_specs:
            boundaries.append(boundary(f"{stage}_start", bpf_start_ns, dev))
            live.extend(tpc5stage.start_configs(stage, "tpch", paths[key], out_dir))
            time.sleep(args.stage_seconds)
            boundaries.append(boundary(f"{stage}_end", bpf_start_ns, dev))

        boundaries.append(boundary("stage5_tp_surge_start", bpf_start_ns, dev))
        tp_high = tpc5stage.start(
            "stage5_tp_surge",
            tpc5stage.benchbase_cmd("tpcc", paths["tpcc_high"], create=False, load=False, execute=True),
            out_dir / "tpcc_high.log",
        )
        live.append(tp_high)
        live.extend(tpc5stage.start_configs("stage5_ap_pressure", "tpch", paths["ap_s5"], out_dir))
        time.sleep(args.stage_seconds)
        boundaries.append(boundary("stage5_tp_surge_end", bpf_start_ns, dev))

    finally:
        for spec in reversed(live):
            tpc5stage.stop(spec)
        try:
            tpc5stage.terminate_residual_workload_backends()
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] residual backend cleanup failed: {exc}", flush=True)
        try:
            if not wait_workload_backends_gone(args.backend_wait_seconds):
                print("[warn] workload backends still visible before global pg_stat read", flush=True)
            if args.stats_flush_wait_seconds > 0:
                time.sleep(args.stats_flush_wait_seconds)
            if bpf_proc is not None:
                boundaries.append(boundary("global_measure_end_after_stop", bpf_start_ns, dev))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] failed to record global post-stop boundary: {exc}", flush=True)
        if bpf_proc is not None and bpf_proc.poll() is None:
            bpf_proc.send_signal(signal.SIGTERM)
            try:
                bpf_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bpf_proc.kill()
                bpf_proc.wait(timeout=10)
        if trace_fh is not None:
            trace_fh.close()

    write_csv(out_dir / "boundaries.csv", boundaries)
    (out_dir / "run_config.json").write_text(
        json.dumps(
            {
                "seed": args.seed,
                "tpcc_warehouses": args.tpcc_warehouses,
                "tpch_scale": args.tpch_scale,
                "stage_seconds": args.stage_seconds,
                "sample_interval": args.sample_interval,
                "tp_low_terminals": args.tp_low_terminals,
                "tp_low_rate": args.tp_low_rate,
                "tp_high_terminals": args.tp_high_terminals,
                "tp_high_rate": args.tp_high_rate,
                "effective_tp_high_rate": tpc5stage.effective_tp_high_rate(args),
                "ap_work_mem": args.ap_work_mem,
                "stable_workload": args.stable_workload,
                "stable_tp_high_rate": args.stable_tp_high_rate,
                "ap_rate": args.ap_rate,
                "ap_serial": args.ap_serial,
                "effective_ap_serial": args.ap_serial or args.stable_workload,
                "ap_fixed_query_clients": args.ap_fixed_query_clients or args.stable_workload,
                "ap_query_cycle": args.ap_query_cycle,
                "stabilize_before_run": args.stabilize_before_run,
                "drop_os_cache_before_run": args.drop_os_cache_before_run,
                "shared_buffers_mb": sb_mb,
                "device": dev,
                "trace": str(trace_path),
                "reset_stats": not args.no_reset_stats,
                "global_measure_end_label": "global_measure_end_after_stop",
                "ap_configs": {
                    key: [str(path) for path in tpc5stage.config_paths(paths[key])]
                    for key in ("ap_s1", "ap_s2", "ap_s3", "ap_s4", "ap_s5")
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    global_cmd = [
        "python3",
        str(SCRIPT_DIR / "global_pgstat_eval.py"),
        "--result-dir",
        str(out_dir),
        "--strategies",
        args.strategies,
        "--readahead-grid",
        args.global_readahead_grid,
        "--os-scale-grid",
        args.global_os_scale_grid,
    ]
    subprocess.run(global_cmd, check=True)

    if not args.stage_predictions:
        print(f"[stage-eval] global report: {out_dir / 'global_eval' / 'GLOBAL_PGSTAT_EVALUATION.md'}", flush=True)
        return 0

    bmap = {str(row["label"]): row for row in boundaries}
    stage_defs = [
        ("stage1_memory_rich", "pre_tp_low_start", "stage1_memory_rich_start", "stage1_memory_rich_end"),
        ("stage2_reach_limit", "stage1_memory_rich_start", "stage2_reach_limit_start", "stage2_reach_limit_end"),
        ("stage3_protect_tp", "stage2_reach_limit_start", "stage3_protect_tp_start", "stage3_protect_tp_end"),
        ("stage4_backpressure", "stage3_protect_tp_start", "stage4_backpressure_start", "stage4_backpressure_end"),
        ("stage5_tp_surge", "stage4_backpressure_start", "stage5_tp_surge_start", "stage5_tp_surge_end"),
    ]

    summary: list[dict[str, object]] = []
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    for stage, warm_start_label, measure_start_label, measure_end_label in stage_defs:
        warm_start = bmap[warm_start_label]
        measure_start = bmap[measure_start_label]
        measure_end = bmap[measure_end_label]
        warm_seconds = (int(measure_start["elapsed_ns"]) - int(warm_start["elapsed_ns"])) / 1e9
        measure_seconds = (int(measure_end["elapsed_ns"]) - int(measure_start["elapsed_ns"])) / 1e9
        stage_dir = stages_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        split_trace = stage_dir / "trace.log"
        event_count = split_stage_trace(
            trace_path,
            split_trace,
            int(warm_start["elapsed_ns"]),
            int(measure_start["elapsed_ns"]),
            int(measure_start["elapsed_ns"]),
            int(measure_end["elapsed_ns"]),
        )
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
        actual["trace_events"] = event_count
        measurements = stage_dir / "measurements.csv"
        write_csv(measurements, [actual])
        for strategy in strategies:
            pred = run_predict(
                stage_dir,
                stage,
                strategy,
                split_trace,
                measurements,
                sb_mb,
                int(actual["os_cache_mb"]),
                warm_seconds,
                measure_seconds,
            )
            row = dict(actual)
            row.update(
                {
                    "strategy": strategy,
                    "model": pred["model"],
                    "readahead_pages": pred["readahead_pages"],
                    "os_scale": pred["os_scale"],
                    "sb_hit_rate": float(pred["sb_hit_rate"]),
                    "pred_os": float(pred["physical_os_cond_hit_rate"]),
                    "combined_hit_rate": float(pred["physical_combined_hit_rate"]),
                    "sb_err_pp": float(pred["sb_err_pp"]),
                    "os_err_pp": float(pred["os_err_pp"]),
                    "combined_err_pp": float(pred["combined_err_pp"]),
                    "prediction_file": pred["prediction_file"],
                    "accuracy_file": pred["accuracy_file"],
                }
            )
            row["meas_sb_hr"] = float(row["meas_sb_hr"])
            row["meas_os_hr"] = float(row["meas_os_hr"])
            row["meas_combined"] = float(row["meas_combined"])
            summary.append(row)

    write_csv(out_dir / "stage_model_summary.csv", summary)
    write_report(out_dir, summary, args)
    print(f"[stage-eval] report: {out_dir / 'OS_SB_BY_STAGE_EVALUATION.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
