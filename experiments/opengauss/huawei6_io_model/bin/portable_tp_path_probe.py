#!/usr/bin/env python3
"""Collect portable openGauss buffered-I/O path anchors and frozen holdouts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import lwtid_io_trace
from block_io_attribution import parse_aggregate_trace
from io_latency_baseline import parse_tps
from portable_joint_model import predict_latency, read_json


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    return value


def format_command(template: list[object], **values: object) -> list[str]:
    return [str(item).format(**values) for item in template]


def gsql(config: dict[str, Any], sql: str) -> str:
    database = config["database"]
    gausshome = str(database["gausshome"])
    command = [
        str(database.get("gsql", Path(gausshome) / "bin" / "gsql")),
        "-d", str(database.get("health_database", "postgres")),
        "-At", "-F", ",", "-c", sql,
    ]
    library_path = str(database.get(
        "library_path", f"{gausshome}/lib:{gausshome}/lib/postgresql",
    ))
    os_user = str(database.get("os_user", ""))
    if os_user and os.geteuid() == 0:
        shell = f"export LD_LIBRARY_PATH={shlex.quote(library_path)}; {shlex.join(command)}"
        return subprocess.check_output(["su", "-", os_user, "-c", shell], text=True).strip()
    environment = os.environ.copy()
    environment["LD_LIBRARY_PATH"] = library_path
    return subprocess.check_output(command, text=True, env=environment).strip()


def snapshot_mapping(
    config: dict[str, Any], trace: lwtid_io_trace.LwtidBlockTrace, prefix: str, elapsed: float,
) -> None:
    escaped = prefix.replace("'", "''")
    sql = f"""
SELECT w.lwtid || ',' || a.application_name || ',' || a.datname
FROM pg_thread_wait_status w
JOIN pg_stat_activity a ON a.sessionid = w.sessionid
WHERE a.application_name LIKE '{escaped}%';
"""
    try:
        output = gsql(config, sql)
    except subprocess.CalledProcessError:
        return
    for line in output.splitlines():
        fields = line.split(",", 2)
        if len(fields) != 3:
            continue
        lwtid, application_name, database = fields
        trace.mappings.append({
            "elapsed_seconds": round(elapsed, 6),
            "lwtid": int(lwtid),
            "application_name": application_name,
            "database": database,
            "class": "tp",
        })


def fileio_command(config: dict[str, Any], depth: int, seconds: int) -> list[str]:
    storage = config["storage_probe"]
    return [
        "taskset", "-c", str(storage["ap_cpus"]), "sysbench", "fileio",
        f"--file-num={int(storage.get('file_num', 8))}",
        f"--file-total-size={storage.get('file_total_size', '4G')}",
        f"--file-block-size={int(storage.get('ap_block_kib', 128))}K",
        "--file-test-mode=rndrd", "--file-io-mode=async",
        f"--file-async-backlog={depth}", "--file-extra-flags=direct",
        "--file-fsync-freq=0", "--file-fsync-end=off", "--threads=1",
        "--rate=0", f"--time={seconds}", "--report-interval=1", "run",
    ]


def trace_buckets(
    trace: lwtid_io_trace.LwtidBlockTrace, trace_dir: Path,
) -> dict[int, dict[str, float]]:
    mapping = {int(row["lwtid"]): str(row["class"]) for row in trace.mappings}
    return parse_aggregate_trace(
        trace_dir / "block_request_latency.csv", trace.started_monotonic_ns, mapping,
    )


def summarize_window(
    buckets: dict[int, dict[str, float]], tps: dict[int, float], start: int, end: int,
) -> dict[str, float]:
    seconds = sorted(second for second in set(buckets) & set(tps) if start <= second <= end)
    if len(seconds) < 5:
        raise RuntimeError(f"only {len(seconds)} aligned BPF/TPS windows in [{start}, {end}]")
    operations = sum(
        buckets[second]["tp_read_ops"] + buckets[second]["tp_write_ops"] for second in seconds
    )
    latency_us = sum(
        buckets[second]["tp_read_latency_us_sum"] + buckets[second]["tp_write_latency_us_sum"]
        for second in seconds
    )
    byte_count = sum(
        buckets[second]["tp_read_bytes"] + buckets[second]["tp_write_bytes"] for second in seconds
    )
    transactions = sum(tps[second] for second in seconds)
    if operations <= 0 or transactions <= 0:
        raise RuntimeError("aligned window has no TP I/O or transactions")
    return {
        "windows": len(seconds),
        "tps": transactions / len(seconds),
        "critical_io_per_tx": operations / transactions,
        "await_ms": latency_us / operations / 1000.0,
        "mean_request_kib": byte_count / operations / 1024.0,
        "iops": operations / len(seconds),
    }


def persist_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_case(
    config: dict[str, Any], mode: str, depth: int, repeat: int, out_dir: Path,
    frozen_model: dict[str, Any] | None,
) -> dict[str, Any]:
    anchor = config["tp_anchor"]
    storage = config["storage_probe"]
    case_id = f"r{repeat}_qd{depth}"
    case_dir = out_dir / case_id
    summary_path = case_dir / "case_summary.json"
    if summary_path.exists():
        return read_json(summary_path)
    case_dir.mkdir(parents=True, exist_ok=True)
    seconds = int(anchor.get("seconds", 45))
    terminals = int(anchor["terminals"])
    pre_start = int(anchor.get("pre_start", 8))
    pre_end = int(anchor.get("pre_end", 16))
    injection_start = int(anchor.get("injection_start", 18))
    injection_seconds = int(anchor.get("injection_seconds", 22))
    if seconds < injection_start + injection_seconds:
        raise ValueError("tp_anchor.seconds must cover the complete I/O intervention")
    command = format_command(
        list(anchor["command"]), seconds=seconds, terminals=terminals, case_id=case_id,
    )
    if not command or "stdbuf" not in Path(command[0]).name:
        command = ["stdbuf", "-oL", "-eL", *command]
    prefix = str(anchor.get("application_prefix", "sysbench_tp_portable"))
    application_name = f"{prefix}_{case_id}"
    environment = os.environ.copy()
    environment["PGAPPNAME"] = application_name
    trace_dir = case_dir / "block_trace"
    trace = lwtid_io_trace.LwtidBlockTrace(
        trace_dir,
        str(config["device"]),
        Path(__file__).resolve().parents[1] / "bpftrace" / "lwtid_block_latency_aggregate.bt",
    )
    trace.start()
    tp_handle = (case_dir / "tp.log").open("w", encoding="utf-8")
    tp_process = subprocess.Popen(command, stdout=tp_handle, stderr=subprocess.STDOUT, env=environment)
    io_process: subprocess.Popen[str] | None = None
    io_handle = None
    started = time.monotonic()
    pre_summary: dict[str, float] | None = None
    prediction: dict[str, Any] | None = None
    injection_started_epoch: float | None = None
    try:
        while tp_process.poll() is None:
            elapsed = time.monotonic() - started
            snapshot_mapping(config, trace, application_name, elapsed)
            if pre_summary is None and elapsed >= pre_end:
                buckets = trace_buckets(trace, trace_dir)
                live_tps = {
                    int(second): float(value) for second, value in parse_tps(case_dir / "tp.log")
                }
                pre_summary = summarize_window(buckets, live_tps, pre_start, pre_end)
                prediction = {
                    "case_id": case_id,
                    "mode": mode,
                    "ap_queue_depth": depth,
                    "created_epoch_seconds": time.time(),
                    "created_before_intervention": True,
                    "terminals": terminals,
                    "baseline_tp_tps": pre_summary["tps"],
                    "baseline_tp_critical_io_per_tx": pre_summary["critical_io_per_tx"],
                    "baseline_tp_await_ms": pre_summary["await_ms"],
                    "tp_mean_request_kib": pre_summary["mean_request_kib"],
                    "contains_pressure_tps": False,
                }
                if frozen_model is not None:
                    model_latency, _, _ = predict_latency(frozen_model, depth)
                    model_l0 = float(frozen_model["tp_anchor"]["baseline_tp_await_ms"])
                    predicted_latency = pre_summary["await_ms"] + max(0.0, model_latency - model_l0)
                    base_response_ms = terminals * 1000.0 / pre_summary["tps"]
                    added_ms = pre_summary["critical_io_per_tx"] * max(
                        0.0, predicted_latency - pre_summary["await_ms"],
                    )
                    prediction.update({
                        "frozen_model_sha256": hashlib.sha256(
                            json.dumps(frozen_model, sort_keys=True).encode()
                        ).hexdigest(),
                        "predicted_tp_await_ms": predicted_latency,
                        "predicted_tp_tps": terminals * 1000.0 / (base_response_ms + added_ms),
                    })
                persist_json(case_dir / "online_prediction.json", prediction)
            if pre_summary is not None and injection_started_epoch is None and elapsed >= injection_start:
                injection_started_epoch = time.time()
                persist_json(case_dir / "intervention_marker.json", {
                    "case_id": case_id,
                    "started_epoch_seconds": injection_started_epoch,
                    "ap_queue_depth": depth,
                })
                if depth > 0:
                    io_handle = (case_dir / "ap_fileio.log").open("w", encoding="utf-8")
                    io_process = subprocess.Popen(
                        fileio_command(config, depth, injection_seconds),
                        cwd=Path(str(storage["file_dir"])),
                        stdout=io_handle,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
            time.sleep(1.0)
        if tp_process.wait() != 0:
            raise RuntimeError(f"TP command failed for {case_id}; see {case_dir / 'tp.log'}")
        if io_process is not None and io_process.wait() != 0:
            raise RuntimeError(f"fileio failed for {case_id}; see {case_dir / 'ap_fileio.log'}")
    finally:
        # Never cancel a workload statement. An error waits for natural command completion.
        if tp_process.poll() is None:
            tp_process.wait()
        if io_process is not None and io_process.poll() is None:
            io_process.wait()
        trace.stop()
        tp_handle.close()
        if io_handle is not None:
            io_handle.close()
    if pre_summary is None or prediction is None or injection_started_epoch is None:
        raise RuntimeError(f"case {case_id} ended before prediction/intervention")
    if float(prediction["created_epoch_seconds"]) >= injection_started_epoch:
        raise RuntimeError(f"prediction leakage detected for {case_id}")
    buckets = trace_buckets(trace, trace_dir)
    tps = {int(second): float(value) for second, value in parse_tps(case_dir / "tp.log")}
    post = summarize_window(
        buckets,
        tps,
        injection_start + int(anchor.get("post_warmup", 5)),
        injection_start + injection_seconds - int(anchor.get("post_tail", 2)),
    )
    summary = {
        "case_id": case_id,
        "mode": mode,
        "repeat": repeat,
        "ap_queue_depth": depth,
        "terminals": terminals,
        "prediction_created_before_intervention": True,
        "baseline_tp_tps": pre_summary["tps"],
        "baseline_tp_critical_io_per_tx": pre_summary["critical_io_per_tx"],
        "baseline_tp_await_ms": pre_summary["await_ms"],
        "tp_mean_request_kib": pre_summary["mean_request_kib"],
        "pressure_tp_tps": post["tps"],
        "pressure_tp_critical_io_per_tx": post["critical_io_per_tx"],
        "pressure_tp_await_ms": post["await_ms"],
        "completed_naturally": True,
    }
    if frozen_model is not None:
        predicted_tps = float(prediction["predicted_tp_tps"])
        predicted_latency = float(prediction["predicted_tp_await_ms"])
        summary.update({
            "predicted_tp_tps": predicted_tps,
            "predicted_tp_await_ms": predicted_latency,
            "tps_absolute_percent_error": abs(predicted_tps - post["tps"]) / post["tps"] * 100.0,
            "latency_absolute_percent_error": abs(predicted_latency - post["await_ms"]) / post["await_ms"] * 100.0,
        })
    persist_json(summary_path, summary)
    return summary


def balanced_order(depths: list[int], repeats: int) -> list[tuple[int, int]]:
    output = []
    for repeat in range(1, repeats + 1):
        ordered = depths if repeat % 2 else list(reversed(depths))
        shift = (repeat - 1) % len(ordered)
        ordered = ordered[shift:] + ordered[:shift]
        output.extend((repeat, depth) for depth in ordered)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--mode", choices=("anchor", "holdout"), required=True)
    parser.add_argument("--depths", required=True, help="comma-separated AP queue depths")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--model", type=Path)
    args = parser.parse_args()
    config = expand_env(read_json(args.config))
    depths = [int(value) for value in args.depths.split(",") if value]
    if not depths or any(depth <= 0 for depth in depths):
        parser.error("depths must contain positive integers")
    if args.mode == "holdout" and args.model is None:
        parser.error("holdout mode requires --model")
    model = read_json(args.model) if args.model else None
    args.out_dir.mkdir(parents=True, exist_ok=True)
    cases = []
    for sequence, (repeat, depth) in enumerate(balanced_order(depths, args.repeats), 1):
        cases.append(run_case(config, args.mode, depth, repeat, args.out_dir, model))
        print(json.dumps({"completed": sequence, "repeat": repeat, "depth": depth}), flush=True)
    if args.mode == "anchor":
        document = {
            "schema": "huawei6.tp-path-anchors/v1",
            "created_epoch_seconds": time.time(),
            "contains_pressure_tps_for_path_fit": False,
            "model_builder_fields": [
                "ap_queue_depth", "terminals", "baseline_tp_tps",
                "baseline_tp_critical_io_per_tx", "baseline_tp_await_ms",
                "pressure_tp_await_ms", "tp_mean_request_kib",
            ],
            "cases": [
                {key: case[key] for key in (
                    "case_id", "repeat", "ap_queue_depth", "terminals", "baseline_tp_tps",
                    "baseline_tp_critical_io_per_tx", "baseline_tp_await_ms",
                    "pressure_tp_await_ms", "tp_mean_request_kib",
                )}
                for case in cases
            ],
        }
        persist_json(args.out_dir / "anchors.json", document)
    else:
        tps_mape = statistics.fmean(float(case["tps_absolute_percent_error"]) for case in cases)
        latency_mape = statistics.fmean(float(case["latency_absolute_percent_error"]) for case in cases)
        report = {
            "schema": "huawei6.portable-model-holdout/v1",
            "model": str(args.model.resolve()),
            "model_sha256": hashlib.sha256(args.model.read_bytes()).hexdigest(),
            "case_count": len(cases),
            "latency_mape_pct": latency_mape,
            "tps_mape_pct": tps_mape,
            "acceptance": {
                "latency_mape_at_most_10_pct": latency_mape <= 10.0,
                "tps_mape_at_most_5_pct": tps_mape <= 5.0,
                "all_natural_completion": all(bool(case["completed_naturally"]) for case in cases),
            },
        }
        report["acceptance"]["passed"] = all(report["acceptance"].values())
        persist_json(args.out_dir / "holdout_report.json", report)
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
