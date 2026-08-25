#!/usr/bin/env python3
"""Execute explicit pg_column_size sampling SQL for EXPLAIN plan nodes."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.operator_model import parse_explain, plan_family, walk_plan
from huawei7.provenance import sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--explain", type=Path, required=True)
    parser.add_argument("--query-file", type=Path, required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--gsql", type=Path, required=True)
    parser.add_argument("--library-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    password = os.environ.get(args.password_env, "")
    if not password:
        raise RuntimeError("password environment variable is unset")
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    if spec.get("schema") != "huawei7.width-sampling-spec/v1":
        raise ValueError("unsupported width sampling spec")
    root = parse_explain(json.loads(args.explain.read_text(encoding="utf-8")))
    family = plan_family(root)
    nodes = {node.signature: node for node in walk_plan(root)}
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = str(args.library_dir)
    anchors = []
    for row in spec.get("samples", []):
        if not isinstance(row, dict):
            raise ValueError("width sample must be an object")
        signature = str(row["node_signature"])
        node = nodes.get(signature)
        if node is None:
            raise ValueError("width spec references a node outside this plan: %s" % signature)
        sql = str(row["sql"])
        completed = subprocess.run([
            str(args.gsql), "-2", "-X", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1",
            "-h", args.host, "-p", str(args.port), "-U", args.user,
            "-d", args.database, "-c", sql,
        ], input=password + "\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=environment, check=False)
        if completed.returncode != 0:
            raise RuntimeError("width SQL failed for %s: %s" % (
                signature, completed.stderr.strip(),
            ))
        fields = completed.stdout.strip().split("\t")
        if len(fields) != 2:
            raise ValueError("width SQL must return exactly avg_width and sample_count")
        actual_width, sample_count = float(fields[0]), int(fields[1])
        if actual_width <= 0 or sample_count < 30:
            raise ValueError("width sample must have positive average and >=30 rows")
        anchors.append({
            "node_signature": signature, "plan_family": family,
            "plan_width": node.plan_width, "actual_width": actual_width,
            "method": "pg_column_size", "sample_count": sample_count,
            "source_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "sample_sql": sql,
            "query_id": str(args.query_id),
            "query_sha256": sha256(args.query_file),
            "query_dop": 1,
            "label": str(row.get("label", "")),
        })
    if not anchors:
        raise ValueError("width sampling spec contains no samples")
    result = {
        "schema": "huawei7.width-anchors/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "query_id": str(args.query_id),
        "query_sha256": sha256(args.query_file),
        "query_dop": 1,
        "explain": str(args.explain.resolve()),
        "anchors": anchors,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
