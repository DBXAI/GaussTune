#!/usr/bin/env python3
"""Measure TP/AP read/write service times with four direct-I/O fio classes."""

import argparse
import json
import random
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.fio_surface import _latency_ms, validate_target
from huawei7.provenance import sha256


CLASSES = {
    "tp_read_ms": ("randread", 8),
    "tp_write_ms": ("randwrite", 8),
    "ap_read_ms": ("randread", 128),
    "ap_write_ms": ("randwrite", 128),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--runtime-seconds", type=int, default=10)
    parser.add_argument("--ramp-seconds", type=int, default=2)
    parser.add_argument("--seed", type=int, default=59243)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 3 or args.runtime_seconds < 5 or args.ramp_seconds < 0:
        parser.error("require >=3 repeats, runtime>=5s, and nonnegative ramp")
    target_info = validate_target(args.target)
    fio = shutil.which("fio")
    if not fio:
        raise RuntimeError("fio is not installed")
    args.out_dir.mkdir(parents=True, exist_ok=False)
    jobs = [(name, repeat) for name in CLASSES
            for repeat in range(1, args.repeats + 1)]
    random.Random(args.seed).shuffle(jobs)
    samples = {name: [] for name in CLASSES}
    evidence = {name: [] for name in CLASSES}
    raw_artifacts = []
    for name, repeat in jobs:
        rw, block_kib = CLASSES[name]
        completed = subprocess.run([
            fio, "--name=huawei7-%s-r%d" % (name, repeat),
            "--filename=%s" % args.target, "--rw=%s" % rw,
            "--bs=%dk" % block_kib, "--direct=1", "--ioengine=libaio",
            "--iodepth=1", "--time_based=1",
            "--runtime=%d" % args.runtime_seconds,
            "--ramp_time=%d" % args.ramp_seconds,
            "--randrepeat=1", "--randseed=%d" % (args.seed + repeat),
            "--output-format=json", "--eta=never",
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError("fio service job failed: %s" % completed.stderr.strip())
        raw_path = args.out_dir / ("%s-repeat-%02d.json" % (name, repeat))
        raw_path.write_text(completed.stdout, encoding="utf-8")
        document = json.loads(completed.stdout)
        job = document["jobs"][0]
        direction = "read" if rw == "randread" else "write"
        directional = job[direction]
        if int(directional["total_ios"]) <= 0:
            raise RuntimeError("fio service job completed no I/O")
        # _latency_ms reads the read object, so provide a tiny shape for write.
        latency = _latency_ms(job if direction == "read" else {"read": directional})
        samples[name].append(latency)
        evidence[name].append(sha256(raw_path))
        raw_artifacts.append({
            "kind": "fio_raw", "service_class": name, "repeat": repeat,
            "path": str(raw_path.resolve()), "sha256": sha256(raw_path),
        })
        print(json.dumps({"class": name, "repeat": repeat,
                          "service_time_ms": latency}, sort_keys=True), flush=True)
    values = {name: statistics.median(rows) for name, rows in samples.items()}
    result = {
        "schema": "huawei7.service-times/v2",
        "machine_fingerprint": args.machine_fingerprint,
        "method": "fio direct=1 iodepth=1 class-specific block size",
        "target": target_info, "service_times_ms": values,
        "evidence": {
            name: {"repeats": len(samples[name]), "samples_ms": samples[name],
                   "raw_sha256": evidence[name]}
            for name in CLASSES
        },
        "source_artifacts": raw_artifacts,
        "valid": True,
    }
    path = args.out_dir / "service_times.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
