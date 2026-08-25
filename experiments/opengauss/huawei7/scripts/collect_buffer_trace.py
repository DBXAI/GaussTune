#!/usr/bin/env python3
"""Collect, attribute, normalize and quality-gate one bounded TP cache trace.

Run as root for uprobes.  The observer connection is executed as the ``omm``
OS user and must use a control database different from the traced TP database.
Benchmark sessions must already be connected with an explicit application_name
matching ``sysbench_tp_*`` or ``tpcc_*`` before this collector starts.
"""

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
from huawei7.cache_replay import validate_observed_hits
from huawei7.schema import PAGE_SIZE
from huawei7.schema import write_trace
from huawei7.trace import inspect_binary_probe, normalize_path
from huawei7.trace_quality import trace_quality


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--target-db-node", required=True, type=int)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--control-dsn", required=True)
    parser.add_argument("--warmup-seconds", type=float, default=30.0)
    parser.add_argument("--measure-seconds", type=float, default=60.0)
    parser.add_argument("--snapshot-interval-ms", type=float, default=100.0)
    parser.add_argument("--attribution-max-age-ms", type=float, default=300.0)
    parser.add_argument("--minimum-tp-access-fraction", type=float, default=0.90)
    parser.add_argument("--actual-shared-buffers-mb", type=float, required=True)
    parser.add_argument("--maximum-hit-mismatch-fraction", type=float, default=0.01)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("buffer uprobes require root")
    if args.warmup_seconds < 0 or args.measure_seconds <= 0:
        parser.error("warmup must be nonnegative and measure must be positive")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = args.out_dir / "buffer_trace.raw"
    trace_path = args.out_dir / "buffer_trace.csv"
    mapping_path = args.out_dir / "lwtid_attribution.csv"
    probe_stderr_path = args.out_dir / "buffer_trace.stderr"
    observer_log_path = args.out_dir / "attribution_observer.log"
    # The observer runs as omm.  Pre-create only its exact output target and
    # grant write permission; the experiment directory itself remains root-owned.
    mapping_path.touch(exist_ok=False)
    omm = pwd.getpwnam("omm")
    os.chown(mapping_path, 0, omm.pw_gid)
    mapping_path.chmod(0o660)

    total = args.warmup_seconds + args.measure_seconds
    snapshot_script = ROOT / "scripts" / "snapshot_sessions.py"
    probe_script = ROOT / "probes" / "opengauss_buffer_trace_bcc.py"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    with raw_path.open("wb") as raw_handle, \
            probe_stderr_path.open("w", encoding="utf-8") as probe_error, \
            observer_log_path.open("w", encoding="utf-8") as observer_log:
        probe = subprocess.Popen(
            [sys.executable, str(probe_script), str(args.target_db_node)],
            stdout=raw_handle, stderr=probe_error,
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
        # Attachment and the first complete attribution snapshot are excluded.
        time.sleep(1.0)
        if probe.poll() is not None:
            raise RuntimeError("buffer probe failed during attachment")
        if observer.poll() is not None:
            raise RuntimeError("attribution observer failed during startup")
        capture_start_ns = time.monotonic_ns()
        warmup_end_ns = capture_start_ns + int(args.warmup_seconds * 1e9)
        measure_end_ns = warmup_end_ns + int(args.measure_seconds * 1e9)
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
            raise RuntimeError("buffer probe exited with status %d" % probe.returncode)
        if observer_status != 0:
            raise RuntimeError("attribution observer exited with status %d" % observer_status)
    mapping_path.chmod(0o640)

    attribution = AttributionIndex(read_snapshots(mapping_path))
    probe_summary = inspect_binary_probe(raw_path)
    events = normalize_path(
        raw_path, warmup_end_ns=warmup_end_ns, measure_end_ns=measure_end_ns,
        attribution=attribution,
        attribution_max_age_ns=int(args.attribution_max_age_ms * 1e6),
    )
    write_trace(trace_path, events)
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
    metadata = {
        "schema": "huawei7.buffer-collection/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "target_database": args.target_database,
        "target_db_node": args.target_db_node,
        "control_dsn": args.control_dsn,
        "capture_start_ns": capture_start_ns,
        "warmup_end_ns": warmup_end_ns,
        "measure_end_ns": measure_end_ns,
        "snapshot_interval_ms": args.snapshot_interval_ms,
        "attribution_max_age_ms": args.attribution_max_age_ms,
        "quality": quality,
        "cache_validation": asdict(cache_validation),
        "buffer_probe_summary": probe_summary,
    }
    meta_path = args.out_dir / "collection.json"
    meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
