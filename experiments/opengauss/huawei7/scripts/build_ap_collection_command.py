#!/usr/bin/env python3
"""Build a secret-free, query/WM-bound argv artifact for isolated AP I/O."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.dataset import ap_dataset_identity, dataset_audit_from_runtime
from huawei7.stage_execution import ap_gsql_command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--query-file", type=Path, required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--work-mem-mb", type=int, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--application-name", required=True)
    parser.add_argument(
        "--explain-analyze", action="store_true",
        help="also collect ANALYZE/BUFFERS runtime labels in the paired run",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.work_mem_mb <= 0 or not args.application_name.startswith("ppt5_ap_"):
        parser.error("positive work_mem and ppt5_ap_* application name are required")
    config = json.loads(args.runtime_config.read_text(encoding="utf-8"))
    if (
        config.get("schema") != "huawei7.stage-runtime/v1"
        or config.get("machine_fingerprint") != args.machine_fingerprint
    ):
        raise ValueError("runtime config is invalid or belongs to another machine")
    dataset_audit, dataset_audit_path = dataset_audit_from_runtime(
        config, machine_fingerprint=args.machine_fingerprint,
    )
    postgres = config.get("postgres")
    if not isinstance(postgres, dict):
        raise ValueError("runtime postgres config is invalid")
    command = ap_gsql_command(
        config, query_file=args.query_file, work_mem_mb=args.work_mem_mb,
        application_name=args.application_name,
        explain_analyze=args.explain_analyze,
    )
    result = {
        "schema": (
            "huawei7.ap-command/v3" if args.explain_analyze
            else "huawei7.ap-command/v2"
        ),
        "machine_fingerprint": args.machine_fingerprint,
        "query_id": str(args.query_id),
        "query_sha256": sha256(args.query_file),
        "work_mem_mb": args.work_mem_mb,
        "executor": "row; enable_vector_engine=off",
        "query_dop": 1,
        "measurement": (
            "explain_analyze_buffers" if args.explain_analyze else "query"
        ),
        "application_name": args.application_name,
        "database": str(postgres.get("ap_database", "")),
        "runtime_config_sha256": sha256(args.runtime_config),
        "dataset": ap_dataset_identity(
            dataset_audit, dataset_audit_path,
            database=str(postgres.get("ap_database", "")),
        ),
        "argv": list(command),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        raise FileExistsError("refusing to overwrite AP command: %s" % args.out)
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
