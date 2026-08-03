#!/usr/bin/env python3
"""Turn request-level block traces into TP/AP I/O-rate windows.

Unknown kernel threads remain explicitly labelled ``other``.  They are kept
in total device load but are never silently reassigned to TP or AP.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path


def classify(application_name: str) -> str:
    if application_name.startswith("sysbench_tp"):
        return "tp"
    if application_name.startswith("ppt5_ap"):
        return "ap"
    return "other"


def parse_aggregate_trace(path: Path, started_ns: int, mapping: dict[int, str]) -> dict[int, dict[str, float]]:
    """Parse bpftrace map dumps from the low-perturbation aggregate probe."""
    windows: dict[int, dict[str, dict[int, float]]] = {}
    current: int | None = None
    pattern = re.compile(r"^@(count|latency_us|bytes)\[(-?\d+)\]:\s+(\d+)$")
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("WINDOW,"):
            current = int((int(raw.split(",", 1)[1]) - started_ns) / 1_000_000_000)
            windows[current] = {"count": {}, "latency_us": {}, "bytes": {}}
            continue
        match = pattern.match(raw.strip())
        if current is None or match is None:
            continue
        metric, tid, value = match.groups()
        windows[current][metric][int(tid)] = float(value)
    buckets: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for second, metrics in windows.items():
        tids = set().union(*[set(values) for values in metrics.values()])
        for tid in tids:
            group = mapping.get(tid, "other")
            ops = metrics["count"].get(tid, 0.0)
            buckets[second][f"{group}_read_ops"] += ops
            buckets[second][f"{group}_read_bytes"] += metrics["bytes"].get(tid, 0.0)
            buckets[second][f"{group}_read_latency_us_sum"] += metrics["latency_us"].get(tid, 0.0)
    return buckets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    meta = json.loads((args.trace_dir / "block_request_latency_meta.json").read_text())
    mapping: dict[int, str] = {}
    mapping_path = args.trace_dir / "lwtid_application_map.csv"
    if mapping_path.exists():
        for row in csv.DictReader(mapping_path.open(newline="", encoding="utf-8")):
            mapping[int(row["lwtid"])] = row["class"]
    trace_path = args.trace_dir / "block_request_latency.csv"
    script_name = str(meta.get("script", ""))
    if "aggregate" in script_name:
        buckets = parse_aggregate_trace(trace_path, int(meta["started_monotonic_ns"]), mapping)
    else:
        buckets: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        with trace_path.open(encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw or raw.startswith("Attaching") or raw.startswith("complete_ns"):
                    continue
                fields = raw.split(",")
                if len(fields) != 5:
                    continue
                complete_ns, tid, byte_count, latency_us, rwbs = fields
                second = int((int(complete_ns) - int(meta["started_monotonic_ns"])) / 1_000_000_000)
                category = mapping.get(int(tid), "other")
                operation = "write" if "W" in rwbs else "read"
                row = buckets[second]
                row[f"{category}_{operation}_ops"] += 1
                row[f"{category}_{operation}_bytes"] += int(byte_count)
                row[f"{category}_{operation}_latency_us_sum"] += int(latency_us)
    fields = [
        "elapsed_second",
        *(
            f"{group}_{operation}_{metric}"
            for group in ("tp", "ap", "other")
            for operation in ("read", "write")
            for metric in ("ops", "bytes", "latency_us_sum", "await_ms")
        ),
        "total_ops", "total_await_ms",
    ]
    rows = []
    for second in sorted(buckets):
        source = buckets[second]
        result: dict[str, float | int] = {"elapsed_second": second}
        total_ops = 0.0
        total_latency = 0.0
        for group in ("tp", "ap", "other"):
            for operation in ("read", "write"):
                prefix = f"{group}_{operation}"
                ops = source[f"{prefix}_ops"]
                latency = source[f"{prefix}_latency_us_sum"]
                result[f"{prefix}_ops"] = int(ops)
                result[f"{prefix}_bytes"] = int(source[f"{prefix}_bytes"])
                result[f"{prefix}_latency_us_sum"] = int(latency)
                result[f"{prefix}_await_ms"] = round(latency / ops / 1000.0, 6) if ops else 0.0
                total_ops += ops
                total_latency += latency
        result["total_ops"] = int(total_ops)
        result["total_await_ms"] = round(total_latency / total_ops / 1000.0, 6) if total_ops else 0.0
        rows.append(result)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
