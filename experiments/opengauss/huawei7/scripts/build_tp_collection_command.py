#!/usr/bin/env python3
"""Build a secret-safe argv file for synchronized TP calibration."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.dataset import dataset_audit_from_runtime, tp_dataset_identity
from huawei7.stage_execution import (
    benchbase_command, benchbase_xml, sysbench_command, tp_connection,
)
from huawei7.transaction_evidence import BENCHMARKS, tp_command_contract_id
from huawei7.provenance import sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--benchmark", choices=BENCHMARKS, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--terminals", type=int, required=True)
    parser.add_argument("--surge-terminals", type=int, default=0)
    parser.add_argument("--warmup-seconds", type=int, required=True)
    parser.add_argument("--measure-seconds", type=int, required=True)
    parser.add_argument("--out-command", type=Path, required=True)
    parser.add_argument("--benchbase-xml", type=Path)
    parser.add_argument("--benchbase-result-dir", type=Path)
    parser.add_argument("--surge-benchbase-xml", type=Path)
    parser.add_argument("--surge-benchbase-result-dir", type=Path)
    args = parser.parse_args()
    if (
        args.terminals <= 0 or args.surge_terminals < 0
        or args.surge_terminals >= args.terminals
        or args.warmup_seconds < 1 or args.measure_seconds < 3
    ):
        parser.error("positive terminals, warmup>=1 and measure>=3 are required")
    if args.terminals == 128 and args.surge_terminals != 0:
        parser.error("PPT N=128 calibration has no surge driver")
    if args.terminals == 144 and args.surge_terminals != 16:
        parser.error("PPT S5 N=144 requires a 128+16 measurement-phase surge")
    if args.out_command.exists():
        raise FileExistsError("refusing to overwrite TP command artifact")
    config = json.loads(args.runtime_config.read_text(encoding="utf-8"))
    if (
        config.get("schema") != "huawei7.stage-runtime/v1"
        or config.get("machine_fingerprint") != args.machine_fingerprint
    ):
        raise ValueError("runtime config is invalid or belongs to another machine")
    dataset_audit, dataset_audit_path = dataset_audit_from_runtime(
        config, machine_fingerprint=args.machine_fingerprint,
    )
    total = args.warmup_seconds + args.measure_seconds
    baseline_terminals = args.terminals - args.surge_terminals
    drivers = []
    if args.benchmark == "sysbench":
        script_path = Path(str(config["tp"]["sysbench"]["script"]))
        if not script_path.is_file():
            raise FileNotFoundError("Sysbench workload script is missing")
        drivers.append({
            "role": "baseline", "terminals": baseline_terminals,
            "start_phase": "warmup",
            "argv": list(sysbench_command(
                config, terminals=baseline_terminals, total_seconds=total,
            )),
            "benchbase_xml": None,
        })
        if args.surge_terminals:
            drivers.append({
                "role": "surge", "terminals": args.surge_terminals,
                "start_phase": "measurement",
                "argv": list(sysbench_command(
                    config, terminals=args.surge_terminals,
                    total_seconds=args.measure_seconds,
                )),
                "benchbase_xml": None,
            })
        tp = config["tp"]["sysbench"]
        connection = tp_connection(config, "sysbench")
        dataset = tp_dataset_identity(
            dataset_audit, dataset_audit_path, benchmark="sysbench",
            database=connection["database"],
            configured_tables=int(tp["tables"]),
            configured_rows=int(tp["table_size"]),
        )
        workload_contract = {
            "schema": "huawei7.tp-workload-contract/v1",
            "mode": "read_only",
            "issued_io_directions": ["R"],
            "zero_io_directions": ["W"],
            "script_artifact": {
                "path": str(script_path.resolve()),
                "sha256": sha256(script_path),
            },
        }
    else:
        if args.benchbase_xml is None or args.benchbase_result_dir is None:
            parser.error("BenchBase requires --benchbase-xml and --benchbase-result-dir")
        if args.surge_terminals and (
            args.surge_benchbase_xml is None
            or args.surge_benchbase_result_dir is None
        ):
            parser.error(
                "BenchBase surge requires --surge-benchbase-xml and "
                "--surge-benchbase-result-dir"
            )
        if args.benchbase_xml.exists():
            raise FileExistsError("refusing to overwrite BenchBase XML")
        if args.surge_benchbase_xml is not None and args.surge_benchbase_xml.exists():
            raise FileExistsError("refusing to overwrite surge BenchBase XML")
        connection = tp_connection(config, "benchbase-tpcc")
        tp = config["tp"]["benchbase-tpcc"]
        if not isinstance(tp, dict):
            raise ValueError("invalid BenchBase runtime config")
        password_name = connection["password_env"]
        if password_name not in os.environ:
            raise RuntimeError("required TP password variable is unset: %s" % password_name)
        args.benchbase_xml.parent.mkdir(parents=True, exist_ok=True)
        args.benchbase_xml.write_text(benchbase_xml(
            config, terminals=baseline_terminals,
            warmup_seconds=args.warmup_seconds,
            measure_seconds=args.measure_seconds,
            password=os.environ[password_name],
        ), encoding="utf-8")
        args.benchbase_xml.chmod(0o600)
        drivers.append({
            "role": "baseline", "terminals": baseline_terminals,
            "start_phase": "warmup",
            "argv": list(benchbase_command(
                config, xml_path=args.benchbase_xml.resolve(),
                result_dir=args.benchbase_result_dir.resolve(),
            )),
            "benchbase_xml": {
                "path": str(args.benchbase_xml.resolve()),
                "sha256": sha256(args.benchbase_xml),
                "result_dir": str(args.benchbase_result_dir.resolve()),
            },
            "benchbase_parameters": {
                "schema": "huawei7.benchbase-driver-contract/v1",
                "terminals": baseline_terminals,
                "scale_factor": int(tp["warehouses"]),
                "batch_size": int(tp.get("batch_size", 128)),
                "warmup_seconds": args.warmup_seconds,
                "measure_seconds": args.measure_seconds,
                "transaction_weights": [45, 43, 4, 4, 4],
            },
        })
        if args.surge_terminals:
            assert args.surge_benchbase_xml is not None
            assert args.surge_benchbase_result_dir is not None
            args.surge_benchbase_xml.parent.mkdir(parents=True, exist_ok=True)
            args.surge_benchbase_xml.write_text(benchbase_xml(
                config, terminals=args.surge_terminals,
                warmup_seconds=0, measure_seconds=args.measure_seconds,
                password=os.environ[password_name],
            ), encoding="utf-8")
            args.surge_benchbase_xml.chmod(0o600)
            drivers.append({
                "role": "surge", "terminals": args.surge_terminals,
                "start_phase": "measurement",
                "argv": list(benchbase_command(
                    config, xml_path=args.surge_benchbase_xml.resolve(),
                    result_dir=args.surge_benchbase_result_dir.resolve(),
                )),
                "benchbase_xml": {
                    "path": str(args.surge_benchbase_xml.resolve()),
                    "sha256": sha256(args.surge_benchbase_xml),
                    "result_dir": str(args.surge_benchbase_result_dir.resolve()),
                },
                "benchbase_parameters": {
                    "schema": "huawei7.benchbase-driver-contract/v1",
                    "terminals": args.surge_terminals,
                    "scale_factor": int(tp["warehouses"]),
                    "batch_size": int(tp.get("batch_size", 128)),
                    "warmup_seconds": 0,
                    "measure_seconds": args.measure_seconds,
                    "transaction_weights": [45, 43, 4, 4, 4],
                },
            })
        dataset = tp_dataset_identity(
            dataset_audit, dataset_audit_path, benchmark="benchbase-tpcc",
            database=connection["database"],
            configured_warehouses=int(tp["warehouses"]),
        )
        dataset["batch_size"] = int(tp.get("batch_size", 128))
        workload_contract = {
            "schema": "huawei7.tp-workload-contract/v1",
            "mode": "read_write",
            "issued_io_directions": ["R", "W"],
            "zero_io_directions": [],
            "transaction_weights": [45, 43, 4, 4, 4],
        }
    args.out_command.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "schema": "huawei7.tp-command/v2",
        "machine_fingerprint": args.machine_fingerprint,
        "benchmark": args.benchmark, "terminals": args.terminals,
        "baseline_terminals": baseline_terminals,
        "surge_terminals": args.surge_terminals,
        "warmup_seconds": args.warmup_seconds,
        "measure_seconds": args.measure_seconds,
        "password_env": connection["password_env"],
        "runtime_config_sha256": sha256(args.runtime_config),
        "dataset": dataset, "drivers": drivers,
        "workload_contract": workload_contract,
    }
    result["command_contract_id"] = tp_command_contract_id(result)
    args.out_command.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.out_command.chmod(0o600)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
