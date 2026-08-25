#!/usr/bin/env python3
"""Checkpoint openGauss and fail closed until dirty memory and device I/O settle."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Tuple


def _memory_writeback_bytes() -> int:
    values = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] in ("Dirty:", "Writeback:"):
            values[fields[0][:-1]] = int(fields[1]) * 1024
    if set(values) != {"Dirty", "Writeback"}:
        raise RuntimeError("cannot read Dirty/Writeback from /proc/meminfo")
    return values["Dirty"] + values["Writeback"]


def _device_stat(path: Path) -> Tuple[int, int]:
    fields = path.read_text(encoding="utf-8").split()
    if len(fields) < 11:
        raise RuntimeError("unsupported block device stat format: %s" % path)
    sectors = int(fields[2]) + int(fields[6])
    in_flight = int(fields[8])
    return sectors, in_flight


def _gsql_scalar(gsql: Path, database: str, statement: str) -> str:
    return subprocess.check_output(
        [str(gsql), "-X", "-At", "-d", database, "-c", statement],
        text=True,
    ).strip()


def _reload_guc(gauss_home: Path, data_dir: Path, assignment: str) -> None:
    completed = subprocess.run(
        [
            str(gauss_home / "bin" / "gs_guc"), "reload",
            "-D", str(data_dir), "-c", assignment,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if completed.stdout:
        print(completed.stdout.rstrip(), file=sys.stderr)
    if completed.returncode != 0:
        raise RuntimeError("gs_guc reload failed for %s" % assignment)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gsql", type=Path, required=True)
    parser.add_argument("--gauss-home", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--database", default="postgres")
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--sample-seconds", type=float, default=2.0)
    parser.add_argument("--required-consecutive-samples", type=int, default=3)
    parser.add_argument("--maximum-dirty-mb", type=float, default=64.0)
    parser.add_argument(
        "--maximum-device-mb-per-second", type=float, default=16.0,
    )
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--temporary-checkpoint-completion-target", type=float)
    args = parser.parse_args()
    if (
        args.sample_seconds < 1
        or args.required_consecutive_samples < 3
        or args.maximum_dirty_mb <= 0
        or args.maximum_device_mb_per_second < 0
        or args.timeout_seconds < args.sample_seconds * args.required_consecutive_samples
    ):
        parser.error("invalid checkpoint/storage quiescence contract")
    temporary_target = args.temporary_checkpoint_completion_target
    if temporary_target is not None and (
        not 0 <= temporary_target <= 1
        or args.gauss_home is None
        or args.data_dir is None
    ):
        parser.error(
            "temporary checkpoint target requires gauss-home/data-dir and [0,1]"
        )
    device = args.device.resolve()
    if not str(device).startswith("/dev/") or not device.exists():
        raise ValueError("device must be an existing /dev block path")
    stat_path = Path("/sys/class/block") / device.name / "stat"
    if not stat_path.is_file():
        raise FileNotFoundError("block device stat is missing: %s" % stat_path)

    original_target = _gsql_scalar(
        args.gsql, args.database, "SHOW checkpoint_completion_target;",
    )
    target_applied = False
    restored = True
    checkpoint = None
    try:
        if temporary_target is not None:
            assert args.gauss_home is not None and args.data_dir is not None
            target_text = "%g" % temporary_target
            _reload_guc(
                args.gauss_home, args.data_dir,
                "checkpoint_completion_target=%s" % target_text,
            )
            target_applied = (
                _gsql_scalar(
                    args.gsql, args.database,
                    "SHOW checkpoint_completion_target;",
                ) == target_text
            )
            if not target_applied:
                raise RuntimeError("temporary checkpoint target did not reload")
        checkpoint_started = time.monotonic()
        checkpoint = subprocess.run(
            [
                str(args.gsql), "-X", "-v", "ON_ERROR_STOP=1",
                "-d", args.database, "-c", "CHECKPOINT;",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        checkpoint_seconds = time.monotonic() - checkpoint_started
        if checkpoint.stdout:
            print(checkpoint.stdout.rstrip(), file=sys.stderr)
    finally:
        if temporary_target is not None and target_applied:
            assert args.gauss_home is not None and args.data_dir is not None
            _reload_guc(
                args.gauss_home, args.data_dir,
                "checkpoint_completion_target=%s" % original_target,
            )
            restored = (
                _gsql_scalar(
                    args.gsql, args.database,
                    "SHOW checkpoint_completion_target;",
                ) == original_target
            )
    if checkpoint is None or checkpoint.returncode != 0:
        status = checkpoint.returncode if checkpoint is not None else -1
        raise RuntimeError("CHECKPOINT failed with status %d" % status)
    if not restored:
        raise RuntimeError("checkpoint completion target was not restored")

    maximum_dirty_bytes = int(args.maximum_dirty_mb * 1024 * 1024)
    maximum_device_bytes_per_second = (
        args.maximum_device_mb_per_second * 1024 * 1024
    )
    deadline = time.monotonic() + args.timeout_seconds
    previous_sectors, _ = _device_stat(stat_path)
    previous_time = time.monotonic()
    consecutive = 0
    samples = []
    while time.monotonic() < deadline:
        time.sleep(args.sample_seconds)
        now = time.monotonic()
        sectors, in_flight = _device_stat(stat_path)
        elapsed = now - previous_time
        device_bytes_per_second = (
            max(0, sectors - previous_sectors) * 512 / elapsed
        )
        dirty_bytes = _memory_writeback_bytes()
        accepted = bool(
            dirty_bytes <= maximum_dirty_bytes
            and device_bytes_per_second <= maximum_device_bytes_per_second
            and in_flight == 0
        )
        consecutive = consecutive + 1 if accepted else 0
        samples.append({
            "sample": len(samples) + 1,
            "elapsed_seconds": now - checkpoint_started,
            "dirty_and_writeback_bytes": dirty_bytes,
            "device_bytes_per_second": device_bytes_per_second,
            "device_in_flight": in_flight,
            "accepted": accepted,
            "consecutive_accepted": consecutive,
        })
        previous_sectors = sectors
        previous_time = now
        if consecutive >= args.required_consecutive_samples:
            break

    valid = consecutive >= args.required_consecutive_samples
    report = {
        "schema": "huawei7.storage-quiescence/v1",
        "device": str(device),
        "checkpoint_completed": True,
        "checkpoint_seconds": checkpoint_seconds,
        "checkpoint_completion_target_original": original_target,
        "checkpoint_completion_target_temporary": temporary_target,
        "checkpoint_completion_target_restored": restored,
        "sample_seconds": args.sample_seconds,
        "required_consecutive_samples": args.required_consecutive_samples,
        "accepted_consecutive_samples": consecutive,
        "maximum_dirty_bytes": maximum_dirty_bytes,
        "maximum_device_bytes_per_second": maximum_device_bytes_per_second,
        "samples": samples,
        "valid": valid,
    }
    print(json.dumps(report, sort_keys=True))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
