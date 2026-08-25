#!/usr/bin/env python3
"""Synchronously collect buffer events and TP block reads for OS-cache holdout."""

import argparse
import json
import os
import pwd
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.attribution import AttributionIndex, read_snapshots
from huawei7.block_trace import parse_block_aggregate, raw_device_number
from huawei7.cache_replay import validate_observed_hits
from huawei7.schema import PAGE_SIZE, write_trace
from huawei7.trace import inspect_binary_probe, normalize_path
from huawei7.trace_quality import trace_quality


def stop(process):
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if process.returncode not in (0, 130):
        raise RuntimeError("probe failed with status %d" % process.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--target-db-node", type=int, required=True)
    parser.add_argument("--control-dsn", required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--warmup-seconds", type=float, default=30)
    parser.add_argument("--measure-seconds", type=float, default=60)
    parser.add_argument("--snapshot-interval-ms", type=float, default=100)
    parser.add_argument("--attribution-max-age-ms", type=float, default=300)
    parser.add_argument("--minimum-tp-access-fraction", type=float, default=.90)
    parser.add_argument("--minimum-tp-block-fraction", type=float, default=.90)
    parser.add_argument("--actual-shared-buffers-mb", type=float, required=True)
    parser.add_argument("--maximum-hit-mismatch-fraction", type=float, default=.01)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("synchronized uprobes/tracepoints require root")
    if args.warmup_seconds < 0 or args.measure_seconds < 3:
        parser.error("warmup must be nonnegative and measurement >=3 seconds")
    args.out_dir.mkdir(parents=True, exist_ok=False)
    mapping = args.out_dir / "lwtid_attribution.csv"
    mapping.touch()
    omm = pwd.getpwnam("omm")
    os.chown(mapping, 0, omm.pw_gid)
    mapping.chmod(0o660)
    buffer_raw = args.out_dir / "buffer_trace.raw"
    block_raw = args.out_dir / "block_trace.raw"
    observer_log = args.out_dir / "attribution_observer.log"
    handles = [
        buffer_raw.open("wb"),
        (args.out_dir / "buffer_trace.stderr").open("w", encoding="utf-8"),
        block_raw.open("w", encoding="utf-8"),
        (args.out_dir / "block_trace.stderr").open("w", encoding="utf-8"),
        observer_log.open("w", encoding="utf-8"),
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    total = args.warmup_seconds + args.measure_seconds
    buffer_probe = subprocess.Popen([
        sys.executable,
        str(ROOT / "probes" / "opengauss_buffer_trace_bcc.py"),
        str(args.target_db_node),
    ], stdout=handles[0], stderr=handles[1])
    block_probe = subprocess.Popen([
        "stdbuf", "-oL", "-eL", "bpftrace",
        str(ROOT / "probes" / "block_rq_aggregate.bt"),
        str(raw_device_number(args.device)),
    ], stdout=handles[2], stderr=handles[3], text=True)
    observer = subprocess.Popen([
        "runuser", "-u", "omm", "--", sys.executable,
        str(ROOT / "scripts" / "snapshot_sessions.py"),
        "--dsn", args.control_dsn, "--target-database", args.target_database,
        "--seconds", str(total + 2), "--interval-ms", str(args.snapshot_interval_ms),
        "--out", str(mapping),
    ], stdout=handles[4], stderr=subprocess.STDOUT, text=True, env=environment)
    try:
        time.sleep(1.25)
        if any(process.poll() is not None for process in (buffer_probe, block_probe, observer)):
            raise RuntimeError("probe/observer failed during attachment")
        capture_start = time.monotonic_ns()
        warmup_end = capture_start + int(args.warmup_seconds * 1e9)
        measure_end = warmup_end + int(args.measure_seconds * 1e9)
        while time.monotonic_ns() < measure_end:
            time.sleep(min(.25, max(0, (measure_end - time.monotonic_ns()) / 1e9)))
        stop(buffer_probe)
        stop(block_probe)
        observer_status = observer.wait(timeout=15)
        if observer_status != 0:
            raise RuntimeError("attribution observer failed with status %d" % observer_status)
    finally:
        for process in (buffer_probe, block_probe):
            if process.poll() is None:
                stop(process)
        if observer.poll() is None:
            observer.terminate()
            observer.wait(timeout=5)
        for handle in handles:
            handle.close()
    mapping.chmod(0o640)
    attribution = AttributionIndex(read_snapshots(mapping))
    probe_summary = inspect_binary_probe(buffer_raw)
    events = normalize_path(
        buffer_raw, warmup_end_ns=warmup_end, measure_end_ns=measure_end,
        attribution=attribution,
        attribution_max_age_ns=int(args.attribution_max_age_ms * 1e6),
    )
    quality = trace_quality(
        events, target_db_node=args.target_db_node,
        minimum_tp_access_fraction=args.minimum_tp_access_fraction,
    )
    cache_validation = validate_observed_hits(
        events,
        actual_shared_buffer_pages=int(
            args.actual_shared_buffers_mb * 1024 * 1024 // PAGE_SIZE
        ),
        maximum_mismatch_fraction=args.maximum_hit_mismatch_fraction,
    )
    if not cache_validation.valid:
        raise RuntimeError(
            "actual-capacity cache replay failed: mismatch=%.6f, measured state anomalies=%d"
            % (
                cache_validation.mismatch_fraction,
                cache_validation.measured_state_anomalies,
            )
        )
    trace_path = args.out_dir / "buffer_trace.csv"
    write_trace(trace_path, events)
    with block_raw.open(encoding="utf-8", errors="replace") as handle:
        block = parse_block_aggregate(
            handle, attribution=attribution, start_ns=warmup_end, end_ns=measure_end,
            attribution_max_age_ns=int(args.attribution_max_age_ms * 1e6),
        )
    if block.collisions or block.orphans:
        raise RuntimeError("block trace collisions/orphans invalidate OS-cache evidence")
    total_requests = sum(row.requests for row in block.rows)
    tp_requests = sum(row.requests for row in block.rows if row.workload_class == "tp")
    tp_fraction = tp_requests / total_requests if total_requests else 0.0
    if tp_fraction < args.minimum_tp_block_fraction:
        raise RuntimeError("TP block attribution fraction %.6f is below gate" % tp_fraction)
    result = {
        "schema": "huawei7.synchronized-cache-validation/v1",
        "trace_id": args.trace_id,
        "machine_fingerprint": args.machine_fingerprint,
        "device": str(args.device), "raw_device_number": raw_device_number(args.device),
        "target_database": args.target_database, "target_db_node": args.target_db_node,
        "capture_start_ns": capture_start, "warmup_end_ns": warmup_end,
        "measure_end_ns": measure_end, "trace_quality": quality,
        "cache_validation": asdict(cache_validation),
        "buffer_probe_summary": probe_summary,
        "tp_block_request_fraction": tp_fraction,
        "block_summary": {
            **asdict(block),
            "rows": [dict(asdict(row), service_time_ms=row.service_time_ms)
                     for row in block.rows],
        },
        "trace_csv": str(trace_path.resolve()), "valid": True,
    }
    (args.out_dir / "collection.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
