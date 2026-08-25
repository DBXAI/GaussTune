#!/usr/bin/env python3
"""Measure strict buffer-probe overhead for the four PPT TP topologies."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256


ARMS = (
    ("sysbench", "n128", 128, 0, 28214),
    ("sysbench", "n144", 144, 16, 28214),
    ("benchbase-tpcc", "n128", 128, 0, 28478),
    ("benchbase-tpcc", "n144", 144, 16, 28478),
)


def _read(path: Path) -> Dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact root must be an object")
    return value


def _run(command, log: Path) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as handle:
        result = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError("command failed (%d), see %s" % (result.returncode, log))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--sysbench-password-env", default="H7_STRICT_SB_PASSWORD")
    parser.add_argument("--tpcc-password-env", default="H7_STRICT_TPCC_PASSWORD")
    parser.add_argument("--warmup-seconds", type=int, default=10)
    parser.add_argument("--measure-seconds", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    wanted = set(args.only)
    result: Dict[str, object] = {
        "schema": "huawei7.strict-ppt-overhead-run/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "warmup_seconds": args.warmup_seconds,
        "measure_seconds": args.measure_seconds,
        "repeats": args.repeats,
        "dataset_protocol": {
            "tpcc_reset_performed": False,
            "tpcc_reload_performed": False,
        },
        "artifacts": {},
        "valid": False,
    }
    for benchmark, topology, terminals, surge, db_node in ARMS:
        arm = "%s-%s" % (benchmark, topology)
        if wanted and arm not in wanted:
            continue
        command_path = (
            args.matrix_dir / benchmark / topology / "command" / "tp-command.json"
        )
        if not command_path.is_file():
            raise FileNotFoundError(command_path)
        output = args.out_dir / benchmark / topology
        artifact = output / "probe_overhead.json"
        if artifact.is_file():
            document = _read(artifact)
            if (
                document.get("schema") == "huawei7.buffer-probe-overhead/v2"
                and document.get("valid") is True
                and document.get("machine_fingerprint") == args.machine_fingerprint
            ):
                result["artifacts"][arm] = {
                    "path": str(artifact.resolve()), "sha256": sha256(artifact),
                }
                continue
        if output.exists():
            # Do not overwrite a failed or changed measurement arm.
            attempt = output.with_name(output.name + ".attempt")
            counter = 1
            while attempt.exists():
                counter += 1
                attempt = output.with_name(
                    output.name + ".attempt-%02d" % counter
                )
            output.rename(attempt)
        password_env = (
            args.sysbench_password_env
            if benchmark == "sysbench" else args.tpcc_password_env
        )
        command = [
            sys.executable, str(ROOT / "scripts" / "measure_buffer_probe_overhead.py"),
            "--command-json", str(command_path),
            "--target-db-node", str(db_node),
            "--device", "/dev/nvme0n1",
            "--benchmark", benchmark,
            "--machine-fingerprint", args.machine_fingerprint,
            "--password-env", password_env,
            "--warmup-seconds", str(args.warmup_seconds),
            "--measure-seconds", str(args.measure_seconds),
            "--repeats", str(args.repeats),
            "--maximum-slowdown-fraction", ".05",
            "--out-dir", str(output),
        ]
        _run(command, output.with_suffix(".measure.console.log"))
        if not artifact.is_file():
            raise RuntimeError("overhead tool produced no artifact: %s" % artifact)
        result["artifacts"][arm] = {
            "path": str(artifact.resolve()), "sha256": sha256(artifact),
        }
    result["valid"] = len(result["artifacts"]) == (
        len(wanted) if wanted else len(ARMS)
    )
    (args.out_dir / "overhead-run.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
