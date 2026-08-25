#!/usr/bin/env python3
"""Collect openGauss row-executor A-width evidence for one AP plan."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.operator_model import parse_explain
from huawei7.operator_width_evidence import (
    executor_width_anchors, required_width_nodes,
)
from huawei7.provenance import sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--explain-analyze", type=Path, required=True)
    parser.add_argument("--query-file", type=Path, required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--work-mem-mb", type=int, required=True)
    parser.add_argument("--gsql", type=Path, required=True)
    parser.add_argument("--library-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.work_mem_mb <= 0:
        parser.error("work_mem must be positive")
    password = os.environ.get(args.password_env, "")
    if not password:
        raise RuntimeError("password environment variable is unset")
    query = args.query_file.read_text(encoding="utf-8").strip()
    explain_document = json.loads(args.explain_analyze.read_text(encoding="utf-8"))
    sql = (
        "SET application_name='ppt5_ap_width_" +
        str(args.query_id).replace("'", "") + "';\n"
        "SET enable_vector_engine=off;\n"
        "SET query_dop=1;\n"
        "SET explain_perf_mode=pretty;\n"
        "SET work_mem='" + str(args.work_mem_mb) + "MB';\n"
        "EXPLAIN PERFORMANCE " + query + "\n"
    )
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = str(args.library_dir)
    completed = subprocess.run([
        str(args.gsql), "-2", "-X", "-v", "ON_ERROR_STOP=1",
        "-h", args.host, "-p", str(args.port), "-U", args.user,
        "-d", args.database, "-c", sql,
    ], input=password + "\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment, check=False)
    args.out_dir.mkdir(parents=True, exist_ok=False)
    raw = args.out_dir / "explain_performance.txt"
    raw.write_text(completed.stdout, encoding="utf-8")
    (args.out_dir / "gsql.stderr").write_text(
        completed.stderr, encoding="utf-8"
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "EXPLAIN PERFORMANCE failed with status %d" % completed.returncode
        )
    anchors = [dict(row) for row in executor_width_anchors(
        explain_document, completed.stdout
    )]
    for row in anchors:
        row["query_id"] = str(args.query_id)
        row["query_sha256"] = sha256(args.query_file)
        row["query_dop"] = 1
        row["source_path"] = str(raw.resolve())
    measured = {str(row["node_signature"]) for row in anchors}
    required = required_width_nodes(parse_explain(explain_document))
    result = {
        "schema": "huawei7.width-anchors/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "query_id": str(args.query_id),
        "work_mem_mb": args.work_mem_mb,
        "executor": "row; enable_vector_engine=off",
        "query_dop": 1,
        "query_sha256": sha256(args.query_file),
        "explain_analyze_sha256": sha256(args.explain_analyze),
        "performance_sha256": sha256(raw),
        "anchors": list(anchors),
        "unmeasured_required_nodes": [
            dict(row) for row in required
            if str(row["node_signature"]) not in measured
        ],
    }
    artifact = args.out_dir / "width_anchors.json"
    artifact.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
