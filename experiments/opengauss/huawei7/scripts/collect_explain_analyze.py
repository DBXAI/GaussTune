#!/usr/bin/env python3
"""Collect one real openGauss EXPLAIN ANALYZE/BUFFERS JSON artifact."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256


def extract_json(text: str) -> object:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        raise ValueError("gsql output contains no EXPLAIN JSON array")
    return json.loads(text[start:end + 1])


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
    parser.add_argument("--application-name", required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.work_mem_mb <= 0 or not args.application_name.startswith("ppt5_ap_"):
        parser.error("positive work_mem and ppt5_ap_* application_name are required")
    password = os.environ.get(args.password_env, "")
    if not password:
        raise RuntimeError("password environment variable is unset")
    query = args.query_file.read_text(encoding="utf-8").strip()
    if not query:
        raise ValueError("query file is empty")
    sql = (
        "SET application_name='" + args.application_name.replace("'", "''") + "';\n"
        "SET enable_vector_engine=off;\n"
        "SET query_dop=1;\n"
        "SET work_mem='" + str(args.work_mem_mb) + "MB';\n"
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query + "\n"
    )
    args.out_dir.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = str(args.library_dir)
    started = time.time()
    completed = subprocess.run([
        str(args.gsql), "-2", "-X", "-At", "-v", "ON_ERROR_STOP=1",
        "-h", args.host, "-p", str(args.port), "-U", args.user,
        "-d", args.database, "-c", sql,
    ], input=password + "\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment, check=False)
    (args.out_dir / "gsql.stderr").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError("EXPLAIN failed with status %d" % completed.returncode)
    document = extract_json(completed.stdout)
    explain_path = args.out_dir / "explain_analyze.json"
    explain_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    metadata = {
        "schema": "huawei7.explain-collection/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "database": args.database, "query_id": args.query_id,
        "work_mem_mb": args.work_mem_mb,
        "application_name": args.application_name,
        "executor": "row; enable_vector_engine=off",
        "query_dop": 1,
        "query_sha256": sha256(args.query_file),
        "explain_sha256": sha256(explain_path),
        "wall_seconds": time.time() - started,
        "valid": True,
    }
    (args.out_dir / "collection.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
