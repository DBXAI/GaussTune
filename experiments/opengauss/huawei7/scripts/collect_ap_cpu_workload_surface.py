#!/usr/bin/env python3
"""Collect all AP CPU work points in one reproducible batch.

This is a batch orchestrator, not a TPS calibration script.  It runs each
query/work_mem point in isolation, samples gaussdb process CPU, subtracts the
idle baseline, and writes one resource-only AP CPU surface.  Different
``work_mem`` values cannot be measured simultaneously by one SQL session, so
"one run" here means one command/manifest that executes all isolated points
sequentially with the same protocol and provenance.

The default point set is every query/work_mem option in the independent AP
model bundle.  Use ``--point`` to provide an explicit subset or
``--dry-run`` to inspect the planned batch and its estimated workload time.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.cpu_surface import _cv
from huawei7.provenance import sha256
from scripts.collect_cpu_service_demand import _run_one, _runtime


def _load_bundle(
    path: Path,
    machine_fingerprint: str,
) -> Mapping[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        document.get("schema") != "huawei7.ap-model-bundle/v1"
        or document.get("valid") is not True
        or document.get("machine_fingerprint") != machine_fingerprint
    ):
        raise ValueError("AP model bundle is invalid or belongs to another machine")
    if not isinstance(document.get("query_options"), dict):
        raise ValueError("AP model bundle has no query_options")
    return document


def _all_points(bundle: Mapping[str, object]):
    points = []
    query_options = bundle["query_options"]
    for raw_query, raw_rows in sorted(
        query_options.items(), key=lambda item: int(item[0])
    ):
        if not isinstance(raw_rows, list) or not raw_rows:
            raise ValueError("AP query %s has no model options" % raw_query)
        for raw_row in sorted(
            raw_rows, key=lambda row: int(round(float(row["work_mem_mb"])))
        ):
            points.append({
                "query": str(raw_query),
                "work_mem_mb": int(round(float(raw_row["work_mem_mb"]))),
                "execution_seconds": float(raw_row["execution_seconds"]),
                "read_requests": float(raw_row["read_requests"]),
                "write_requests": float(raw_row["write_requests"]),
                "plan_family": str(raw_row.get("plan_family", "")),
            })
    return points


def _parse_point(spec: str):
    query, raw_work_mem = spec.split("=", 1)
    return str(int(query)), int(raw_work_mem)


def _select_points(
    bundle: Mapping[str, object],
    raw_specs: Sequence[str],
):
    all_points = _all_points(bundle)
    if not raw_specs:
        return all_points
    wanted = {_parse_point(spec) for spec in raw_specs}
    selected = [
        point for point in all_points
        if (point["query"], point["work_mem_mb"]) in wanted
    ]
    missing = wanted - {
        (point["query"], point["work_mem_mb"]) for point in selected
    }
    if missing:
        raise ValueError(
            "requested AP points are absent from bundle: %s"
            % sorted(missing)
        )
    return selected


def _summary_for_point(
    point: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
    *,
    source_dir: Path,
) -> Mapping[str, object]:
    cpu_values = [
        float(row["cpu_seconds_per_unit"]) for row in rows
    ]
    wall_values = [
        float(row["wall_seconds_per_unit"]) for row in rows
    ]
    buffer_values = [
        float(row["buffer_accesses_per_unit"]) for row in rows
    ]
    rate_values = [
        float(row["buffer_accesses_per_second"]) for row in rows
    ]
    return {
        "query": str(point["query"]),
        "work_mem_mb": int(point["work_mem_mb"]),
        "plan_family": str(point["plan_family"]),
        "cpu_seconds_per_query": statistics.fmean(cpu_values),
        "wall_seconds_per_query": statistics.fmean(wall_values),
        "buffer_accesses_per_query": statistics.fmean(buffer_values),
        "buffer_accesses_per_second": statistics.fmean(rate_values),
        "repeats": len(rows),
        "coefficient_of_variation": {
            "cpu_seconds_per_query": _cv(cpu_values),
            "wall_seconds_per_query": _cv(wall_values),
            "buffer_accesses_per_query": _cv(buffer_values),
            "buffer_accesses_per_second": _cv(rate_values),
        },
        "source": {
            "directory": str(source_dir.resolve()),
            "repeat_artifacts": [
                {
                    "path": str(
                        (source_dir / ("repeat-%02d.json" % index)).resolve()
                    ),
                    "sha256": sha256(
                        source_dir / ("repeat-%02d.json" % index)
                    ),
                }
                for index in range(1, len(rows) + 1)
            ],
        },
    }


def _write_document(
    path: Path,
    *,
    machine_fingerprint: str,
    dataset_fingerprint: str,
    bundle_path: Path,
    bundle: Mapping[str, object],
    points: Sequence[Mapping[str, object]],
    rows: Sequence[Mapping[str, object]],
    status: str,
    started_at: str,
    finished_at: str = "",
) -> None:
    document = {
        "schema": "huawei7.ap-cpu-workload-surface/v1",
        "valid": status == "complete",
        "status": status,
        "machine_fingerprint": machine_fingerprint,
        "dataset_fingerprint": dataset_fingerprint,
        "started_at": started_at,
        "finished_at": finished_at,
        "point_count": len(points),
        "completed_point_count": len(rows),
        "planned_points": list(points),
        "rows": list(rows),
        "source_artifacts": {
            "ap_model_bundle": {
                "path": str(bundle_path.resolve()),
                "sha256": sha256(bundle_path),
            },
        },
        "calibration_contract": {
            "final_stage_tps_used": False,
            "target_stage_tps_used_for_calibration": False,
            "mixed_tp_ap_tps_used": False,
            "isolated_workload_only": True,
            "database_buffer_accesses_measured": True,
            "fitted_parameters": False,
            "selection_uses_benchmark_name": False,
        },
        "method": (
            "isolated-ap-process-cpu-sampling-per-query-and-work_mem-v1"
        ),
        "bundle_model_id": bundle.get("model_bundle_id"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--ap-model-bundle", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--restart-command-json", type=Path)
    parser.add_argument("--shared-buffers-mb", type=int, default=8192)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--idle-seconds", type=float, default=15)
    parser.add_argument("--sample-interval-seconds", type=float, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument(
        "--point", action="append", default=[],
        help="restrict points using query=work_mem_mb; repeatable",
    )
    parser.add_argument("--max-points", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--stop-on-error", action="store_true",
        help="abort on the first failed query/work_mem point",
    )
    args = parser.parse_args()
    if args.repeats < 3:
        raise ValueError("AP CPU surface requires at least 3 repeats")
    if args.idle_seconds < 5:
        raise ValueError("idle baseline must be at least 5 seconds")
    if args.shared_buffers_mb <= 0:
        raise ValueError("shared_buffers_mb must be positive")

    config = _runtime(args.runtime_config)
    machine = str(config["machine_fingerprint"])
    bundle = _load_bundle(args.ap_model_bundle, machine)
    points = _select_points(bundle, args.point)
    if args.max_points is not None:
        if args.max_points <= 0:
            raise ValueError("--max-points must be positive")
        points = points[:args.max_points]
    dataset_audit = Path(str(config["dataset_audit"]))
    if not dataset_audit.is_file():
        raise ValueError("runtime dataset audit is missing")
    dataset_document = json.loads(dataset_audit.read_text(encoding="utf-8"))
    dataset_fingerprint = str(dataset_document["dataset_fingerprint"])
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    estimate_seconds = sum(
        float(point["execution_seconds"]) * args.repeats
        + args.repeats * args.idle_seconds
        for point in points
    )
    manifest = {
        "schema": "huawei7.ap-cpu-workload-batch/v1",
        "valid": True,
        "status": "planned",
        "machine_fingerprint": machine,
        "dataset_fingerprint": dataset_fingerprint,
        "runtime_config": {
            "path": str(args.runtime_config.resolve()),
            "sha256": sha256(args.runtime_config),
        },
        "ap_model_bundle": {
            "path": str(args.ap_model_bundle.resolve()),
            "sha256": sha256(args.ap_model_bundle),
        },
        "point_count": len(points),
        "repeats": args.repeats,
        "estimated_query_and_idle_seconds": estimate_seconds,
        "points": points,
        "calibration_contract": {
            "final_stage_tps_used": False,
            "target_stage_tps_used_for_calibration": False,
            "mixed_tp_ap_tps_used": False,
            "fitted_parameters": False,
            "selection_uses_benchmark_name": False,
        },
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out_dir / "batch-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.dry_run:
        print(json.dumps({
            "status": "planned",
            "point_count": len(points),
            "repeats": args.repeats,
            "estimated_query_and_idle_seconds": estimate_seconds,
            "estimated_hours": estimate_seconds / 3600.0,
            "manifest": str(manifest_path.resolve()),
        }, indent=2, sort_keys=True))
        return 0

    summaries = []
    errors = []
    for index, point in enumerate(points, start=1):
        query = str(point["query"])
        work_mem = int(point["work_mem_mb"])
        point_dir = args.out_dir / ("q%s-wm%s" % (query, work_mem))
        point_dir.mkdir(parents=True, exist_ok=True)
        repeat_rows = []
        try:
            for repeat in range(1, args.repeats + 1):
                row = _run_one(
                    config=config,
                    mode="ap",
                    query_id=query,
                    work_mem_mb=work_mem,
                    terminals=1,
                    warmup_seconds=0,
                    measure_seconds=0,
                    out_dir=point_dir,
                    repeat=repeat,
                    idle_seconds=args.idle_seconds,
                    sample_interval_seconds=args.sample_interval_seconds,
                    timeout_seconds=args.timeout_seconds,
                    precondition_dir=point_dir / (
                        "repeat-%02d-state" % repeat
                    ),
                    restart_command_json=args.restart_command_json,
                    dataset_reset_command_json=None,
                    shared_buffers_mb=args.shared_buffers_mb,
                )
                repeat_rows.append(row)
                print(json.dumps({
                    "point": "%s=%d" % (query, work_mem),
                    "point_index": index,
                    "point_count": len(points),
                    "repeat": repeat,
                    "cpu_seconds_per_query": row[
                        "cpu_seconds_per_unit"
                    ],
                }, sort_keys=True), flush=True)
            summary = _summary_for_point(
                point, repeat_rows, source_dir=point_dir
            )
            summaries.append(summary)
        except Exception as exc:
            error = {
                "point": "%s=%d" % (query, work_mem),
                "error": type(exc).__name__ + ": " + str(exc),
            }
            errors.append(error)
            (point_dir / "error.json").write_text(
                json.dumps(error, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(error, sort_keys=True), flush=True)
            if args.stop_on_error:
                break
        partial_path = args.out_dir / "ap-cpu-workload-surface.partial.json"
        _write_document(
            partial_path,
            machine_fingerprint=machine,
            dataset_fingerprint=dataset_fingerprint,
            bundle_path=args.ap_model_bundle,
            bundle=bundle,
            points=points,
            rows=summaries,
            status="partial",
            started_at=started_at,
        )

    status = "complete" if not errors and len(summaries) == len(points) else (
        "failed" if not summaries else "partial"
    )
    finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
    output_path = args.out_dir / "ap-cpu-workload-surface.json"
    _write_document(
        output_path,
        machine_fingerprint=machine,
        dataset_fingerprint=dataset_fingerprint,
        bundle_path=args.ap_model_bundle,
        bundle=bundle,
        points=points,
        rows=summaries,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
    )
    result = {
        "status": status,
        "point_count": len(points),
        "completed_point_count": len(summaries),
        "error_count": len(errors),
        "output": str(output_path.resolve()),
        "manifest": str(manifest_path.resolve()),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
