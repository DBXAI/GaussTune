#!/usr/bin/env python3
"""Predict TPS from a pre-intervention TP window, then inject unseen I/O pressure."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import io_latency_sampler as sampler
import lwtid_io_trace
from block_io_attribution import parse_aggregate_trace
from io_latency_baseline import parse_tps
from mixed_io_latency_tps_formula import solve as solve_mixed_io
from mixed_storage_surface_formula import predict_added as predict_surface_added
from freeze_buffered_path_latency_transfer import predict_buffered_added


TP_PASSWORD = os.environ.get("HUAWEI6_TP_PASSWORD") or os.environ.get("HUAWEI5_TP_PASSWORD", "")


ORDER = (
    "r1_qd0", "r1_qd12", "r1_qd6", "r1_qd24",
    "r2_qd24", "r2_qd6", "r2_qd12", "r2_qd0",
    "r3_qd12", "r3_qd0", "r3_qd24", "r3_qd6",
)
MIXED_HOLDOUT_ORDER = (
    "r1_qd0", "r1_qd24", "r1_qd6",
    "r2_qd6", "r2_qd0", "r2_qd24",
    "r3_qd24", "r3_qd6", "r3_qd0",
)
SURFACE_DB_HOLDOUT_ORDER = (
    "r1_qd0", "r1_qd20", "r1_qd10",
    "r2_qd10", "r2_qd0", "r2_qd20",
    "r3_qd20", "r3_qd10", "r3_qd0",
)
BUFFERED_PATH_HOLDOUT_ORDER = (
    "r1_qd0", "r1_qd18", "r1_qd9",
    "r2_qd9", "r2_qd0", "r2_qd18",
    "r3_qd18", "r3_qd9", "r3_qd0",
)
DEFAULT_TERMINALS = 128


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def tp_command(seconds: int, workload: str, terminals: int) -> list[str]:
    if workload == "tpch_random_tid":
        return [
            "/usr/bin/stdbuf", "-oL", "-eL", "/usr/bin/sysbench",
            str(Path(__file__).resolve().parents[1] / "workloads" / "tpch_random_tid.lua"),
            "--db-driver=pgsql", "--pgsql-host=127.0.0.1", "--pgsql-port=5432",
            "--pgsql-user=h5_tpuser", f"--pgsql-password={TP_PASSWORD}",
            "--pgsql-db=h5_tpch", "--db-ps-mode=disable",
            f"--threads={terminals}", "--rate=0", f"--time={seconds}",
            "--report-interval=1", "--percentile=95", "run",
        ]
    return [
        "/usr/bin/stdbuf", "-oL", "-eL", "/usr/bin/sysbench",
        "/usr/share/sysbench/oltp_read_only.lua",
        "--db-driver=pgsql", "--pgsql-host=127.0.0.1", "--pgsql-port=5432",
        "--pgsql-user=h5_tpuser", f"--pgsql-password={TP_PASSWORD}",
        "--pgsql-db=h5_tpcc", "--db-ps-mode=disable", "--tables=16",
        "--table-size=1000000", f"--threads={terminals}", "--rate=0",
        f"--time={seconds}", "--report-interval=1", "--percentile=95", "run",
    ]


def io_command(file_dir: Path, depth: int, seconds: int) -> list[str]:
    return [
        "/usr/bin/taskset", "-c", "15", "/usr/bin/sysbench", "fileio",
        "--file-num=8", "--file-total-size=4G", "--file-block-size=128K",
        "--file-test-mode=rndrd", "--file-io-mode=async",
        f"--file-async-backlog={depth}", "--file-extra-flags=direct",
        "--file-fsync-freq=0", "--file-fsync-end=off", "--threads=1",
        "--rate=0", f"--time={seconds}", "--report-interval=1", "run",
    ]


def delta_windows(
    rows: list[dict[str, object]], start: float, end: float, commit_field: str,
) -> list[dict[str, float]]:
    windows = []
    for previous, current in zip(rows, rows[1:]):
        elapsed = float(current["elapsed_seconds"]) - float(previous["elapsed_seconds"])
        current_second = float(current["elapsed_seconds"])
        if elapsed <= 0.2 or elapsed > 2.0 or not (start <= current_second <= end):
            continue
        operations = (
            int(current["read_ios"]) - int(previous["read_ios"])
            + int(current["write_ios"]) - int(previous["write_ios"])
        )
        io_millis = (
            int(current["read_millis"]) - int(previous["read_millis"])
            + int(current["write_millis"]) - int(previous["write_millis"])
        )
        commits = int(current[commit_field]) - int(previous[commit_field])
        windows.append({
            "elapsed": elapsed,
            "operations": operations,
            "io_millis": io_millis,
            "commits": commits,
        })
    return windows


def create_online_prediction(
    case_id: str,
    depth: int,
    samples: list[dict[str, object]],
    frozen_formula_path: Path,
    pre_start: int,
    pre_end: int,
    case_dir: Path,
    trace: lwtid_io_trace.LwtidBlockTrace,
    terminals: int,
    commit_field: str,
    frozen_mixed_formula_path: Path | None,
    frozen_storage_surface_path: Path | None,
    frozen_buffered_transfer_path: Path | None,
) -> dict[str, object]:
    formula = json.loads(frozen_formula_path.read_text(encoding="utf-8"))
    params = formula["parameters"]["rndrd_128KiB"]
    windows = delta_windows(samples, pre_start, pre_end, commit_field)
    if len(windows) < 6:
        raise RuntimeError(f"insufficient pre-intervention windows for {case_id}")
    elapsed = sum(row["elapsed"] for row in windows)
    mapping = {int(row["lwtid"]): str(row["class"]) for row in trace.mappings}
    buckets = parse_aggregate_trace(
        case_dir / "block_trace" / "block_request_latency.csv",
        trace.started_monotonic_ns,
        mapping,
    )
    live_tps = {int(second): float(value) for second, value in parse_tps(case_dir / "sysbench_tp.log")}
    selected_seconds = sorted(
        second for second in set(buckets) & set(live_tps) if pre_start <= second <= pre_end
    )
    selected_buckets = [buckets[second] for second in selected_seconds]
    tp_operations = sum(
        bucket["tp_read_ops"] + bucket["tp_write_ops"] for bucket in selected_buckets
    )
    tp_latency_us = sum(
        bucket["tp_read_latency_us_sum"] + bucket["tp_write_latency_us_sum"]
        for bucket in selected_buckets
    )
    tp_bytes = sum(
        bucket["tp_read_bytes"] + bucket["tp_write_bytes"] for bucket in selected_buckets
    )
    transactions = sum(live_tps[second] for second in selected_seconds)
    if transactions <= 0 or tp_operations <= 0 or len(selected_buckets) < 5:
        raise RuntimeError(f"invalid pre-intervention counters for {case_id}")
    baseline_tps = transactions / len(selected_seconds)
    requests_per_transaction = tp_operations / transactions
    baseline_await_ms = tp_latency_us / tp_operations / 1000.0
    pre_tp_iops = tp_operations / len(selected_seconds)
    pre_tp_outstanding_depth = pre_tp_iops * baseline_await_ms / 1000.0
    mixed_result = None
    surface_added_await_ms = None
    buffered_added_await_ms = None
    if frozen_buffered_transfer_path is not None:
        transfer = json.loads(frozen_buffered_transfer_path.read_text(encoding="utf-8"))
        if transfer.get("contains_tps_labels"):
            raise RuntimeError("buffered path transfer contains TPS labels")
        buffered_added_await_ms = predict_buffered_added(transfer, depth)
        pressure_await_ms = baseline_await_ms + buffered_added_await_ms
    elif frozen_storage_surface_path is not None:
        surface = json.loads(frozen_storage_surface_path.read_text(encoding="utf-8"))
        if surface.get("contains_database_tps") or surface.get("contains_holdout_qd6_qd12_qd24"):
            raise RuntimeError("storage surface contains forbidden TPS or holdout data")
        surface_added_await_ms = predict_surface_added(surface, depth)
        pressure_await_ms = baseline_await_ms + surface_added_await_ms
    elif frozen_mixed_formula_path is not None:
        mixed = json.loads(frozen_mixed_formula_path.read_text(encoding="utf-8"))
        if mixed.get("contains_qd6_or_qd24_mixed_measurements") or mixed.get("contains_tps_labels"):
            raise RuntimeError("mixed formula contains forbidden holdout outcomes or TPS labels")
        mixed_result = solve_mixed_io(
            terminals, baseline_tps, requests_per_transaction, baseline_await_ms, depth,
            float(mixed["tp_8k_capacity_iops"]), float(mixed["ap_128k_capacity_iops"]),
        )
        pressure_await_ms = mixed_result["predicted_await_ms"]
    else:
        pressure_await_ms = baseline_await_ms if depth == 0 else max(
            float(params["service_floor_ms"]),
            1000.0 * (pre_tp_outstanding_depth + depth) / float(params["capacity_iops"]),
        )
    added_wait_ms = max(0.0, pressure_await_ms - baseline_await_ms)
    baseline_transaction_ms = terminals * 1000.0 / baseline_tps
    predicted_transaction_ms = baseline_transaction_ms + requests_per_transaction * added_wait_ms
    prediction = {
        "case_id": case_id,
        "external_queue_depth": depth,
        "prediction_created_epoch_seconds": time.time(),
        "prediction_created_before_intervention": True,
        "contains_post_intervention_tps": False,
        "pre_window_start_seconds": pre_start,
        "pre_window_end_seconds": pre_end,
        "pre_window_count": len(windows),
        "pre_aligned_bpf_tps_window_count": len(selected_seconds),
        "pre_tp_commit_tps": baseline_tps,
        "pre_device_requests_per_tp_transaction": requests_per_transaction,
        "pre_tp_request_await_ms": baseline_await_ms,
        "pre_tp_iops": pre_tp_iops,
        "pre_tp_mean_request_kib": tp_bytes / tp_operations / 1024.0,
        "pre_tp_outstanding_depth": pre_tp_outstanding_depth,
        "predicted_total_outstanding_depth": pre_tp_outstanding_depth + depth,
        "predicted_pressure_await_ms": pressure_await_ms,
        "predicted_added_wait_ms": added_wait_ms,
        "baseline_transaction_ms": baseline_transaction_ms,
        "predicted_added_transaction_ms": requests_per_transaction * added_wait_ms,
        "predicted_transaction_ms": predicted_transaction_ms,
        "terminals": terminals,
        "predicted_tp_tps": terminals * 1000.0 / predicted_transaction_ms,
        "latency_formula_sha256": hashlib.sha256(frozen_formula_path.read_bytes()).hexdigest(),
        "mixed_formula_sha256": (
            hashlib.sha256(frozen_mixed_formula_path.read_bytes()).hexdigest()
            if frozen_mixed_formula_path is not None else None
        ),
        "mixed_formula_result": mixed_result,
        "storage_surface_sha256": (
            hashlib.sha256(frozen_storage_surface_path.read_bytes()).hexdigest()
            if frozen_storage_surface_path is not None else None
        ),
        "storage_surface_added_await_ms": surface_added_await_ms,
        "buffered_transfer_sha256": (
            hashlib.sha256(frozen_buffered_transfer_path.read_bytes()).hexdigest()
            if frozen_buffered_transfer_path is not None else None
        ),
        "buffered_path_added_await_ms": buffered_added_await_ms,
        "tps_formula": "T_pred_ms = terminals*1000 / pre_TPS + pre_tp_requests_per_tx * max(0, predicted_await_ms - pre_await_ms)",
        "tps_conversion": "TPS_pred = terminals*1000 / T_pred_ms",
        "fitted_tps_coefficient": False,
    }
    prediction_path = case_dir / "online_prediction.json"
    prediction_path.write_text(json.dumps(prediction, indent=2) + "\n", encoding="utf-8")
    with prediction_path.open("r", encoding="utf-8") as handle:
        os.fsync(handle.fileno())
    return prediction


def summarize_case(
    case_dir: Path,
    case_id: str,
    repeat: int,
    depth: int,
    injection_start: int,
    injection_seconds: int,
    commit_field: str,
) -> dict[str, object]:
    prediction_path = case_dir / "online_prediction.json"
    marker = json.loads((case_dir / "intervention_marker.json").read_text(encoding="utf-8"))
    prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
    if prediction["prediction_created_epoch_seconds"] >= marker["intervention_started_epoch_seconds"]:
        raise RuntimeError(f"prediction was not frozen before intervention for {case_id}")
    tps_rows = [
        {"elapsed_seconds": int(elapsed), "tp_tps": value}
        for elapsed, value in parse_tps(case_dir / "sysbench_tp.log")
    ]
    write_csv(case_dir / "tp_tps_samples.csv", tps_rows)
    start, end = injection_start + 5, injection_start + injection_seconds - 2
    stable_tps = [float(row["tp_tps"]) for row in tps_rows if start <= int(row["elapsed_seconds"]) <= end]
    samples = read_csv(case_dir / "io_latency_samples.csv")
    stable_windows = delta_windows(samples, start, end, commit_field)
    trace_rows = {
        int(row["elapsed_second"]): row
        for row in read_csv(case_dir / "block_trace_attribution.csv")
        if start <= int(row["elapsed_second"]) <= end
    }
    tps_by_second = {int(row["elapsed_seconds"]): float(row["tp_tps"]) for row in tps_rows}
    aligned = sorted(set(trace_rows) & set(tps_by_second))
    tp_ops = sum(int(trace_rows[second]["tp_read_ops"]) + int(trace_rows[second]["tp_write_ops"]) for second in aligned)
    tp_latency_us = sum(
        int(trace_rows[second]["tp_read_latency_us_sum"]) + int(trace_rows[second]["tp_write_latency_us_sum"])
        for second in aligned
    )
    tp_bytes = sum(
        int(trace_rows[second]["tp_read_bytes"]) + int(trace_rows[second]["tp_write_bytes"])
        for second in aligned
    )
    if len(stable_tps) < 12 or len(stable_windows) < 12 or len(aligned) < 12:
        raise RuntimeError(f"insufficient post-intervention windows for {case_id}")
    elapsed = sum(row["elapsed"] for row in stable_windows)
    commits = sum(row["commits"] for row in stable_windows)
    operations = sum(row["operations"] for row in stable_windows)
    io_millis = sum(row["io_millis"] for row in stable_windows)
    transactions = sum(tps_by_second[second] for second in aligned)
    return {
        "case_id": case_id,
        "repeat": repeat,
        "external_queue_depth": depth,
        "prediction_created_before_intervention": True,
        "stable_window_count": len(stable_windows),
        "actual_sysbench_tp_tps": sum(stable_tps) / len(stable_tps),
        "actual_db_commit_tps": commits / elapsed,
        "actual_device_iops": operations / elapsed,
        "actual_device_await_ms": io_millis / operations,
        "actual_tp_requests_per_transaction": tp_ops / transactions,
        "actual_tp_mean_request_kib": tp_bytes / tp_ops / 1024.0 if tp_ops else 0.0,
        "actual_tp_request_await_ms": tp_latency_us / tp_ops / 1000.0 if tp_ops else 0.0,
        "tp_completed_normally": True,
        "external_io_completed_normally": True,
    }


def run_case(
    case_id: str,
    frozen_formula_path: Path,
    file_dir: Path,
    out_dir: Path,
    device: str,
    tp_seconds: int,
    injection_start: int,
    injection_seconds: int,
    pre_start: int,
    pre_end: int,
    terminals: int,
    workload: str,
    commit_field: str,
    frozen_mixed_formula_path: Path | None,
    frozen_storage_surface_path: Path | None,
    frozen_buffered_transfer_path: Path | None,
) -> None:
    repeat = int(case_id[1])
    depth = int(case_id.split("qd", 1)[1])
    case_dir = out_dir / case_id
    if (case_dir / "case_summary.json").exists():
        return
    case_dir.mkdir(parents=True, exist_ok=True)
    trace = lwtid_io_trace.LwtidBlockTrace(
        case_dir / "block_trace", device,
        Path(__file__).resolve().parents[1] / "bpftrace" / "lwtid_block_latency_aggregate.bt",
    )
    trace.start()
    started = time.monotonic()
    samples: list[dict[str, object]] = []
    environment = os.environ.copy()
    environment["PGAPPNAME"] = f"sysbench_tp_online_{case_id}"
    io_process = None
    io_handle = None
    prediction_created = False
    marker_created = False
    with (case_dir / "sysbench_tp.log").open("w", encoding="utf-8") as tp_handle:
        tp_process = subprocess.Popen(
            tp_command(tp_seconds, workload, terminals),
            stdout=tp_handle, stderr=subprocess.STDOUT, env=environment,
        )
        try:
            while tp_process.poll() is None:
                elapsed = time.monotonic() - started
                samples.append(sampler.sample(device, started))
                trace.snapshot_lwtids(elapsed)
                if not prediction_created and elapsed >= pre_end:
                    create_online_prediction(
                        case_id, depth, samples, frozen_formula_path, pre_start, pre_end,
                        case_dir, trace, terminals, commit_field,
                        frozen_mixed_formula_path,
                        frozen_storage_surface_path,
                        frozen_buffered_transfer_path,
                    )
                    prediction_created = True
                if prediction_created and not marker_created and elapsed >= injection_start:
                    marker = {
                        "case_id": case_id,
                        "intervention_started_epoch_seconds": time.time(),
                        "external_queue_depth": depth,
                    }
                    (case_dir / "intervention_marker.json").write_text(
                        json.dumps(marker, indent=2) + "\n", encoding="utf-8",
                    )
                    marker_created = True
                    if depth > 0:
                        io_handle = (case_dir / "sysbench_fileio.log").open("w", encoding="utf-8")
                        io_process = subprocess.Popen(
                            io_command(file_dir, depth, injection_seconds),
                            cwd=file_dir, stdout=io_handle, stderr=subprocess.STDOUT,
                        )
                time.sleep(1.0)
            if tp_process.wait() != 0:
                raise RuntimeError(f"TP process failed for {case_id}")
            if io_process is not None and io_process.wait() != 0:
                raise RuntimeError(f"I/O process failed for {case_id}")
        finally:
            trace.stop()
            if io_handle is not None:
                io_handle.close()
    write_csv(case_dir / "io_latency_samples.csv", samples)
    subprocess.run([
        sys.executable, str(Path(__file__).resolve().parent / "block_io_attribution.py"),
        "--trace-dir", str(case_dir / "block_trace"),
        "--out", str(case_dir / "block_trace_attribution.csv"),
    ], check=True)
    summary = summarize_case(
        case_dir, case_id, repeat, depth, injection_start, injection_seconds, commit_field,
    )
    (case_dir / "case_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-latency-formula", required=True, type=Path)
    parser.add_argument("--frozen-mixed-formula", type=Path)
    parser.add_argument("--frozen-storage-surface", type=Path)
    parser.add_argument("--frozen-buffered-transfer", type=Path)
    parser.add_argument("--file-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--device", default="nvme0n1")
    parser.add_argument("--tp-seconds", type=int, default=58)
    parser.add_argument("--injection-start", type=int, default=22)
    parser.add_argument("--injection-seconds", type=int, default=28)
    parser.add_argument("--pre-start", type=int, default=12)
    parser.add_argument("--pre-end", type=int, default=20)
    parser.add_argument("--terminals", type=int, default=DEFAULT_TERMINALS)
    parser.add_argument("--workload", choices=("tpcc", "tpch_random_tid"), default="tpcc")
    parser.add_argument(
        "--case-set",
        choices=("default", "mixed_holdout", "surface_db_holdout", "buffered_path_holdout"),
        default="default",
    )
    args = parser.parse_args()
    if not args.frozen_latency_formula.exists():
        parser.error("frozen latency formula does not exist")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.case_set == "default":
        order = ORDER
    elif args.case_set == "mixed_holdout":
        order = MIXED_HOLDOUT_ORDER
    elif args.case_set == "surface_db_holdout":
        order = SURFACE_DB_HOLDOUT_ORDER
    else:
        order = BUFFERED_PATH_HOLDOUT_ORDER
    manifest = {
        "mode": "online_pre_intervention_prediction_then_unseen_io_holdout",
        "created_epoch_seconds": time.time(),
        "execution_order": order,
        "unseen_external_queue_depths": (
            [6, 24] if args.case_set == "mixed_holdout"
            else (
                [10, 20] if args.case_set == "surface_db_holdout"
                else ([9, 18] if args.case_set == "buffered_path_holdout" else [6, 12, 24])
            )
        ),
        "prediction_uses_post_intervention_tps": False,
        "tps_formula_coefficient": 1.0,
        "terminals": args.terminals,
        "workload": args.workload,
        "latency_formula_sha256": hashlib.sha256(args.frozen_latency_formula.read_bytes()).hexdigest(),
        "mixed_formula_sha256": (
            hashlib.sha256(args.frozen_mixed_formula.read_bytes()).hexdigest()
            if args.frozen_mixed_formula is not None else None
        ),
        "storage_surface_sha256": (
            hashlib.sha256(args.frozen_storage_surface.read_bytes()).hexdigest()
            if args.frozen_storage_surface is not None else None
        ),
        "buffered_transfer_sha256": (
            hashlib.sha256(args.frozen_buffered_transfer.read_bytes()).hexdigest()
            if args.frozen_buffered_transfer is not None else None
        ),
    }
    (args.out_dir / "experiment_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for sequence, case_id in enumerate(order, 1):
        run_case(
            case_id, args.frozen_latency_formula, args.file_dir, args.out_dir, args.device,
            args.tp_seconds, args.injection_start, args.injection_seconds, args.pre_start, args.pre_end,
            args.terminals, args.workload,
            "tp_xact_commit" if args.workload == "tpcc" else "ap_xact_commit",
            args.frozen_mixed_formula,
            args.frozen_storage_surface,
            args.frozen_buffered_transfer,
        )
        print(json.dumps({"completed_sequence": sequence, "case_id": case_id}), flush=True)
        time.sleep(2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
