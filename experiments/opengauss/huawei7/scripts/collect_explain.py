#!/usr/bin/env python3
"""Collect a blind openGauss JSON plan without executing the AP query."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from scripts.collect_explain_analyze import extract_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gsql", type=Path, required=True)
    parser.add_argument("--library-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--query-file", type=Path, required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--work-mem-mb", type=int, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    password = os.environ.get(args.password_env, "")
    if not password or args.work_mem_mb <= 0:
        raise ValueError("password variable and positive work_mem are required")
    query = args.query_file.read_text(encoding="utf-8").strip()
    if args.out.exists():
        raise FileExistsError("refusing to overwrite blind EXPLAIN: %s" % args.out)
    metadata_path = args.out.with_name(args.out.name + ".collection.json")
    if metadata_path.exists():
        raise FileExistsError("refusing to overwrite blind metadata: %s" % metadata_path)
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = str(args.library_dir)
    sql = (
        "SET application_name='ppt5_ap_blind_explain';\n"
        "SET enable_vector_engine=off;\n"
        "SET query_dop=1;\n"
        "SET work_mem='%dMB';\nEXPLAIN (FORMAT JSON) %s\n"
        % (args.work_mem_mb, query)
    )
    completed = subprocess.run([
        str(args.gsql), "-2", "-X", "-At", "-v", "ON_ERROR_STOP=1",
        "-h", args.host, "-p", str(args.port), "-U", args.user,
        "-d", args.database, "-c", sql,
    ], input=password + "\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment, check=False)
    if completed.returncode != 0:
        raise RuntimeError("blind EXPLAIN failed: %s" % completed.stderr.strip())
    document = extract_json(completed.stdout)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    metadata = {
        "schema": "huawei7.blind-explain-collection/v1",
        "query_id": args.query_id, "work_mem_mb": args.work_mem_mb,
        "machine_fingerprint": args.machine_fingerprint,
        "query_sha256": sha256(args.query_file),
        "explain_sha256": sha256(args.out), "blind": True,
        "executor": "row; enable_vector_engine=off",
        "query_dop": 1,
        "valid": True,
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
