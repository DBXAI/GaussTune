#!/usr/bin/env python3
"""Measure saturated TPCC throughput under each Huawei5 AP stage."""

from __future__ import annotations

import argparse
import csv
import json
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import tpc5stage


STAGES = [
    ("no_ap", None),
    ("stage1_memory_rich", "ap_s1"),
    ("stage2_reach_limit", "ap_s2"),
    ("stage3_protect_tp", "ap_s3"),
    ("stage4_backpressure", "ap_s4"),
    ("stage5_tp_surge", "ap_s5"),
]


def parse_stage_work_mem(value: str) -> dict[str, str]:
    if not value.strip():
        return {}
    known = {stage for stage, _key in STAGES if stage != "no_ap"}
    result: dict[str, str] = {}
    for item in value.split(","):
        stage, separator, work_mem = item.strip().partition("=")
        if not separator or stage not in known or not work_mem:
            raise ValueError(f"invalid stage work_mem assignment: {item!r}")
        if work_mem.isdigit():
            work_mem += "MB"
        result[stage] = work_mem
    return result


def write_ap_setup(work_mem: str, temp_file_limit: str = "") -> None:
    lines = [
        "SET application_name = 'tpch_ap';",
        f"SET work_mem = '{work_mem}';",
    ]
    if temp_file_limit:
        lines.append(f"SET temp_file_limit = '{temp_file_limit}';")
    lines.extend(
        [
            "SET enable_hashjoin = on;",
            "SET enable_mergejoin = on;",
            "SET enable_nestloop = on;",
        ]
    )
    tpc5stage.write(tpc5stage.CONF / "tpch_ap_session.sql", "\n".join(lines) + "\n")


def read_meminfo() -> dict[str, int]:
    wanted = {
        "MemAvailable",
        "Cached",
        "Active(file)",
        "Inactive(file)",
        "AnonPages",
        "Dirty",
        "Shmem",
    }
    values: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            key, rest = line.split(":", 1)
            if key in wanted:
                values[key] = int(rest.split()[0])
    return values


def read_vmstat() -> dict[str, int]:
    wanted = {
        "pgscan_direct",
        "pgscan_kswapd",
        "pgsteal_direct",
        "pgsteal_kswapd",
        "pswpin",
        "pswpout",
    }
    values: dict[str, int] = {}
    with open("/proc/vmstat", encoding="utf-8") as fh:
        for line in fh:
            key, value = line.split()
            if key in wanted or key.startswith("workingset_refault") or key.startswith("allocstall"):
                values[key] = int(value)
    return values


def read_memory_psi() -> dict[str, float]:
    values: dict[str, float] = {}
    with open("/proc/pressure/memory", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            category = parts[0]
            for item in parts[1:]:
                key, value = item.split("=", 1)
                if key in {"avg10", "total"}:
                    values[f"psi_{category}_{key}"] = float(value)
    return values


def gauss_memory_kb() -> dict[str, int]:
    pids = subprocess.check_output(["pgrep", "-x", "gaussdb"], text=True).splitlines()
    if not pids:
        return {}
    wanted = {"VmRSS", "VmSize", "VmSwap", "RssAnon", "RssFile", "RssShmem"}
    values: dict[str, int] = {}
    with open(f"/proc/{pids[0]}/status", encoding="utf-8") as fh:
        for line in fh:
            key, rest = line.split(":", 1)
            if key in wanted:
                values[key] = int(rest.split()[0])
    return values


def memory_sample(stage: str, sb_mb: int, elapsed_seconds: float) -> dict[str, object]:
    mem = read_meminfo()
    gauss = gauss_memory_kb()
    psi = read_memory_psi()
    return {
        "stage": stage,
        "sb_mb": sb_mb,
        "elapsed_seconds": f"{elapsed_seconds:.3f}",
        "mem_available_mb": f"{mem.get('MemAvailable', 0) / 1024:.3f}",
        "file_cache_mb": f"{(mem.get('Active(file)', 0) + mem.get('Inactive(file)', 0)) / 1024:.3f}",
        "cached_mb": f"{mem.get('Cached', 0) / 1024:.3f}",
        "anon_pages_mb": f"{mem.get('AnonPages', 0) / 1024:.3f}",
        "dirty_mb": f"{mem.get('Dirty', 0) / 1024:.3f}",
        "shmem_mb": f"{mem.get('Shmem', 0) / 1024:.3f}",
        "gauss_rss_mb": f"{gauss.get('VmRSS', 0) / 1024:.3f}",
        "gauss_rss_anon_mb": f"{gauss.get('RssAnon', 0) / 1024:.3f}",
        "gauss_rss_file_mb": f"{gauss.get('RssFile', 0) / 1024:.3f}",
        "gauss_swap_mb": f"{gauss.get('VmSwap', 0) / 1024:.3f}",
        "psi_some_avg10": f"{psi.get('psi_some_avg10', 0.0):.3f}",
        "psi_full_avg10": f"{psi.get('psi_full_avg10', 0.0):.3f}",
        "psi_some_total_us": f"{psi.get('psi_some_total', 0.0):.0f}",
        "psi_full_total_us": f"{psi.get('psi_full_total', 0.0):.0f}",
    }


def db_counters() -> tuple[int, int, int]:
    output = tpc5stage.gsql_output(
        """
SELECT (xact_commit + xact_rollback)::bigint,
       blks_hit::bigint,
       blks_read::bigint
FROM pg_stat_database
WHERE datname = 'h5_tpcc';
"""
    )
    fields = output.strip().split("|")
    if len(fields) != 3:
        fields = output.strip().split()
    if len(fields) != 3:
        raise RuntimeError(f"unexpected pg_stat_database output: {output!r}")
    return tuple(int(value.strip()) for value in fields)  # type: ignore[return-value]


def ap_counters() -> tuple[int, int, int, int, int]:
    output = tpc5stage.gsql_output(
        """
SELECT (xact_commit + xact_rollback)::bigint,
       temp_files::bigint,
       temp_bytes::bigint,
       blks_read::bigint,
       blks_hit::bigint
FROM pg_stat_database
WHERE datname = 'h5_tpch';
"""
    )
    fields = output.strip().split("|")
    if len(fields) != 5:
        fields = output.strip().split()
    if len(fields) != 5:
        raise RuntimeError(f"unexpected h5_tpch pg_stat_database output: {output!r}")
    return tuple(int(value.strip()) for value in fields)  # type: ignore[return-value]


def active_tpcc_sessions() -> int:
    output = tpc5stage.gsql_output(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'tpcc%';\n"
    )
    return int(output.strip().splitlines()[-1])


def wait_for_tpcc(expected: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if active_tpcc_sessions() >= expected:
            return
        time.sleep(0.5)
    raise TimeoutError(f"TPCC sessions did not reach {expected}; current={active_tpcc_sessions()}")


def wait_for_tpch_gone(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        sessions, _active = tpc5stage.tpch_activity_counts()
        if sessions == 0:
            return
        time.sleep(0.5)


def terminate_tpch_backends() -> None:
    tpc5stage.gsql_output(
        """
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE pid <> pg_backend_pid()
  AND (application_name LIKE 'tpch%' OR application_name = 'tpch_ap');
"""
    )


def build_runtime_args(args: argparse.Namespace) -> SimpleNamespace:
    total_seconds = args.tp_warmup_seconds + args.stage_count * (
        args.stage_warmup_seconds + args.measure_seconds + 20
    ) + 300
    return SimpleNamespace(
        seed=20260614,
        tpcc_warehouses=250,
        tpch_scale=85.0,
        ap_work_mem=args.ap_work_mem,
        ap_temp_file_limit="",
        tp_low_terminals=2,
        tp_low_rate=40,
        tp_high_terminals=args.tp_terminals,
        tp_high_rate="unlimited",
        stable_tp_high_rate="180",
        stable_workload=False,
        ap_rate="unlimited",
        ap_serial=True,
        ap_fixed_query_clients=True,
        ap_query_cycle="1,3,5,7,9,13,18,21",
        ap_s1=1,
        ap_s2=1,
        ap_s3=2,
        ap_s4=4,
        ap_s5=args.ap_s5_clients,
        stage_seconds=total_seconds,
        sample_interval=args.sample_interval,
        stage_boundary_mode="time",
        tpch_start_timeout_seconds=60.0,
        tpch_query_timeout_seconds=0.0,
        tp_run_seconds=total_seconds,
        total_seconds=total_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--sb-mb", required=True, type=int)
    parser.add_argument("--tp-terminals", type=int, default=32)
    parser.add_argument("--tp-warmup-seconds", type=int, default=45)
    parser.add_argument("--stage-warmup-seconds", type=int, default=30)
    parser.add_argument("--measure-seconds", type=int, default=90)
    parser.add_argument("--sample-interval", type=int, default=5)
    parser.add_argument("--ap-work-mem", default="1024MB")
    parser.add_argument(
        "--stage-work-mem",
        default="",
        help="comma-separated stage=work_mem overrides, for example stage2_reach_limit=1150MB",
    )
    parser.add_argument("--ap-s5-clients", type=int, default=4)
    parser.add_argument("--trace-output", type=Path)
    parser.add_argument(
        "--trace-script",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "bpftrace" / "trace_path_aware.bt",
    )
    parser.add_argument(
        "--stages",
        default=",".join(stage for stage, _key in STAGES),
        help="comma-separated stage names to execute",
    )
    args = parser.parse_args()
    try:
        stage_work_mem = parse_stage_work_mem(args.stage_work_mem)
    except ValueError as exc:
        parser.error(str(exc))

    selected = {value.strip() for value in args.stages.split(",") if value.strip()}
    stage_list = [item for item in STAGES if item[0] in selected]
    unknown = selected - {stage for stage, _key in STAGES}
    if unknown:
        raise SystemExit(f"unknown stages: {','.join(sorted(unknown))}")
    if not stage_list:
        raise SystemExit("no stages selected")
    args.stage_count = len(stage_list)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    runtime = build_runtime_args(args)
    paths = tpc5stage.render_configs(runtime)
    live = []
    rows: list[dict[str, object]] = []
    memory_rows: list[dict[str, object]] = []
    boundaries: list[dict[str, object]] = []
    bpf_proc: subprocess.Popen | None = None
    gzip_proc: subprocess.Popen | None = None
    trace_fh = None
    bpf_start_ns = 0

    tpc5stage.terminate_residual_workload_backends()
    try:
        if args.trace_output:
            args.trace_output.parent.mkdir(parents=True, exist_ok=True)
            gauss_pid = int(subprocess.check_output(["pgrep", "-x", "gaussdb"], text=True).splitlines()[0])
            trace_fh = args.trace_output.open("wb")
            gzip_proc = subprocess.Popen(["gzip", "-1"], stdin=subprocess.PIPE, stdout=trace_fh)
            if gzip_proc.stdin is None:
                raise RuntimeError("failed to open gzip stdin")
            bpf_start_ns = time.monotonic_ns()
            bpf_proc = subprocess.Popen(
                ["bpftrace", str(args.trace_script), str(gauss_pid)],
                stdout=gzip_proc.stdin,
                stderr=subprocess.STDOUT,
            )
            gzip_proc.stdin.close()
            time.sleep(2)
            if bpf_proc.poll() is not None:
                raise RuntimeError(f"bpftrace exited early with status {bpf_proc.returncode}")

        tp = tpc5stage.start(
            "tpcc_saturation",
            tpc5stage.benchbase_cmd(
                "tpcc",
                paths["tpcc_high"],
                create=False,
                load=False,
                execute=True,
                output_dir=args.out_dir / "benchbase",
            ),
            args.out_dir / "tpcc_saturation.log",
        )
        live.append(tp)
        wait_for_tpcc(args.tp_terminals)
        time.sleep(args.tp_warmup_seconds)

        for stage, ap_key in stage_list:
            ap_specs = []
            if ap_key is not None:
                effective_work_mem = stage_work_mem.get(stage, args.ap_work_mem)
                write_ap_setup(effective_work_mem)
                ap_specs = tpc5stage.start_configs(
                    stage,
                    "tpch",
                    paths[ap_key],
                    args.out_dir,
                )
                live.extend(ap_specs)
                tpc5stage.wait_for_tpch_query_start(ap_specs, 60.0)
            time.sleep(args.stage_warmup_seconds)

            if bpf_proc is not None:
                boundaries.append(
                    {
                        "label": f"{stage}_start",
                        "wall_time": time.strftime("%F %T"),
                        "elapsed_ns": time.monotonic_ns() - bpf_start_ns,
                    }
                )

            start_wall = time.time()
            start_tx, start_hit, start_read = db_counters()
            start_ap_tx, start_temp_files, start_temp_bytes, start_ap_read, start_ap_hit = ap_counters()
            vm_start = read_vmstat()
            samples = []
            cpu_samples = []
            tpc5stage.cpu_percent()
            previous_wall = start_wall
            previous_tx = start_tx
            deadline = start_wall + args.measure_seconds
            while time.time() < deadline:
                time.sleep(min(args.sample_interval, max(0.05, deadline - time.time())))
                now = time.time()
                tx, _hit, _read = db_counters()
                elapsed = now - previous_wall
                if elapsed > 0:
                    samples.append((tx - previous_tx) / elapsed)
                cpu = tpc5stage.cpu_percent()
                if cpu:
                    cpu_samples.append(float(cpu))
                memory_rows.append(memory_sample(stage, args.sb_mb, now - start_wall))
                previous_wall = now
                previous_tx = tx

            end_wall = time.time()
            end_tx, end_hit, end_read = db_counters()
            end_ap_tx, end_temp_files, end_temp_bytes, end_ap_read, end_ap_hit = ap_counters()
            vm_end = read_vmstat()
            if bpf_proc is not None:
                boundaries.append(
                    {
                        "label": f"{stage}_end",
                        "wall_time": time.strftime("%F %T"),
                        "elapsed_ns": time.monotonic_ns() - bpf_start_ns,
                    }
                )
            elapsed = end_wall - start_wall
            accesses = (end_hit - start_hit) + (end_read - start_read)
            stage_memory = [row for row in memory_rows if row["stage"] == stage]
            mem_available = [float(row["mem_available_mb"]) for row in stage_memory]
            file_cache = [float(row["file_cache_mb"]) for row in stage_memory]
            gauss_rss = [float(row["gauss_rss_mb"]) for row in stage_memory]
            psi_some = [float(row["psi_some_avg10"]) for row in stage_memory]
            psi_full = [float(row["psi_full_avg10"]) for row in stage_memory]
            rows.append(
                {
                    "stage": stage,
                    "sb_mb": args.sb_mb,
                    "ap_work_mem": stage_work_mem.get(stage, args.ap_work_mem),
                    "tp_terminals": args.tp_terminals,
                    "ap_clients": len(ap_specs),
                    "measure_seconds": f"{elapsed:.3f}",
                    "transactions": end_tx - start_tx,
                    "tps": f"{(end_tx - start_tx) / elapsed:.6f}",
                    "ap_transactions": end_ap_tx - start_ap_tx,
                    "ap_qps": f"{(end_ap_tx - start_ap_tx) / elapsed:.6f}",
                    "ap_temp_files_delta": end_temp_files - start_temp_files,
                    "ap_temp_bytes_delta": end_temp_bytes - start_temp_bytes,
                    "ap_blks_read_delta": end_ap_read - start_ap_read,
                    "ap_blks_hit_delta": end_ap_hit - start_ap_hit,
                    "sample_min_tps": f"{min(samples):.6f}" if samples else "",
                    "sample_max_tps": f"{max(samples):.6f}" if samples else "",
                    "cpu_avg_percent": f"{sum(cpu_samples) / len(cpu_samples):.3f}" if cpu_samples else "",
                    "cpu_min_percent": f"{min(cpu_samples):.3f}" if cpu_samples else "",
                    "cpu_max_percent": f"{max(cpu_samples):.3f}" if cpu_samples else "",
                    "sb_hit_rate": f"{(end_hit - start_hit) / accesses:.6f}" if accesses else "",
                    "mem_available_min_mb": f"{min(mem_available):.3f}" if mem_available else "",
                    "file_cache_min_mb": f"{min(file_cache):.3f}" if file_cache else "",
                    "gauss_rss_max_mb": f"{max(gauss_rss):.3f}" if gauss_rss else "",
                    "memory_psi_some_avg10_max": f"{max(psi_some):.3f}" if psi_some else "",
                    "memory_psi_full_avg10_max": f"{max(psi_full):.3f}" if psi_full else "",
                    "pgscan_delta": sum(
                        vm_end.get(key, 0) - vm_start.get(key, 0)
                        for key in ("pgscan_direct", "pgscan_kswapd")
                    ),
                    "pgsteal_delta": sum(
                        vm_end.get(key, 0) - vm_start.get(key, 0)
                        for key in ("pgsteal_direct", "pgsteal_kswapd")
                    ),
                    "workingset_refault_delta": sum(
                        vm_end.get(key, 0) - vm_start.get(key, 0)
                        for key in vm_end
                        if key.startswith("workingset_refault")
                    ),
                    "allocstall_delta": sum(
                        vm_end.get(key, 0) - vm_start.get(key, 0)
                        for key in vm_end
                        if key.startswith("allocstall")
                    ),
                }
            )

            for spec in reversed(ap_specs):
                tpc5stage.stop(spec)
            terminate_tpch_backends()
            wait_for_tpch_gone(timeout=5.0)

    finally:
        for spec in reversed(live):
            tpc5stage.stop(spec)
        tpc5stage.terminate_residual_workload_backends()
        if bpf_proc is not None and bpf_proc.poll() is None:
            bpf_proc.send_signal(signal.SIGTERM)
            try:
                bpf_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                bpf_proc.kill()
                bpf_proc.wait(timeout=10)
        if gzip_proc is not None:
            try:
                gzip_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                gzip_proc.kill()
                gzip_proc.wait(timeout=10)
        if trace_fh is not None:
            trace_fh.close()

    output = args.out_dir / "stage_tps.csv"
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    if memory_rows:
        with (args.out_dir / "runtime_memory_samples.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(memory_rows[0]))
            writer.writeheader()
            writer.writerows(memory_rows)
    (args.out_dir / "run_config.json").write_text(
        json.dumps(
            {
                **vars(args),
                "trace": str(args.trace_output) if args.trace_output else "",
                "shared_buffers_mb": args.sb_mb,
            },
            default=str,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if boundaries:
        with (args.out_dir / "boundaries.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["label", "wall_time", "elapsed_ns"])
            writer.writeheader()
            writer.writerows(boundaries)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
