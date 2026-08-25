"""Derive work_mem plan-family switches from a complete blind EXPLAIN grid."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Mapping

from .operator_model import parse_explain, plan_family
from .provenance import sha256


def _blind(document: object) -> None:
    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).startswith("Actual ") or str(key) in (
                    "Total Runtime", "Execution Time", "Planning Time",
                ):
                    raise ValueError("plan-switch evidence contains execution outcome")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(document)


def build_plan_switch_evidence(
    manifest: Mapping[str, object], base: Path,
) -> Dict[str, object]:
    if manifest.get("schema") != "huawei7.plan-switch-manifest/v1":
        raise ValueError("unsupported plan-switch manifest")
    machine = str(manifest.get("machine_fingerprint", ""))
    query_id = int(manifest["query_id"])
    minimum = int(manifest["minimum_mb"])
    maximum = int(manifest["maximum_mb"])
    grid = int(manifest["grid_mb"])
    if not machine or minimum <= 0 or maximum < minimum or grid <= 0:
        raise ValueError("invalid plan-switch grid")
    expected = list(range(minimum, maximum + 1, grid))
    if expected[-1] != maximum:
        expected.append(maximum)
    raw = manifest.get("plans")
    if not isinstance(raw, list):
        raise ValueError("plan-switch plans must be a list")
    by_memory = {}
    query_hashes = set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("plan-switch row must be an object")
        memory = int(row["work_mem_mb"])
        if memory in by_memory:
            raise ValueError("duplicate plan-switch grid point")
        path = Path(str(row["explain"]))
        if not path.is_absolute():
            path = base / path
        document = json.loads(path.read_text(encoding="utf-8"))
        _blind(document)
        collection_path = Path(str(row.get("collection", "")))
        if not collection_path.is_absolute():
            collection_path = base / collection_path
        if not collection_path.is_file():
            raise ValueError("plan-switch row lacks blind collection metadata")
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
        if (
            collection.get("schema") != "huawei7.blind-explain-collection/v1"
            or collection.get("machine_fingerprint") != machine
            or int(collection.get("query_id", -1)) != query_id
            or int(collection.get("work_mem_mb", -1)) != memory
            or collection.get("explain_sha256") != sha256(path)
            or collection.get("executor") != "row; enable_vector_engine=off"
            or int(collection.get("query_dop", -1)) != 1
            or collection.get("blind") is not True
            or collection.get("valid") is not True
        ):
            raise ValueError("blind EXPLAIN metadata does not bind this grid row")
        query_hash = str(collection.get("query_sha256", ""))
        if len(query_hash) != 64:
            raise ValueError("blind EXPLAIN metadata lacks query SHA-256")
        query_hashes.add(query_hash)
        by_memory[memory] = {
            "work_mem_mb": memory, "plan_family": plan_family(parse_explain(document)),
            "explain": str(path.resolve()), "explain_sha256": sha256(path),
            "collection": str(collection_path.resolve()),
            "collection_sha256": sha256(collection_path),
        }
    if sorted(by_memory) != expected:
        raise ValueError(
            "plan-switch evidence must cover the complete uniform grid: "
            "expected=%r actual=%r" % (expected, sorted(by_memory))
        )
    if len(query_hashes) != 1:
        raise ValueError("plan-switch grid rows were not collected from one query")
    switches = []
    previous = None
    for memory in expected:
        family = by_memory[memory]["plan_family"]
        if previous is not None and family != previous:
            switches.append(memory)
        previous = family
    return {
        "schema": "huawei7.plan-switch-evidence/v1",
        "machine_fingerprint": machine, "query_id": query_id,
        "minimum_mb": minimum, "maximum_mb": maximum, "grid_mb": grid,
        "query_sha256": next(iter(query_hashes)),
        "plan_switch_points_mb": switches,
        "plans": [by_memory[memory] for memory in expected], "valid": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = build_plan_switch_evidence(manifest, args.manifest.resolve().parent)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
