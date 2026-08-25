#!/usr/bin/env python3
"""Adaptively warm TPCC until consecutive TP-only runs converge."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.stability import (
    assess_precondition_convergence, storage_quiescence_from_text,
)
from huawei7.stage_execution import (
    benchbase_command, benchbase_xml, tp_connection,
)


def _redact(path: Path, secret: str) -> None:
    if not secret or not path.is_file():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if secret in text:
        path.write_text(text.replace(secret, "REDACTED"), encoding="utf-8")


def _command_argv(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, list) or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise ValueError("command must be a JSON argv array: %s" % path)
    return list(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--terminals", type=int, required=True)
    parser.add_argument("--run-seconds", type=int, default=30)
    parser.add_argument("--minimum-runs", type=int, default=3)
    parser.add_argument("--maximum-runs", type=int, default=20)
    parser.add_argument("--required-tail-runs", type=int, default=3)
    parser.add_argument("--maximum-relative-range", type=float, default=.10)
    parser.add_argument("--between-run-command-json", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if (
        args.terminals <= 0
        or args.run_seconds < 30
        or args.minimum_runs < 3
        or args.maximum_runs < args.minimum_runs
        or args.required_tail_runs < 3
        or args.minimum_runs < args.required_tail_runs
        or not 0 < args.maximum_relative_range < 1
    ):
        parser.error("invalid TPCC preconditioning contract")

    config = json.loads(args.runtime_config.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("schema") != "huawei7.stage-runtime/v1":
        raise ValueError("unsupported stage runtime config")
    connection = tp_connection(config, "benchbase-tpcc")
    password_name = connection["password_env"]
    password = os.environ.get(password_name, "")
    if not password:
        raise RuntimeError(
            "required password environment variable is unset: %s" % password_name
        )
    args.out_dir.mkdir(parents=True, exist_ok=False)
    samples = []
    throughputs = []
    convergence = assess_precondition_convergence(
        throughputs, required_tail_runs=args.required_tail_runs,
        maximum_relative_range=args.maximum_relative_range,
    )

    for repeat in range(1, args.maximum_runs + 1):
        scratch = Path(tempfile.mkdtemp(
            prefix="huawei7-tpcc-precondition-", dir="/dev/shm",
        ))
        os.chmod(scratch, 0o700)
        xml_path = scratch / "precondition.xml"
        result_dir = scratch / "results"
        log_path = args.out_dir / ("run-%02d.benchbase.log" % repeat)
        retained_summary = args.out_dir / ("run-%02d.summary.json" % repeat)
        try:
            xml_path.write_text(benchbase_xml(
                config, terminals=args.terminals, warmup_seconds=0,
                measure_seconds=args.run_seconds, password=password,
            ), encoding="utf-8")
            os.chmod(xml_path, 0o600)
            environment = dict(os.environ)
            environment["PGAPPNAME"] = "tpcc_adaptive_precondition_r%02d" % repeat
            with log_path.open("w", encoding="utf-8") as handle:
                completed = subprocess.run(
                    benchbase_command(
                        config, xml_path=xml_path, result_dir=result_dir,
                    ),
                    stdout=handle, stderr=subprocess.STDOUT, text=True,
                    env=environment,
                )
            _redact(log_path, password)
            if completed.returncode != 0:
                raise RuntimeError(
                    "TPCC precondition run %d failed with status %d"
                    % (repeat, completed.returncode)
                )
            matches = sorted(result_dir.rglob("*.summary.json"))
            if len(matches) != 1:
                raise RuntimeError(
                    "expected one TPCC precondition summary, found %d"
                    % len(matches)
                )
            summary = json.loads(matches[0].read_text(encoding="utf-8"))
            throughput = float(summary["Throughput (requests/second)"])
            measured_requests = int(summary["Measured Requests"])
            if throughput <= 0 or measured_requests <= 0:
                raise RuntimeError("TPCC precondition produced no measured requests")
            shutil.copy2(matches[0], retained_summary)
            checkpoint_artifact = None
            storage_quiescence = None
            if args.between_run_command_json is not None:
                checkpoint_log = args.out_dir / (
                    "run-%02d.checkpoint.log" % repeat
                )
                with checkpoint_log.open("w", encoding="utf-8") as handle:
                    subprocess.run(
                        _command_argv(args.between_run_command_json), check=True,
                        stdout=handle, stderr=subprocess.STDOUT, text=True,
                    )
                storage_quiescence = storage_quiescence_from_text(
                    checkpoint_log.read_text(
                        encoding="utf-8", errors="replace",
                    )
                )
                checkpoint_artifact = {
                    "path": str(checkpoint_log.resolve()),
                    "sha256": sha256(checkpoint_log),
                }
            throughputs.append(throughput)
            sample = {
                "run": repeat,
                "throughput_tps": throughput,
                "measured_requests": measured_requests,
                "driver_log": {
                    "path": str(log_path.resolve()),
                    "sha256": sha256(log_path),
                },
                "summary": {
                    "path": str(retained_summary.resolve()),
                    "sha256": sha256(retained_summary),
                },
            }
            if checkpoint_artifact is not None:
                sample.update({
                    "checkpoint_log": checkpoint_artifact,
                    "storage_quiescence": storage_quiescence,
                })
            samples.append(sample)
            convergence = assess_precondition_convergence(
                throughputs, required_tail_runs=args.required_tail_runs,
                maximum_relative_range=args.maximum_relative_range,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if repeat >= args.minimum_runs and convergence["converged"] is True:
            break

    converged = bool(
        len(samples) >= args.minimum_runs
        and convergence.get("converged") is True
    )
    report = {
        "schema": "huawei7.tp-adaptive-precondition/v1",
        "benchmark": "benchbase-tpcc",
        "connection_transport": "password-authenticated-dedicated-role",
        "database": connection["database"],
        "terminals": args.terminals,
        "run_seconds": args.run_seconds,
        "minimum_runs": args.minimum_runs,
        "maximum_runs": args.maximum_runs,
        "runtime_config": {
            "path": str(args.runtime_config.resolve()),
            "sha256": sha256(args.runtime_config),
        },
        "between_run_postcondition": (
            {
                "checkpoint_command": {
                    "path": str(args.between_run_command_json.resolve()),
                    "sha256": sha256(args.between_run_command_json),
                },
                "contract": "CHECKPOINT plus dirty-memory/device-I/O quiescence",
            }
            if args.between_run_command_json is not None else None
        ),
        "samples": samples,
        "convergence": convergence,
        "converged": converged,
        "valid": converged,
    }
    report_path = args.out_dir / "precondition_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if converged else 1


if __name__ == "__main__":
    raise SystemExit(main())
