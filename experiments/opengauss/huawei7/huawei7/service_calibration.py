"""Compile four-class block service times from repeated real calibrations."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Dict, Mapping, Sequence

from .provenance import sha256
from .fio_surface import _latency_ms


CLASSES = {
    "tp_read_ms": ("tp", "R"), "tp_write_ms": ("tp", "W"),
    "ap_read_ms": ("ap", "R"), "ap_write_ms": ("ap", "W"),
}


def validate_service_time_evidence(document: Mapping[str, object]) -> None:
    """Reparse all four raw classes and recompute their medians."""

    if document.get("schema") != "huawei7.service-times/v2":
        raise ValueError("service-time evidence is not raw-bound v2")
    machine = str(document.get("machine_fingerprint", ""))
    raw = document.get("source_artifacts")
    values = document.get("service_times_ms")
    if not isinstance(raw, list) or not isinstance(values, dict):
        raise ValueError("service-time evidence lacks raw sources or values")
    samples = {name: [] for name in CLASSES}
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("invalid service-time source row")
        name = str(row.get("service_class", ""))
        if name not in samples:
            raise ValueError("unknown service-time class")
        path = Path(str(row.get("path", "")))
        if not path.is_file() or sha256(path) != row.get("sha256"):
            raise ValueError("service-time raw source is missing or changed")
        source = json.loads(path.read_text(encoding="utf-8"))
        if row.get("kind") == "block_calibration":
            workload_class, direction = CLASSES[name]
            if (
                source.get("schema") != "huawei7.block-calibration/v1"
                or source.get("machine_fingerprint") != machine
                or source.get("required_class") != workload_class
                or source.get("valid") is not True
            ):
                raise ValueError("invalid block service calibration source")
            matched = [
                item for item in source.get("summary", {}).get("rows", [])
                if item.get("workload_class") == workload_class
                and item.get("rw") == direction and int(item.get("requests", 0)) > 0
            ]
            if len(matched) != 1:
                raise ValueError("block service calibration class is ambiguous")
            samples[name].append(float(matched[0]["service_time_ms"]))
        elif row.get("kind") == "fio_raw":
            job = source["jobs"][0]
            direction = "read" if name.endswith("read_ms") else "write"
            directional = job[direction]
            if int(directional["total_ios"]) <= 0:
                raise ValueError("fio service source contains no I/O")
            samples[name].append(_latency_ms(
                job if direction == "read" else {"read": directional},
            ))
        else:
            raise ValueError("unsupported service-time raw source kind")
    for name, rows in samples.items():
        measured = statistics.median(rows) if rows else -1.0
        reported = float(values.get(name, -1))
        if len(rows) < 3 or abs(measured - reported) > max(1e-12, abs(measured) * 1e-12):
            raise ValueError("service-time median differs from raw class %s" % name)


def build_service_times(
    manifest: Mapping[str, object], base: Path,
) -> Dict[str, object]:
    if manifest.get("schema") != "huawei7.service-time-manifest/v1":
        raise ValueError("unsupported service-time manifest")
    machine = str(manifest.get("machine_fingerprint", ""))
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("service-time inputs must be an object")
    values = {}
    evidence = {}
    source_artifacts = []
    for output_name, (workload_class, direction) in CLASSES.items():
        paths = inputs.get(output_name)
        if not isinstance(paths, list) or len(paths) < 3:
            raise ValueError("%s needs at least three block calibrations" % output_name)
        samples = []
        hashes = []
        for raw_path in paths:
            path = Path(str(raw_path))
            if not path.is_absolute():
                path = base / path
            document = json.loads(path.read_text(encoding="utf-8"))
            if (
                document.get("schema") != "huawei7.block-calibration/v1"
                or document.get("machine_fingerprint") != machine
                or document.get("valid") is not True
                or document.get("required_class") != workload_class
            ):
                raise ValueError("invalid or mismatched block calibration: %s" % path)
            rows = document.get("summary", {}).get("rows", [])
            matched = [row for row in rows
                       if row.get("workload_class") == workload_class
                       and row.get("rw") == direction and int(row.get("requests", 0)) > 0]
            if len(matched) != 1:
                raise ValueError("%s lacks exactly one %s/%s row" % (
                    path, workload_class, direction,
                ))
            service = float(matched[0]["service_time_ms"])
            if service <= 0:
                raise ValueError("measured service time must be positive")
            samples.append(service)
            hashes.append(sha256(path))
            source_artifacts.append({
                "kind": "block_calibration", "service_class": output_name,
                "path": str(path.resolve()), "sha256": sha256(path),
            })
        values[output_name] = statistics.median(samples)
        evidence[output_name] = {
            "repeats": len(samples), "samples_ms": samples,
            "evidence_id": hashlib.sha256("".join(sorted(hashes)).encode("ascii")).hexdigest(),
        }
    return {
        "schema": "huawei7.service-times/v2",
        "machine_fingerprint": machine, "service_times_ms": values,
        "evidence": evidence, "source_artifacts": source_artifacts,
        "valid": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = build_service_times(manifest, args.manifest.resolve().parent)
    result["manifest_artifact"] = {
        "path": str(args.manifest.resolve()), "sha256": sha256(args.manifest),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
