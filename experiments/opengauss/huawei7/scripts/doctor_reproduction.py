#!/usr/bin/env python3
"""Preflight a genuinely fresh host against the pinned Huawei7 contract."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.machine import collect_machine, validate_ppt_hardware
from huawei7.provenance import check_manifest, sha256
from huawei7.dataset import read_dataset_audit


def version(argv):
    completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, check=True)
    return completed.stdout.strip().splitlines()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=ROOT / "config" / "reproduction_contract.json")
    parser.add_argument("--source-manifest", type=Path, default=ROOT / "config" / "source_manifest.json")
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--gauss-home", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--jdbc-jar", type=Path, required=True)
    parser.add_argument("--benchbase-root", type=Path, required=True)
    parser.add_argument("--benchbase-home", type=Path, required=True)
    parser.add_argument("--dbgen-root", type=Path, required=True)
    parser.add_argument("--data-filesystem-path", type=Path, required=True)
    parser.add_argument("--minimum-free-decimal-gb", type=float, default=160)
    parser.add_argument("--dataset-audit", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    software = contract["software"]
    gaussdb = args.gauss_home / "bin" / "gaussdb"
    source_commit = subprocess.check_output(
        ["git", "-C", str(args.source_root), "rev-parse", "HEAD"], text=True,
    ).strip()
    machine = collect_machine(args.device, gaussdb, source_commit)
    validate_ppt_hardware(machine)
    failures = []
    provenance = check_manifest(args.source_manifest, args.source_root, gaussdb)
    checks = {
        "gaussdb_sha256": sha256(gaussdb),
        "jdbc_sha256": sha256(args.jdbc_jar),
        "benchbase_jar_sha256": sha256(args.benchbase_home / "benchbase.jar"),
        "source_commit": source_commit,
        "benchbase_commit": subprocess.check_output(
            ["git", "-C", str(args.benchbase_root), "rev-parse", "HEAD"], text=True,
        ).strip(),
        "dbgen_commit": subprocess.check_output(
            ["git", "-C", str(args.dbgen_root), "rev-parse", "HEAD"], text=True,
        ).strip(),
        "sysbench_version": version(["/usr/bin/sysbench", "--version"]),
        "fio_version": version(["/usr/bin/fio", "--version"]),
        "bpftrace_version": version(["/usr/bin/bpftrace", "--version"]),
    }
    expected = {
        "gaussdb_sha256": software["gaussdb_sha256"],
        "jdbc_sha256": software["opengauss_jdbc_sha256"],
        "benchbase_jar_sha256": software["benchbase_postgres_jar_sha256"],
        "source_commit": software["opengauss_source_commit"],
        "benchbase_commit": software["benchbase_commit"],
        "dbgen_commit": software["tpch_dbgen_commit"],
        "sysbench_version": "sysbench " + software["sysbench_version"],
        "fio_version": software["fio_version"],
        "bpftrace_version": "bpftrace " + software["bpftrace_version"],
    }
    for key, expected_value in expected.items():
        if checks[key] != expected_value:
            failures.append("%s expected=%s actual=%s" % (
                key, expected_value, checks[key],
            ))
    usage = shutil.disk_usage(args.data_filesystem_path)
    minimum_free = int(args.minimum_free_decimal_gb * 1e9)
    dataset_audit = None
    dataset_audit_artifact = None
    dataset_contract_sha256 = None
    if args.dataset_audit is not None:
        dataset_audit = read_dataset_audit(
            args.dataset_audit,
            machine_fingerprint=str(machine["machine_fingerprint"]),
        )
        dataset_audit_artifact = {
            "path": str(args.dataset_audit.resolve()),
            "sha256": sha256(args.dataset_audit),
        }
        contract_evidence = dataset_audit.get("contract_artifact")
        if not isinstance(contract_evidence, dict):
            raise ValueError("reuse dataset audit lacks contract artifact")
        dataset_contract_sha256 = str(contract_evidence.get("sha256", ""))
    if dataset_audit is None and usage.free < minimum_free:
        failures.append("fresh dataset build needs %.1fGB free, actual %.3fGB" % (
            args.minimum_free_decimal_gb, usage.free / 1e9,
        ))
    result = {
        "schema": "huawei7.fresh-machine-doctor/v1",
        "contract_sha256": sha256(args.contract),
        "source_manifest_sha256": sha256(args.source_manifest),
        "machine": machine, "provenance": provenance,
        "software_checks": checks,
        "dataset_mode": "reuse-audited" if dataset_audit is not None else "fresh-build",
        "dataset_audit_artifact": dataset_audit_artifact,
        "dataset_fingerprint": (
            dataset_audit.get("dataset_fingerprint") if dataset_audit else None
        ),
        "dataset_contract_sha256": dataset_contract_sha256,
        "free_bytes": usage.free,
        "fresh_build_minimum_free_bytes": minimum_free,
        "minimum_free_bytes": 0 if dataset_audit is not None else minimum_free,
        "failures": failures, "valid": not failures,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    if failures:
        raise RuntimeError("fresh-machine doctor failed:\n" + "\n".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
