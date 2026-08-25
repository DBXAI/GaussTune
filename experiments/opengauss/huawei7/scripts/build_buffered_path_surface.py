#!/usr/bin/env python3
"""Freeze a resource-only database-buffered TP access-latency surface.

Each input repeat is a controlled mixed TP/AP run with both the database
Buffer Manager probe and the device request attribution enabled.  The AP
pressure coordinate is derived from measured AP device IOPS and independently
measured service times.  The builder uses medians and piecewise-linear
interpolation only and never reads a TPS target.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.buffered_path import (
    BufferedPathPoint,
    BufferedTPRequestSurface,
    summarize_buffered_repeats,
)
from huawei7.provenance import sha256


def _service_times(path: Path) -> Mapping[str, float]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        document.get("schema") != "huawei7.service-times/v2"
        or document.get("valid") is not True
    ):
        raise ValueError("service-time artifact is invalid")
    values = document.get("service_times_ms")
    if not isinstance(values, dict):
        raise ValueError("service-time artifact lacks service_times_ms")
    result = {
        "ap_read_ms": float(values["ap_read_ms"]),
        "ap_write_ms": float(values["ap_write_ms"]),
    }
    if min(result.values()) <= 0:
        raise ValueError("AP service times must be positive")
    return result


def _normalize(
    path: Path,
    *,
    service: Mapping[str, float],
    ap_buffer_demands: Mapping[str, float] = None,
    ap_options: Mapping[str, Mapping[int, Mapping[str, object]]] = None,
    ap_reference_work_mem: Mapping[str, int] = None,
) -> Mapping[str, object]:
    row = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(row, dict):
        raise ValueError("repeat artifact must be an object: %s" % path)
    if row.get("schema") != "huawei7.mixed-resource-repeat/v1":
        raise ValueError("unsupported mixed-resource repeat schema: %s" % path)
    buffered = row.get("buffered_path")
    if not isinstance(buffered, dict):
        raise ValueError("repeat lacks buffered_path measurements: %s" % path)
    database = buffered.get("database")
    device = buffered.get("device")
    if not isinstance(database, dict):
        raise ValueError("repeat lacks database buffered measurements: %s" % path)
    # The database Buffer Manager path is the pressure surface.  A device
    # trace is optional: a fully cached AP scan may have zero device IOPS
    # while still creating substantial AP buffer pressure.  In that case the
    # AP mix coordinate defaults to read-only instead of inventing a device
    # request count.
    if not isinstance(device, dict):
        device = {}
    ap_read_iops = float(device.get("ap_read_iops", 0.0))
    ap_write_iops = float(device.get("ap_write_iops", 0.0))
    # The database Buffer Manager demand is the pressure coordinate.  This
    # remains meaningful when AP scans are served from shared buffers and
    # therefore generate zero physical-device IOPS.
    ap_buffer_rate = float(
        database.get("ap_buffer_accesses_per_second", 0.0)
    )
    if ap_buffer_demands is not None:
        specs = row.get("query_specs", [])
        if not isinstance(specs, list):
            raise ValueError("repeat query_specs is invalid: %s" % path)
        ap_buffer_rate = 0.0
        for spec in specs:
            query = str(spec["query"])
            base_rate = float(ap_buffer_demands[query])
            ratio = 1.0
            if ap_options is not None:
                work_mem = int(spec["work_mem_mb"])
                reference_work_mem = int(ap_reference_work_mem[query])
                candidate = ap_options[query][work_mem]
                reference = ap_options[query][reference_work_mem]
                ratio = (
                    float(candidate["logical_read_pages"])
                    / max(float(reference["logical_read_pages"]), 1e-12)
                    * float(reference["execution_seconds"])
                    / max(float(candidate["execution_seconds"]), 1e-12)
                )
            ap_buffer_rate += base_rate * ratio
    if ap_buffer_rate < 0:
        raise ValueError("AP buffer access rate cannot be negative")
    if ap_buffer_rate > 0:
        ap_queue_depth = ap_buffer_rate
        pressure_axis = "ap_buffer_accesses_per_second"
    else:
        ap_queue_depth = (
            ap_read_iops * float(service["ap_read_ms"])
            + ap_write_iops * float(service["ap_write_ms"])
        ) / 1000.0
        pressure_axis = "ap_device_queue_depth"
    normalized = dict(row)
    normalized["buffered_path"] = dict(buffered)
    normalized["buffered_path"]["ap_queue_depth"] = ap_queue_depth
    normalized["buffered_path"]["ap_buffer_accesses_per_second"] = (
        ap_buffer_rate
    )
    normalized["buffered_path"]["pressure_axis"] = pressure_axis
    normalized["buffered_path"]["tp_buffer_access_await_ms"] = float(
        database["tp_buffer_access_await_ms"]
    )
    normalized["buffered_path"]["tp_buffer_accesses_per_tx"] = float(
        database["tp_buffer_accesses_per_tx"]
    )
    normalized["buffered_path"]["ap_read_fraction"] = (
        ap_read_iops / (ap_read_iops + ap_write_iops)
        if ap_read_iops + ap_write_iops > 0 else 1.0
    )
    normalized["_source_path"] = str(path.resolve())
    return normalized


def _point_document(point: BufferedPathPoint) -> Mapping[str, object]:
    return {
        "ap_queue_depth": point.ap_queue_depth,
        "tp_buffer_access_await_ms": point.tp_buffer_access_await_ms,
        "tp_buffer_accesses_per_tx": point.tp_buffer_accesses_per_tx,
        "repeats": point.repeats,
        "ap_read_fraction": point.ap_read_fraction,
    }


def _surface_from_points(
    points: Sequence[BufferedPathPoint],
    *,
    machine_fingerprint: str,
) -> BufferedTPRequestSurface:
    baseline = min(points, key=lambda point: point.ap_queue_depth)
    if baseline.ap_queue_depth > 1e-9:
        raise ValueError("training repeats lack an AP-free baseline point")
    positive = [point for point in points if point.ap_queue_depth > 1e-9]
    if not positive:
        raise ValueError("buffered surface needs AP pressure points")
    fractions = [point.ap_read_fraction for point in positive]
    if max(fractions) - min(fractions) > 0.05:
        raise ValueError("AP read/write mix changes across buffered points")
    return BufferedTPRequestSurface(
        points,
        machine_fingerprint,
        baseline_tp_buffer_access_await_ms=(
            baseline.tp_buffer_access_await_ms
        ),
        ap_read_fraction=statistics.median(fractions),
        minimum_repeats=3,
    )


def _validate_holdout(
    surface: BufferedTPRequestSurface,
    rows: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    if not rows:
        return {
            "provided": False,
            "accepted": False,
            "rows": [],
            "mean_absolute_error_fraction": None,
            "maximum_absolute_error_fraction": None,
        }
    grouped = {}
    for row in rows:
        buffered = row["buffered_path"]
        pressure = float(buffered["ap_queue_depth"])
        actual = float(buffered["tp_buffer_access_await_ms"])
        grouped.setdefault(pressure, []).append((actual, str(row["_source_path"])))
    errors = []
    holdout_points = []
    for pressure, measurements in sorted(grouped.items()):
        predicted = surface.latency_ms(pressure)
        if (
            pressure <= surface.ap_axis[0] + 1e-9
            or pressure >= surface.ap_axis[-1] - 1e-9
        ):
            raise ValueError(
                "holdout AP queue must be strictly inside training domain: %.6g"
                % pressure
            )
        actual = statistics.median(item[0] for item in measurements)
        error = abs(predicted - actual) / actual
        errors.append(error)
        holdout_points.append({
            "sources": [item[1] for item in measurements],
            "ap_queue_depth": pressure,
            "actual_tp_buffer_access_await_ms": actual,
            "predicted_tp_buffer_access_await_ms": predicted,
            "absolute_error_fraction": error,
            "repeat_count": len(measurements),
        })
    mean_error = statistics.fmean(errors)
    maximum_error = max(errors)
    return {
        "provided": True,
        "accepted": mean_error <= 0.10 and maximum_error <= 0.20,
        "rows": holdout_points,
        "mean_absolute_error_fraction": mean_error,
        "maximum_absolute_error_fraction": maximum_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", action="append", required=True, type=Path)
    parser.add_argument("--holdout-repeat", action="append", default=[], type=Path)
    parser.add_argument("--service-times", required=True, type=Path)
    parser.add_argument(
        "--ap-buffer-demand-surface", required=True, type=Path,
        help="isolated AP buffer-access demand used for the pressure axis",
    )
    parser.add_argument(
        "--ap-model-bundle", type=Path,
        help=(
            "optional independent AP bundle; when supplied, project the "
            "pressure axis by candidate logical-page rate for each work_mem"
        ),
    )
    parser.add_argument(
        "--tp-terminals", type=int, default=128,
        help="TP terminal count at which the DB request surface was measured",
    )
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if len(args.repeat) < 9:
        parser.error("need at least three pressure points x three repeats")
    service = _service_times(args.service_times)
    demand_document = json.loads(
        args.ap_buffer_demand_surface.read_text(encoding="utf-8")
    )
    if (
        demand_document.get("schema")
        != "huawei7.ap-buffer-demand-surface/v1"
        or demand_document.get("valid") is not True
        or demand_document.get("contains_tps_labels") is not False
        or demand_document.get("fitted_parameters") is not False
        or demand_document.get("machine_fingerprint") != args.machine_fingerprint
    ):
        raise ValueError("AP buffer demand surface is invalid or fitted")
    ap_buffer_demands = {}
    ap_reference_work_mem = {}
    reference_pattern = re.compile(r"q(\d+)-wm(\d+)")
    for item in demand_document.get("rows", []):
        query = str(item["query"])
        ap_buffer_demands[query] = float(
            item["buffer_accesses_per_second"]
        )
        source = item.get("source", {})
        match = reference_pattern.search(
            str(source.get("path", "")) if isinstance(source, dict) else ""
        )
        if match and match.group(1) == query:
            ap_reference_work_mem[query] = int(match.group(2))
    if len(ap_buffer_demands) < 3:
        raise ValueError("AP buffer demand surface has too few queries")
    ap_options = None
    if args.ap_model_bundle is not None:
        ap_document = json.loads(
            args.ap_model_bundle.read_text(encoding="utf-8")
        )
        if (
            ap_document.get("schema") != "huawei7.ap-model-bundle/v1"
            or ap_document.get("valid") is not True
            or ap_document.get("machine_fingerprint") != args.machine_fingerprint
        ):
            raise ValueError("AP model bundle is invalid or belongs to another machine")
        ap_options = {}
        pattern = re.compile(r"q(\d+)-wm(\d+)")
        for query, rows in ap_document.get("query_options", {}).items():
            ap_options[str(query)] = {
                int(row["work_mem_mb"]): row
                for row in rows
            }
        missing = set(ap_buffer_demands) - set(ap_reference_work_mem)
        if missing:
            raise ValueError(
                "AP model bundle lacks reference work_mem for %s"
                % sorted(missing)
            )
    training_rows = tuple(
        _normalize(
            path,
            service=service,
            ap_buffer_demands=ap_buffer_demands,
            ap_options=ap_options,
            ap_reference_work_mem=ap_reference_work_mem,
        )
        for path in args.repeat
    )
    if {
        str(row.get("machine_fingerprint", "")) for row in training_rows
    } != {args.machine_fingerprint}:
        raise ValueError("training repeats belong to different machines")
    points = summarize_buffered_repeats(
        training_rows,
        maximum_await_cv=0.25,
        maximum_queue_cv=0.10,
        maximum_access_cv=0.10,
    )
    surface = _surface_from_points(
        points, machine_fingerprint=args.machine_fingerprint,
    )
    baseline_point = min(
        points, key=lambda point: point.ap_queue_depth
    )
    holdout_rows = tuple(
        _normalize(
            path,
            service=service,
            ap_buffer_demands=ap_buffer_demands,
            ap_options=ap_options,
            ap_reference_work_mem=ap_reference_work_mem,
        )
        for path in args.holdout_repeat
    )
    if any(
        str(row.get("machine_fingerprint", "")) != args.machine_fingerprint
        for row in holdout_rows
    ):
        raise ValueError("holdout repeats belong to a different machine")
    training_keys = {
        str(row.get("pressure_point", row.get("stage_key", "")))
        for row in training_rows
    }
    if any(
        str(row.get("pressure_point", row.get("stage_key", ""))) in training_keys
        for row in holdout_rows
    ):
        raise ValueError("holdout pressure point overlaps training")
    holdout = _validate_holdout(surface, holdout_rows)
    document = {
        "schema": "huawei7.buffered-tp-request-surface/v1",
        "valid": True,
        "machine_fingerprint": args.machine_fingerprint,
        "contains_tps_labels": False,
        "method": "resource-only-db-buffer-access-vs-ap-queue-v1",
        "pressure_axis": "ap_buffer_accesses_per_second",
        "tp_terminals": int(args.tp_terminals),
        "workload_signature": {
            "method": "resource-feature-domain-v1",
            "features": [
                "tp_terminals",
                "native_tp_buffer_accesses_per_tx",
            ],
            "baseline_tp_buffer_accesses_per_tx": (
                baseline_point.tp_buffer_accesses_per_tx
            ),
            "relative_tp_buffer_access_tolerance": 0.10,
            "no_benchmark_name_matching": True,
        },
        "interpolation": "piecewise_linear_median_resource_points",
        "fitted_parameters": False,
        "baseline_tp_buffer_access_await_ms": (
            surface.baseline_tp_buffer_access_await_ms
        ),
        "ap_read_fraction": surface.ap_read_fraction,
        "ap_mix_tolerance": surface.ap_mix_tolerance,
        "minimum_repeats_per_point": 3,
        "points": [_point_document(point) for point in surface.points],
        "training_repeats": [
            {"path": str(path.resolve()), "sha256": sha256(path)}
            for path in args.repeat
        ],
        "holdout": holdout,
        "service_times": {
            "path": str(args.service_times.resolve()),
            "sha256": sha256(args.service_times),
        },
        "ap_buffer_demand_surface": {
            "path": str(args.ap_buffer_demand_surface.resolve()),
            "sha256": sha256(args.ap_buffer_demand_surface),
        },
        "ap_model_bundle": (
            {
                "path": str(args.ap_model_bundle.resolve()),
                "sha256": sha256(args.ap_model_bundle),
                "pressure_projection": (
                    "candidate_logical_page_rate_projection"
                ),
            }
            if args.ap_model_bundle is not None else None
        ),
        "stability_gate": {
            "maximum_await_cv": 0.25,
            "maximum_pressure_cv": 0.10,
            "maximum_access_cv": 0.10,
            "reason": (
                "the database request latency numerator is sampled; robust "
                "median points are retained and the disjoint holdout is the "
                "recommendation gate"
            ),
        },
        "calibration_contract": {
            "final_stage_tps_used": False,
            "target_stage_tps_used_for_calibration": False,
            "mixed_tp_ap_tps_used": False,
            "resource_only_output": True,
            "database_request_latency_measured": True,
            "no_regression_or_stage_factor": True,
        },
        "accepted_for_recommendation": bool(holdout["accepted"]),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "schema": document["schema"],
        "points": len(document["points"]),
        "holdout_accepted": document["accepted_for_recommendation"],
        "valid": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
