#!/usr/bin/env python3
"""Freeze five Huawei7 model results into the exact real-run contract."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.stage_execution import validate_model_result_artifacts
from huawei7.stage_spec import read_stage_spec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-spec", type=Path, default=ROOT / "config" / "ppt_five_stages.json")
    parser.add_argument("--machine-fingerprint", required=True)
    for benchmark in ("sysbench", "benchbase-tpcc"):
        for name in ("S1", "S2", "S3", "S4", "S5"):
            parser.add_argument(
                "--%s-%s" % (benchmark, name.lower()), type=Path, required=True,
            )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    stages = read_stage_spec(args.stage_spec)
    rows = []
    global_query_hashes = {}
    dataset_fingerprint = ""
    for benchmark in ("sysbench", "benchbase-tpcc"):
        for stage in stages:
            path = getattr(args, (benchmark + "_" + stage.name.lower()).replace("-", "_"))
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("schema") != "huawei7.ppt-architecture-result/v2":
                raise ValueError("%s is not a Huawei7 architecture result" % path)
            if document.get("machine_fingerprint") != args.machine_fingerprint:
                raise ValueError("%s belongs to a different machine" % path)
            validate_model_result_artifacts(document)
            model_dataset_fingerprint = str(
                document.get("dataset_fingerprint", "")
            )
            if len(model_dataset_fingerprint) != 64:
                raise ValueError("%s lacks an audited dataset fingerprint" % path)
            if (
                dataset_fingerprint
                and model_dataset_fingerprint != dataset_fingerprint
            ):
                raise ValueError("model results use different dataset audits")
            dataset_fingerprint = model_dataset_fingerprint
            if document.get("tp_benchmark") != benchmark:
                raise ValueError("%s was calibrated for a different TP benchmark" % path)
            if int(document.get("tp_terminals", -1)) != stage.tp_terminals:
                raise ValueError("%s TP terminals differ from %s" % (path, stage.name))
            if (
                int(document.get("tp_baseline_terminals", -1))
                != stage.tp_baseline_terminals
                or int(document.get("tp_surge_terminals", -1))
                != stage.tp_surge_terminals
                or document.get("tp_surge_start_phase")
                != ("measurement" if stage.tp_surge_terminals else None)
            ):
                raise ValueError("%s TP surge topology differs from %s" % (path, stage.name))
            best = document.get("best")
            if not isinstance(best, dict):
                raise ValueError("%s has no best candidate" % path)
            assignments = {str(int(query)): int(memory)
                           for query, memory in best["work_mem"]}
            if tuple(sorted(int(query) for query in assignments)) != stage.ap_queries:
                raise ValueError("%s best candidate does not cover %s" % (path, stage.name))
            query_hashes_raw = document.get("ap_query_sha256")
            if not isinstance(query_hashes_raw, dict):
                raise ValueError("%s has no AP query hashes" % path)
            query_hashes = {}
            for query in stage.ap_queries:
                digest = str(query_hashes_raw.get(str(query), ""))
                if len(digest) != 64:
                    raise ValueError("%s lacks Q%d SHA-256" % (path, query))
                if query in global_query_hashes and global_query_hashes[query] != digest:
                    raise ValueError("model results disagree on Q%d SQL identity" % query)
                global_query_hashes[query] = digest
                query_hashes[str(query)] = digest
            rows.append({
                "benchmark": benchmark, "stage": stage.name,
                "tp_terminals": stage.tp_terminals,
                "tp_baseline_terminals": stage.tp_baseline_terminals,
                "tp_surge_terminals": stage.tp_surge_terminals,
                "tp_surge_start_phase": (
                    "measurement" if stage.tp_surge_terminals else None
                ),
                "shared_buffers_mb": int(best["shared_buffers_mb"]),
                "work_mem_by_query": assignments,
                "predicted_tps": float(best["predicted_tps"]),
                "query_sha256": query_hashes,
                "dataset_fingerprint": dataset_fingerprint,
                "model_result": str(path.resolve()),
                "model_result_sha256": sha256(path),
            })
    result = {
        "schema": "huawei7.five-stage-recommendations/v3",
        "machine_fingerprint": args.machine_fingerprint,
        "dataset_fingerprint": dataset_fingerprint,
        "benchmarks": ["sysbench", "benchbase-tpcc"],
        "selection_frozen_before_real_stage_measurements": True,
        "query_sha256": {str(key): value for key, value in sorted(global_query_hashes.items())},
        "stages": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
