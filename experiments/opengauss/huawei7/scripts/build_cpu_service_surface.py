#!/usr/bin/env python3
"""Build a leakage-safe CPU service surface from isolated repeat artifacts."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.cpu_surface import (
    build_surface_document, demand_from_repeats, validate_surface_document,
)
from huawei7.provenance import sha256


def _source_artifacts(path: Path, rows):
    artifacts = []
    for i, row in enumerate(rows):
        repeat_path = path / ("repeat-%02d.json" % (i + 1))
        artifacts.append({
            "kind": "cpu_service_repeat",
            "path": str(repeat_path.resolve()),
            "sha256": sha256(repeat_path),
        })
        raw = row.get("raw_cpu_samples", {})
        if isinstance(raw, dict):
            for kind in ("idle", "workload"):
                item = raw.get(kind)
                if isinstance(item, dict):
                    raw_path = Path(str(item.get("path", "")))
                    if (
                        not raw_path.is_file()
                        or sha256(raw_path) != item.get("sha256")
                    ):
                        raise ValueError(
                            "CPU raw sample artifact is missing or changed: %s"
                            % raw_path
                        )
                    artifacts.append({
                        "kind": "cpu_%s_samples" % kind,
                        "path": str(raw_path.resolve()),
                        "sha256": sha256(raw_path),
                    })
    return artifacts


def _load(path: Path, *, machine_fingerprint: str, mode: str, key: str):
    x = json.loads((path / "cpu-service-demand.json").read_text())
    if (
        x.get("schema") != "huawei7.cpu-service-demand/v1"
        or x.get("valid") is not True
        or x.get("machine_fingerprint") != machine_fingerprint
        or x.get("mode") != mode
        or x.get("key") != key
        or not isinstance(x.get("repeats"), list)
        or len(x["repeats"]) < 3
        or x.get("calibration_contract", {}).get("final_stage_tps_used") is not False
        or x.get("calibration_contract", {}).get("mixed_tp_ap_tps_used") is not False
    ):
        raise ValueError("invalid or leakage-prone CPU service artifact: %s" % path)
    return x


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--logical-cpus", type=int, required=True)
    parser.add_argument("--sysbench-dir", type=Path, required=True)
    parser.add_argument("--tpcc-dir", type=Path, required=True)
    parser.add_argument("--ap", action="append", required=True,
                        help="query_id=directory")
    parser.add_argument("--capacity-utilization-limit", type=float, default=1.0)
    parser.add_argument("--capacity-surface", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sysbench = _load(
        args.sysbench_dir, machine_fingerprint=args.machine_fingerprint,
        mode="sysbench", key="sysbench",
    )
    sysbench_rows = sysbench["repeats"]
    sysbench_demand = demand_from_repeats(
        key="sysbench",
        workload="sysbench",
        units=str(sysbench["units"]),
        cpu_seconds=[
            float(row["excess_process_cpu_seconds"]) for row in sysbench_rows
        ],
        unit_counts=[float(row["unit_count"]) for row in sysbench_rows],
        wall_seconds=[
            float(row["workload_seconds"]) for row in sysbench_rows
        ],
        source_artifacts=_source_artifacts(args.sysbench_dir, sysbench_rows),
    )
    tp = _load(
        args.tpcc_dir, machine_fingerprint=args.machine_fingerprint,
        mode="tpcc", key="tpcc",
    )
    tp_rows = tp["repeats"]
    tp_demand = demand_from_repeats(
        key="tpcc",
        workload="tpcc",
        units=str(tp["units"]),
        cpu_seconds=[float(row["excess_process_cpu_seconds"]) for row in tp_rows],
        unit_counts=[float(row["unit_count"]) for row in tp_rows],
        wall_seconds=[float(row["workload_seconds"]) for row in tp_rows],
        source_artifacts=_source_artifacts(args.tpcc_dir, tp_rows),
    )
    ap_demands = {}
    for spec in args.ap:
        query, raw_dir = spec.split("=", 1)
        path = Path(raw_dir)
        doc = _load(
            path, machine_fingerprint=args.machine_fingerprint,
            mode="ap", key=query,
        )
        rows = doc["repeats"]
        ap_demands[query] = demand_from_repeats(
            key=query,
            workload="ap",
            units=str(doc["units"]),
            cpu_seconds=[
                float(row["excess_process_cpu_seconds"]) for row in rows
            ],
            unit_counts=[float(row["unit_count"]) for row in rows],
            wall_seconds=[float(row["workload_seconds"]) for row in rows],
            source_artifacts=_source_artifacts(path, rows),
        )
    document = build_surface_document(
        machine_fingerprint=args.machine_fingerprint,
        logical_cpus=args.logical_cpus,
        tp_demands={"sysbench": sysbench_demand, "tpcc": tp_demand},
        ap_demands=ap_demands,
        capacity_utilization_limit=args.capacity_utilization_limit,
        capacity_surface=json.loads(
            args.capacity_surface.read_text(encoding="utf-8")
        ),
    )
    document["capacity_surface_artifact"] = {
        "path": str(args.capacity_surface.resolve()),
        "sha256": sha256(args.capacity_surface),
    }
    document["input_artifacts"] = {
        "tpcc": {
            "path": str((args.tpcc_dir / "cpu-service-demand.json").resolve()),
            "sha256": sha256(args.tpcc_dir / "cpu-service-demand.json"),
        },
        "sysbench": {
            "path": str(
                (args.sysbench_dir / "cpu-service-demand.json").resolve()
            ),
            "sha256": sha256(
                args.sysbench_dir / "cpu-service-demand.json"
            ),
        },
        **{
            "ap_q%s" % query: {
                "path": str((Path(raw_dir) / "cpu-service-demand.json").resolve()),
                "sha256": sha256(Path(raw_dir) / "cpu-service-demand.json"),
            }
            for query, raw_dir in (spec.split("=", 1) for spec in args.ap)
        },
    }
    validate_surface_document(document)
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
