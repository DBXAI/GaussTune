"""Collect and validate the target-machine fio surface required by PPT 16."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import shutil
import stat
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .device import DeviceSurface, SurfacePoint


MARKER_SUFFIX = ".huawei7-fio-target.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def marker_path(path: Path) -> Path:
    return Path(str(path) + MARKER_SUFFIX)


def prepare_target(path: Path, size_bytes: int) -> Dict[str, object]:
    """Create one explicitly named disposable fio file and its safety marker."""

    if "huawei7" not in path.name.lower():
        raise ValueError("fio target basename must contain 'huawei7'")
    if size_bytes < 64 * 1024 * 1024:
        raise ValueError("fio target must be at least 64 MiB")
    if path.exists():
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("fio target exists and is not a regular file")
        raise FileExistsError("refusing to overwrite existing fio target %s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        os.posix_fallocate(handle.fileno(), 0, size_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    # fallocate creates unwritten extents.  Reading those can return zeroes
    # without a device request, yielding spectacular but fictitious IOPS.
    # Convert every extent to real data with direct I/O before creating the
    # safety marker accepted by collection.
    fio_path = shutil.which("fio")
    if fio_path is None:
        raise RuntimeError("fio is required to precondition the target")
    precondition = subprocess.run([
        fio_path, "--name=huawei7-precondition", "--filename=%s" % path,
        "--rw=write", "--bs=1m", "--direct=1", "--ioengine=libaio",
        "--iodepth=16", "--size=%d" % size_bytes, "--end_fsync=1",
        "--output-format=json", "--eta=never",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if precondition.returncode != 0:
        raise RuntimeError("fio target preconditioning failed: %s" % precondition.stderr.strip())
    info = {
        "schema": "huawei7.fio-target/v1",
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "device": "%d:%d" % (os.major(path.stat().st_dev), os.minor(path.stat().st_dev)),
        "inode": path.stat().st_ino,
        "created_unix": time.time(),
        "preconditioned": True,
        "precondition_command": "fio direct sequential write bs=1MiB iodepth=16 end_fsync=1",
    }
    marker_path(path).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


def validate_target(path: Path) -> Mapping[str, object]:
    marker = marker_path(path)
    if not path.is_file() or not marker.is_file():
        raise RuntimeError("fio target or Huawei7 safety marker is missing")
    info = json.loads(marker.read_text(encoding="utf-8"))
    if info.get("preconditioned") is not True:
        raise RuntimeError("fio target marker predates mandatory direct-write preconditioning")
    current = path.stat()
    if str(path.resolve()) != info["path"] or current.st_ino != int(info["inode"]):
        raise RuntimeError("fio target no longer matches its safety marker")
    if current.st_size != int(info["size_bytes"]):
        raise RuntimeError("fio target size changed after preparation")
    return info


@dataclass(frozen=True)
class FioPointResult:
    split: str
    repeat: int
    tp_queue_depth: int
    ap_queue_depth: int
    ap_read_fraction: float
    tp_iops: float
    ap_read_iops: float
    ap_write_iops: float
    tp_read_latency_ms: float
    tp_read_p95_ms: float
    runtime_seconds: int


def _latency_ms(job: Mapping[str, object], percentile: Optional[str] = None) -> float:
    read = job["read"]
    if not isinstance(read, dict):
        raise RuntimeError("fio JSON read result is malformed")
    clat = read.get("clat_ns")
    scale = 1e-6
    if not isinstance(clat, dict):
        clat = read.get("clat")
        scale = 1e-3
    if not isinstance(clat, dict):
        raise RuntimeError("fio JSON has no completion-latency object")
    if percentile is None:
        return float(clat["mean"]) * scale
    values = clat.get("percentile", {})
    if not isinstance(values, dict) or percentile not in values:
        raise RuntimeError("fio JSON lacks percentile %s" % percentile)
    return float(values[percentile]) * scale


def run_point(
    *, fio_path: str, target: Path, split: str, repeat: int,
    tp_qd: int, ap_qd: int, ap_read_fraction: float,
    runtime_seconds: int, ramp_seconds: int, seed: int,
    tp_block_kib: int, ap_block_kib: int,
) -> FioPointResult:
    info = validate_target(target)
    size = int(info["size_bytes"])
    region = size // 2
    if tp_qd <= 0 or ap_qd < 0:
        raise ValueError("TP QD must be positive and AP QD non-negative")
    command = [
        fio_path, "--output-format=json", "--eta=never", "--thread=1",
        "--time_based=1", "--runtime=%d" % runtime_seconds,
        "--ramp_time=%d" % ramp_seconds, "--direct=1", "--ioengine=libaio",
        "--randrepeat=1", "--randseed=%d" % seed,
        "--name=tp", "--filename=%s" % target, "--rw=randread",
        "--bs=%dk" % tp_block_kib, "--iodepth=%d" % tp_qd,
        "--offset=0", "--size=%d" % region,
    ]
    if ap_qd > 0:
        command.extend([
            "--name=ap", "--filename=%s" % target, "--rw=randrw",
            "--rwmixread=%d" % round(ap_read_fraction * 100),
            "--bs=%dk" % ap_block_kib, "--iodepth=%d" % ap_qd,
            "--offset=%d" % region, "--size=%d" % region,
        ])
    completed = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("fio failed: %s" % completed.stderr.strip())
    document = json.loads(completed.stdout)
    jobs = {str(job["jobname"]): job for job in document["jobs"]}
    tp = jobs["tp"]
    ap = jobs.get("ap")
    ap_read = float(ap["read"]["iops"]) if ap is not None else 0.0
    ap_write = float(ap["write"]["iops"]) if ap is not None else 0.0
    return FioPointResult(
        split, repeat, tp_qd, ap_qd, ap_read_fraction,
        float(tp["read"]["iops"]), ap_read, ap_write,
        _latency_ms(tp), _latency_ms(tp, "95.000000"), runtime_seconds,
    )


def write_rows(path: Path, rows: Iterable[FioPointResult]) -> None:
    values = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(values[0]).keys()))
        writer.writeheader()
        for row in values:
            writer.writerow(asdict(row))


def read_rows(path: Path) -> List[FioPointResult]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [FioPointResult(
            row["split"], int(row["repeat"]), int(row["tp_queue_depth"]),
            int(row["ap_queue_depth"]), float(row["ap_read_fraction"]),
            float(row["tp_iops"]), float(row["ap_read_iops"]),
            float(row["ap_write_iops"]), float(row["tp_read_latency_ms"]),
            float(row["tp_read_p95_ms"]), int(row["runtime_seconds"]),
        ) for row in csv.DictReader(handle)]


def median_surface(rows: Sequence[FioPointResult]) -> Tuple[SurfacePoint, ...]:
    grouped: Dict[Tuple[int, int], List[float]] = {}
    for row in rows:
        grouped.setdefault((row.tp_queue_depth, row.ap_queue_depth), []).append(
            row.tp_read_latency_ms
        )
    points = [SurfacePoint(float(tp), float(ap), statistics.median(values))
              for (tp, ap), values in grouped.items()]
    # QD->0 boundary uses the measured QD1 service latency for interpolation;
    # it is an explicit derived boundary, not a new measurement.
    ap_axis = sorted({point.ap_queue_depth for point in points})
    for ap in ap_axis:
        at_ap = [point for point in points if point.ap_queue_depth == ap]
        minimum = min(at_ap, key=lambda point: point.tp_queue_depth)
        if minimum.tp_queue_depth > 0:
            points.append(SurfacePoint(0.0, ap, minimum.tp_read_latency_ms))
    return tuple(points)


def validate_holdout(
    training: Sequence[FioPointResult], holdout: Sequence[FioPointResult],
    machine_fingerprint: str, maximum_mape: float,
) -> Dict[str, object]:
    training_grid = {(row.tp_queue_depth, row.ap_queue_depth) for row in training}
    holdout_grid = {(row.tp_queue_depth, row.ap_queue_depth) for row in holdout}
    training_repeats = [
        sum((row.tp_queue_depth, row.ap_queue_depth) == point for row in training)
        for point in training_grid
    ]
    holdout_repeats = [
        sum((row.tp_queue_depth, row.ap_queue_depth) == point for row in holdout)
        for point in holdout_grid
    ]
    quality_valid = (
        len(training_grid) >= 4 and len(holdout_grid) >= 3
        and not (training_grid & holdout_grid)
        and min(training_repeats, default=0) >= 3
        and min(holdout_repeats, default=0) >= 3
    )
    read_fraction = statistics.median(row.ap_read_fraction for row in training)
    surface = DeviceSurface(
        median_surface(training), machine_fingerprint, ap_read_fraction=read_fraction,
    )
    errors = []
    details = []
    for row in holdout:
        predicted = surface.latency_ms(row.tp_queue_depth, row.ap_queue_depth)
        error = abs(predicted - row.tp_read_latency_ms) / row.tp_read_latency_ms
        errors.append(error)
        details.append({
            "tp_queue_depth": row.tp_queue_depth,
            "ap_queue_depth": row.ap_queue_depth,
            "actual_ms": row.tp_read_latency_ms,
            "predicted_ms": predicted,
            "absolute_percentage_error": error,
        })
    mape = sum(errors) / len(errors) if errors else float("inf")
    return {
        "schema": "huawei7.fio-surface-holdout/v1",
        "machine_fingerprint": machine_fingerprint,
        "ap_read_fraction": read_fraction,
        "mape": mape,
        "maximum_mape": maximum_mape,
        "accepted": bool(errors) and quality_valid and mape <= maximum_mape,
        "quality_valid": quality_valid,
        "training_grid_points": len(training_grid),
        "holdout_grid_points": len(holdout_grid),
        "minimum_training_repeats": min(training_repeats, default=0),
        "minimum_holdout_repeats": min(holdout_repeats, default=0),
        "grid_overlap": len(training_grid & holdout_grid),
        "points": details,
        "surface": [asdict(point) for point in median_surface(training)],
        "derived_boundary": "tp_qd=0 copies measured minimum-TP-QD latency at each AP QD",
    }


def validate_fio_report_evidence(document: Mapping[str, object]) -> None:
    """Rebuild a v2 validation report from its disjoint raw CSV inputs."""

    if document.get("schema") != "huawei7.fio-surface-holdout/v2":
        raise ValueError("fio validation report is not source-bound v2 evidence")
    inputs = document.get("input_artifacts")
    if not isinstance(inputs, dict) or set(inputs) != {"training", "holdout"}:
        raise ValueError("fio validation report lacks training/holdout inputs")
    paths = {}
    for name in ("training", "holdout"):
        row = inputs[name]
        if not isinstance(row, dict):
            raise ValueError("invalid fio %s input artifact" % name)
        path = Path(str(row.get("path", "")))
        if not path.is_file() or file_sha256(path) != row.get("sha256"):
            raise ValueError("fio %s input is missing or changed" % name)
        paths[name] = path
    expected = validate_holdout(
        read_rows(paths["training"]), read_rows(paths["holdout"]),
        str(document.get("machine_fingerprint", "")),
        float(document.get("maximum_mape", -1)),
    )
    expected.pop("schema", None)
    actual = {
        key: value for key, value in document.items()
        if key not in ("schema", "input_artifacts")
    }
    if actual != expected:
        raise ValueError("fio validation report differs from recomputed raw CSV result")


def validate_fio_surface_set_evidence(document: Mapping[str, object]) -> None:
    """Rehash and recompute every independently validated AP-mix surface."""

    if document.get("schema") != "huawei7.fio-surface-set/v1":
        raise ValueError("fio surface set has an unsupported schema")
    machine = str(document.get("machine_fingerprint", ""))
    reports = document.get("reports")
    if document.get("valid") is not True or not machine or not isinstance(reports, list):
        raise ValueError("fio surface set identity is invalid")
    if not reports:
        raise ValueError("fio surface set is empty")
    fractions = []
    for row in reports:
        if not isinstance(row, dict):
            raise ValueError("fio surface set contains an invalid report row")
        path = Path(str(row.get("path", "")))
        if not path.is_file() or file_sha256(path) != row.get("sha256"):
            raise ValueError("fio surface-set report is missing or changed")
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("fio surface-set report root must be an object")
        validate_fio_report_evidence(report)
        fraction = float(report.get("ap_read_fraction", -1))
        if (
            report.get("machine_fingerprint") != machine
            or report.get("accepted") is not True
            or fraction != float(row.get("ap_read_fraction", -2))
        ):
            raise ValueError("fio surface-set report identity differs from its row")
        fractions.append(fraction)
    if fractions != sorted(fractions) or len(fractions) != len(set(fractions)):
        raise ValueError("fio surface-set fractions must be unique and sorted")


def parse_axis(text: str) -> List[int]:
    values = sorted({int(value) for value in text.split(",") if value.strip()})
    if not values:
        raise ValueError("empty queue-depth axis")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("path", type=Path)
    prepare.add_argument("--size-gib", type=float, required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("path", type=Path)
    collect.add_argument("out", type=Path)
    collect.add_argument("--split", choices=("train", "holdout"), required=True)
    collect.add_argument("--tp-qd", required=True)
    collect.add_argument("--ap-qd", required=True)
    collect.add_argument("--repeats", type=int, default=3)
    collect.add_argument("--runtime-seconds", type=int, default=30)
    collect.add_argument("--ramp-seconds", type=int, default=5)
    collect.add_argument("--ap-read-fraction", type=float, required=True)
    collect.add_argument("--tp-block-kib", type=int, default=8)
    collect.add_argument("--ap-block-kib", type=int, required=True)
    collect.add_argument("--seed", type=int, default=15721)
    validate = sub.add_parser("validate")
    validate.add_argument("training", type=Path)
    validate.add_argument("holdout", type=Path)
    validate.add_argument("out", type=Path)
    validate.add_argument("--machine-fingerprint", required=True)
    validate.add_argument("--maximum-mape", type=float, default=0.20)
    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare_target(args.path, int(args.size_gib * 1024 ** 3)), indent=2))
        return 0
    if args.command == "collect":
        fio_path = shutil.which("fio")
        if fio_path is None:
            raise RuntimeError("fio is not installed")
        if not 0 <= args.ap_read_fraction <= 1 or args.repeats <= 0:
            raise ValueError("invalid AP mix/repeat count")
        grid = [(tp, ap, repeat) for tp in parse_axis(args.tp_qd)
                for ap in parse_axis(args.ap_qd) for repeat in range(1, args.repeats + 1)]
        random.Random(args.seed).shuffle(grid)
        rows = []
        for tp, ap, repeat in grid:
            row = run_point(
                fio_path=fio_path, target=args.path, split=args.split, repeat=repeat,
                tp_qd=tp, ap_qd=ap, ap_read_fraction=args.ap_read_fraction,
                runtime_seconds=args.runtime_seconds, ramp_seconds=args.ramp_seconds,
                seed=args.seed + repeat + tp * 101 + ap * 1009,
                tp_block_kib=args.tp_block_kib, ap_block_kib=args.ap_block_kib,
            )
            rows.append(row)
            print(json.dumps(asdict(row), sort_keys=True), flush=True)
        write_rows(args.out, rows)
        return 0
    report = validate_holdout(
        read_rows(args.training), read_rows(args.holdout),
        args.machine_fingerprint, args.maximum_mape,
    )
    report["schema"] = "huawei7.fio-surface-holdout/v2"
    report["input_artifacts"] = {
        "training": {
            "path": str(args.training.resolve()), "sha256": file_sha256(args.training),
        },
        "holdout": {
            "path": str(args.holdout.resolve()), "sha256": file_sha256(args.holdout),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
