#!/usr/bin/env python3
"""Collect target-device AP/TP request counts and service times without guesses."""

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
from huawei7.provenance import sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, type=Path,
                        help="whole request-queue device, e.g. /dev/nvme0n1")
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--control-dsn", required=True)
    parser.add_argument("--warmup-seconds", type=float, default=3.0)
    parser.add_argument("--measure-seconds", type=float, default=10.0)
    parser.add_argument("--snapshot-interval-ms", type=float, default=100.0)
    parser.add_argument("--attribution-max-age-ms", type=float, default=300.0)
    parser.add_argument("--required-class", choices=("tp", "ap"), required=True)
    parser.add_argument("--minimum-class-request-fraction", type=float, default=0.90)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("block tracepoint collection requires root")
    if args.warmup_seconds < 0 or args.measure_seconds < 3:
        parser.error("warmup must be nonnegative; measure must be at least 3 seconds")
    if not 0 <= args.minimum_class_request_fraction <= 1:
        parser.error("minimum class fraction must be in [0,1]")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = args.out_dir / "block_trace.raw"
    stderr_path = args.out_dir / "block_trace.stderr"
    mapping_path = args.out_dir / "lwtid_attribution.csv"
    observer_log_path = args.out_dir / "attribution_observer.log"
    mapping_path.touch(exist_ok=False)
    omm = pwd.getpwnam("omm")
    os.chown(mapping_path, 0, omm.pw_gid)
    mapping_path.chmod(0o660)

    total = args.warmup_seconds + args.measure_seconds
    probe_script = ROOT / "probes" / "block_rq_aggregate.bt"
    snapshot_script = ROOT / "scripts" / "snapshot_sessions.py"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    with raw_path.open("w", encoding="utf-8") as raw_handle, \
            stderr_path.open("w", encoding="utf-8") as error_handle, \
            observer_log_path.open("w", encoding="utf-8") as observer_log:
        probe = subprocess.Popen(
            ["stdbuf", "-oL", "-eL", "bpftrace", str(probe_script),
             str(raw_device_number(args.device))],
            stdout=raw_handle, stderr=error_handle, text=True,
        )
        observer = subprocess.Popen(
            ["runuser", "-u", "omm", "--", sys.executable, str(snapshot_script),
             "--dsn", args.control_dsn,
             "--target-database", args.target_database,
             "--seconds", str(total + 2.0),
             "--interval-ms", str(args.snapshot_interval_ms),
             "--out", str(mapping_path)],
            stdout=observer_log, stderr=subprocess.STDOUT, text=True,
            env=environment,
        )
        time.sleep(1.0)
        if probe.poll() is not None or observer.poll() is not None:
            raise RuntimeError("probe/observer failed during startup")
        capture_start_ns = time.monotonic_ns()
        measure_start_ns = capture_start_ns + int(args.warmup_seconds * 1e9)
        measure_end_ns = measure_start_ns + int(args.measure_seconds * 1e9)
        while time.monotonic_ns() < measure_end_ns:
            time.sleep(min(0.25, max(0.0, (measure_end_ns - time.monotonic_ns()) / 1e9)))
        probe.send_signal(signal.SIGINT)
        try:
            probe.wait(timeout=10)
        except subprocess.TimeoutExpired:
            probe.kill()
            probe.wait(timeout=5)
        observer_status = observer.wait(timeout=15)
        if probe.returncode not in (0, 130):
            raise RuntimeError("block probe exited with status %d" % probe.returncode)
        if observer_status != 0:
            raise RuntimeError("attribution observer exited with status %d" % observer_status)
    mapping_path.chmod(0o640)

    index = AttributionIndex(read_snapshots(mapping_path))
    with raw_path.open(encoding="utf-8", errors="replace") as handle:
        summary = parse_block_aggregate(
            handle, attribution=index,
            start_ns=measure_start_ns, end_ns=measure_end_ns,
            attribution_max_age_ns=int(args.attribution_max_age_ms * 1e6),
        )
    if summary.collisions or summary.orphans:
        raise RuntimeError(
            "block key quality failure: collisions=%d orphans=%d"
            % (summary.collisions, summary.orphans)
        )
    total_requests = sum(row.requests for row in summary.rows)
    class_requests = sum(
        row.requests for row in summary.rows
        if row.workload_class == args.required_class
    )
    if total_requests <= 0:
        raise RuntimeError("block calibration observed no read/write requests")
    class_fraction = class_requests / total_requests
    if class_fraction < args.minimum_class_request_fraction:
        raise RuntimeError(
            "%s request attribution %.6f is below required %.6f"
            % (args.required_class, class_fraction,
               args.minimum_class_request_fraction)
        )
    result = {
        "schema": "huawei7.block-calibration/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "device": str(args.device),
        "raw_device_number": raw_device_number(args.device),
        "target_database": args.target_database,
        "capture_start_ns": capture_start_ns,
        "measurement_start_ns": measure_start_ns,
        "measurement_end_ns": measure_end_ns,
        "summary": {
            **asdict(summary),
            "rows": [dict(asdict(row), service_time_ms=row.service_time_ms)
                     for row in summary.rows],
        },
        "required_class": args.required_class,
        "class_request_fraction": class_fraction,
        "source_artifacts": [{
            "kind": kind, "path": str(path.resolve()), "sha256": sha256(path),
        } for kind, path in (
            ("block_probe_raw", raw_path),
            ("block_probe_stderr", stderr_path),
            ("attribution_snapshots", mapping_path),
            ("attribution_observer_log", observer_log_path),
        )],
        "valid": True,
    }
    out = args.out_dir / "block_calibration.json"
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
