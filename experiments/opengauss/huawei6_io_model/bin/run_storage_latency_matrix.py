#!/usr/bin/env python3
"""Run an AP/TP-independent direct-I/O latency characterization matrix."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from io_latency_sampler import read_block_stat


@dataclass(frozen=True)
class Profile:
    name: str
    split: str
    mode: str
    block_kib: int
    queue_depth: int
    read_write_ratio: float = 1.5


PROFILES = (
    Profile("mix128_q1", "train", "rndrw", 128, 1),
    Profile("mix128_q4", "train", "rndrw", 128, 4),
    Profile("mix128_q16", "train", "rndrw", 128, 16),
    Profile("mix128_q2", "holdout", "rndrw", 128, 2),
    Profile("mix128_q8", "holdout", "rndrw", 128, 8),
    Profile("read128_q1", "train", "rndrd", 128, 1),
    Profile("read128_q4", "train", "rndrd", 128, 4),
    Profile("read128_q16", "train", "rndrd", 128, 16),
    Profile("read128_q2", "holdout", "rndrd", 128, 2),
    Profile("read128_q8", "holdout", "rndrd", 128, 8),
    Profile("write128_q1", "train", "rndwr", 128, 1),
    Profile("write128_q4", "train", "rndwr", 128, 4),
    Profile("write128_q16", "train", "rndwr", 128, 16),
    Profile("write128_q2", "holdout", "rndwr", 128, 2),
    Profile("write128_q8", "holdout", "rndwr", 128, 8),
    Profile("mix8_q1", "train", "rndrw", 8, 1),
    Profile("mix8_q8", "train", "rndrw", 8, 8),
    Profile("mix8_q32", "train", "rndrw", 8, 32),
    Profile("mix8_q2", "holdout", "rndrw", 8, 2),
    Profile("mix8_q16", "holdout", "rndrw", 8, 16),
)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def command(profile: Profile, file_dir: Path, seconds: int) -> list[str]:
    result = [
        "/usr/bin/sysbench", "fileio",
        "--file-num=8", "--file-total-size=4G",
        f"--file-block-size={profile.block_kib}K",
        f"--file-test-mode={profile.mode}",
        "--file-io-mode=async",
        f"--file-async-backlog={profile.queue_depth}",
        "--file-extra-flags=direct",
        "--file-fsync-freq=0", "--file-fsync-end=off",
        "--threads=1", "--rate=0", f"--time={seconds}",
        "--report-interval=1",
    ]
    if profile.mode == "rndrw":
        result.append(f"--file-rw-ratio={profile.read_write_ratio}")
    result.extend(("run",))
    return result


def interval(previous: dict[str, int], current: dict[str, int], seconds: float) -> dict[str, float] | None:
    reads = current["read_ios"] - previous["read_ios"]
    writes = current["write_ios"] - previous["write_ios"]
    operations = reads + writes
    if operations <= 0 or seconds <= 0:
        return None
    read_ms = current["read_millis"] - previous["read_millis"]
    write_ms = current["write_millis"] - previous["write_millis"]
    sectors = current["read_sectors"] - previous["read_sectors"] + current["write_sectors"] - previous["write_sectors"]
    weighted_ms = current["weighted_io_millis"] - previous["weighted_io_millis"]
    return {
        "read_iops": reads / seconds,
        "write_iops": writes / seconds,
        "total_iops": operations / seconds,
        "throughput_mib_s": sectors * 512.0 / seconds / 1024.0 / 1024.0,
        "device_await_ms": (read_ms + write_ms) / operations,
        "average_outstanding": weighted_ms / seconds / 1000.0,
    }


def run_profile(profile: Profile, file_dir: Path, out_dir: Path, device: str, seconds: int) -> dict[str, object]:
    profile_dir = out_dir / profile.name
    profile_dir.mkdir(parents=True, exist_ok=True)
    log_path = profile_dir / "sysbench_fileio.log"
    raw_rows: list[dict[str, object]] = []
    started = time.monotonic()
    previous_time = started
    previous = read_block_stat(device)
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(command(profile, file_dir, seconds), cwd=file_dir, stdout=log_handle, stderr=subprocess.STDOUT)
        while process.poll() is None:
            time.sleep(0.5)
            now = time.monotonic()
            current = read_block_stat(device)
            measured = interval(previous, current, now - previous_time)
            if measured is not None:
                raw_rows.append({"elapsed_seconds": round(now - started, 6), **measured})
            previous, previous_time = current, now
        if process.wait() != 0:
            raise RuntimeError(f"fileio profile failed: {profile.name}; see {log_path}")
    write_csv(profile_dir / "device_windows.csv", raw_rows)
    stable = [row for row in raw_rows if 2.0 <= float(row["elapsed_seconds"]) <= seconds - 1.0]
    if len(stable) < 8:
        raise RuntimeError(f"insufficient stable windows for {profile.name}: {len(stable)}")
    return {
        "profile": profile.name,
        "split": profile.split,
        "mode": profile.mode,
        "block_kib": profile.block_kib,
        "configured_queue_depth": profile.queue_depth,
        "read_write_ratio": profile.read_write_ratio if profile.mode == "rndrw" else (1.0 if profile.mode == "rndrd" else 0.0),
        "stable_windows": len(stable),
        "actual_read_iops": round(statistics.fmean(float(row["read_iops"]) for row in stable), 6),
        "actual_write_iops": round(statistics.fmean(float(row["write_iops"]) for row in stable), 6),
        "actual_total_iops": round(statistics.fmean(float(row["total_iops"]) for row in stable), 6),
        "actual_throughput_mib_s": round(statistics.fmean(float(row["throughput_mib_s"]) for row in stable), 6),
        "actual_device_await_ms": round(statistics.fmean(float(row["device_await_ms"]) for row in stable), 6),
        "actual_average_outstanding": round(statistics.fmean(float(row["average_outstanding"]) for row in stable), 6),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--device", default="nvme0n1")
    parser.add_argument("--seconds", type=int, default=12)
    args = parser.parse_args()
    if args.seconds < 10:
        parser.error("--seconds must be at least 10")
    if not (args.file_dir / "test_file.0").exists():
        parser.error("prepared sysbench files are missing")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for profile in PROFILES:
        rows.append(run_profile(profile, args.file_dir, args.out_dir, args.device, args.seconds))
        write_csv(args.out_dir / "storage_latency_matrix.csv", rows)
        time.sleep(1.0)
    (args.out_dir / "experiment_manifest.json").write_text(json.dumps({
        "mode": "independent_direct_io_no_database_no_tps",
        "device": args.device,
        "seconds_per_profile": args.seconds,
        "profiles": [profile.__dict__ for profile in PROFILES],
    }, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
