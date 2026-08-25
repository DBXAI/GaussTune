#!/usr/bin/env python3
"""Collect conservative pg_column_size projection factors for one plan family."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.operator_model import parse_explain, plan_family
from huawei7.operator_width_evidence import required_width_nodes
from huawei7.provenance import sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--explain", type=Path, required=True)
    parser.add_argument("--sample-sql", type=Path, required=True)
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
    root = parse_explain(json.loads(args.explain.read_text(encoding="utf-8")))
    sql = args.sample_sql.read_text(encoding="utf-8").strip()
    if not sql:
        raise ValueError("projection sampling SQL is empty")
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = str(args.library_dir)
    completed = subprocess.run([
        str(args.gsql), "-2", "-X", "-At", "-F", "\t",
        "-v", "ON_ERROR_STOP=1", "-h", args.host,
        "-p", str(args.port), "-U", args.user, "-d", args.database,
        "-c", sql,
    ], input=password + "\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment, check=False)
    if completed.returncode:
        raise RuntimeError(
            "projection width SQL failed: %s" % completed.stderr.strip()
        )
    fields = completed.stdout.strip().split("\t")
    if len(fields) != 2:
        raise ValueError("projection SQL must return avg_width and sample_count")
    measured_width, sample_count = float(fields[0]), int(fields[1])
    if measured_width <= 0 or sample_count < 30 or root.plan_width <= 0:
        raise ValueError("projection width requires a positive >=30-row sample")
    observed_ratio = measured_width / root.plan_width
    conservative_ratio = max(1.0, observed_ratio)
    family = plan_family(root)
    sql_sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    anchors = []
    for node in required_width_nodes(root):
        plan_width = float(node["plan_width"])
        anchors.append({
            "node_signature": str(node["node_signature"]),
            "plan_family": family,
            "plan_width": plan_width,
            "actual_width": plan_width * conservative_ratio,
            "method": "pg_column_size_family_projection",
            "sample_count": sample_count,
            "source_sha256": sql_sha,
            "sample_sql": sql,
            "sample_sql_path": str(args.sample_sql.resolve()),
            "sample_sql_sha256": sha256(args.sample_sql),
            "query_id": str(args.query_id),
            "query_sha256": sha256(args.query_file),
            "query_dop": 1,
            "derivation": (
                "node_plan_width * max(1, measured_projection_width / "
                "root_plan_width)"
            ),
            "measured_projection_width": measured_width,
            "root_plan_width": root.plan_width,
            "observed_ratio": observed_ratio,
            "conservative_ratio": conservative_ratio,
        })
    if not anchors:
        raise ValueError("plan family contains no modeled width nodes")
    result = {
        "schema": "huawei7.width-anchors/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "query_id": str(args.query_id),
        "query_sha256": sha256(args.query_file),
        "query_dop": 1,
        "plan_family": family,
        "explain": str(args.explain.resolve()),
        "explain_sha256": sha256(args.explain),
        "sample_sql": str(args.sample_sql.resolve()),
        "sample_sql_sha256": sha256(args.sample_sql),
        "measured_projection_width": measured_width,
        "sample_count": sample_count,
        "observed_ratio": observed_ratio,
        "conservative_ratio": conservative_ratio,
        "anchors": anchors,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        raise FileExistsError("refusing to overwrite width artifact: %s" % args.out)
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "query_id": str(args.query_id), "plan_family": family,
        "sample_count": sample_count,
        "measured_projection_width": measured_width,
        "conservative_ratio": conservative_ratio,
        "anchor_count": len(anchors),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
