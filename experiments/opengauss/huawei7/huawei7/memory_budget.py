"""Derive the PPT fixed allocatable memory pool from repeated host evidence."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Dict, List, Mapping, Tuple

from .provenance import sha256


def _resolve(base: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else base / path


def _linear_fit(points: List[Tuple[float, float]]) -> Tuple[float, float]:
    mean_x = statistics.fmean(x for x, _ in points)
    mean_y = statistics.fmean(y for _, y in points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator <= 0:
        raise ValueError("memory snapshots need distinct shared_buffers settings")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    return slope, mean_y - slope * mean_x


def _validate_snapshot_document(
    document: Mapping[str, object], machine: str,
) -> None:
    if (
        document.get("schema") != "huawei7.memory-snapshot/v1"
        or document.get("machine_fingerprint") != machine
        or document.get("valid") is not True
    ):
        raise ValueError("invalid or cross-machine memory snapshot")
    samples = document.get("samples")
    idle_checks = document.get("idle_checks")
    if not isinstance(samples, list) or len(samples) < 3:
        raise ValueError("each SB memory snapshot needs at least three samples")
    if not isinstance(idle_checks, list) or len(idle_checks) < len(samples):
        raise ValueError("memory snapshot lacks measured idle-session checks")
    for check in idle_checks:
        if (
            not isinstance(check, dict)
            or int(check.get("active_sessions_before", -1)) != 0
            or int(check.get("active_sessions_after", -1)) != 0
        ):
            raise ValueError("memory snapshot was taken with an active client session")


def validate_memory_budget_evidence(
    budget: Mapping[str, object], machine: str,
) -> Tuple[Path, ...]:
    """Rehash and revalidate every snapshot underneath a budget artifact."""

    evidence = budget.get("snapshot_evidence")
    if not isinstance(evidence, list) or len(evidence) < 3:
        raise ValueError("memory budget needs at least three snapshot artifacts")
    paths = []
    settings = set()
    for row in evidence:
        if not isinstance(row, dict):
            raise ValueError("memory snapshot evidence must be an object")
        path = Path(str(row.get("path", "")))
        if not path.is_file() or sha256(path) != str(row.get("sha256", "")):
            raise ValueError("memory snapshot evidence is missing or changed")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("memory snapshot must be an object")
        _validate_snapshot_document(document, machine)
        setting = float(document["shared_buffers_mb"])
        if setting in settings:
            raise ValueError("memory snapshot SB settings must be distinct")
        settings.add(setting)
        paths.append(path)
    return tuple(paths)


def build_memory_budget(manifest: Mapping[str, object], base: Path) -> Dict[str, object]:
    if manifest.get("schema") != "huawei7.memory-budget-manifest/v1":
        raise ValueError("unsupported memory-budget manifest")
    machine = str(manifest.get("machine_fingerprint", ""))
    safety_margin_mb = float(manifest.get("safety_margin_mb", -1))
    if not machine or safety_margin_mb < 0:
        raise ValueError("machine fingerprint and nonnegative safety margin are required")
    raw = manifest.get("snapshots")
    if not isinstance(raw, list) or len(raw) < 3:
        raise ValueError("memory budget requires at least three SB settings")
    documents = []
    evidence = []
    seen = set()
    for value in raw:
        path = _resolve(base, value)
        document = json.loads(path.read_text(encoding="utf-8"))
        _validate_snapshot_document(document, machine)
        sb = float(document["shared_buffers_mb"])
        if sb in seen:
            raise ValueError("memory snapshot SB settings must be distinct")
        seen.add(sb)
        documents.append(document)
        evidence.append({"path": str(path.resolve()), "sha256": sha256(path)})
    hosts = {int(document["memory_bytes"]) for document in documents}
    if len(hosts) != 1:
        raise ValueError("MemTotal changed across memory snapshots")
    points = []
    private_values = []
    system_values = []
    for document in documents:
        samples = document["samples"]
        sb = float(document["shared_buffers_mb"])
        points.append((sb, statistics.median(
            float(row["sysv_virtual_mb"]) for row in samples
        )))
        private_values.extend(float(row["private_rss_mb"]) for row in samples)
        system_values.extend(float(row["non_db_nonreclaimable_mb"]) for row in samples)
    slope, intercept = _linear_fit(points)
    if not .95 <= slope <= 1.20:
        raise RuntimeError(
            "SysV shared-memory/SB slope %.6f is outside openGauss-compatible range"
            % slope
        )
    fixed_shared_mb = max(0.0, intercept)
    fixed_private_mb = max(private_values)
    database_fixed_mb = math.ceil(fixed_shared_mb + fixed_private_mb)
    observed_system_mb = max(system_values)
    system_other_reserve_mb = math.ceil(observed_system_mb + safety_margin_mb)
    host_mb = next(iter(hosts)) / 1024.0 ** 2
    tunable_pool_mb = host_mb - database_fixed_mb - system_other_reserve_mb
    if tunable_pool_mb <= 0:
        raise RuntimeError("measured fixed/reserve memory consumes the host")
    return {
        "schema": "huawei7.memory-budget/v1",
        "machine_fingerprint": machine, "memory_bytes": next(iter(hosts)),
        "host_mb": host_mb,
        "database_fixed_mb": database_fixed_mb,
        "system_other_reserve_mb": system_other_reserve_mb,
        "tunable_pool_mb": tunable_pool_mb,
        "safety_margin_mb": safety_margin_mb,
        "fit": {
            "sysv_mb_per_shared_buffer_mb": slope,
            "fixed_sysv_intercept_mb": fixed_shared_mb,
            "maximum_private_rss_mb": fixed_private_mb,
            "maximum_non_db_nonreclaimable_mb": observed_system_mb,
        },
        "method": (
            "Mpool=MemTotal-ceil(SysV_intercept+max_private_RSS)-"
            "ceil(max(MemTotal-MemAvailable-gaussdb_RSS)+safety_margin)"
        ),
        "snapshot_evidence": evidence, "valid": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = build_memory_budget(manifest, args.manifest.resolve().parent)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
