"""Rate-capped device model for provisioned cloud block storage (2026-10-15).

The 2026-08 fio surface (QD 0-4) never reached the device cap.  On this host
(Alibaba Cloud ESSD, 350 GiB) the fio saturation run of 2026-10-15 measured:
  * ~6000 operations/s shared by reads and writes (8 KiB read, 8 KiB write
    and a 1:2 read/write mix all give 5995-6005 device IOPS);
  * ~190 MB/s bandwidth (128 KiB sequential read);
  * above the cap, latency = outstanding / cap (QD 16/40/64 -> 2.72/6.80/
    10.87 ms at ~5880 IOPS: Little's law at a fixed service rate);
  * mixed classes share the cap in proportion to their outstanding requests
    (TP QD 40 + AP QD 8: AP 15.3 % measured vs 8/48 = 16.7 %;
     TP QD 40 + AP QD 2: AP 4.1 % vs 2/42 = 4.8 %).

So the device is one server with a fixed rate; every request sees the same
wait, and a closed-loop class keeping Q requests outstanding receives
cap * Q / sum(Q).  No parameter is fitted: the caps come from fio.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping


@dataclass(frozen=True)
class DeviceCap:
    iops: float
    mb_s: float
    unloaded_read_ms: float   # QD1 8 KiB random read

    @classmethod
    def from_saturation(cls, document: Mapping[str, object]) -> "DeviceCap":
        points = {p["point"]: p for p in document["points"]}  # type: ignore[index]

        def total(name: str) -> float:
            run = points[name]["device_whole_run"]  # type: ignore[index]
            return float(run["read_iops"]) + float(run["write_iops"])

        iops = sorted(total(n) for n in ("r8_qd16", "r8_qd40", "r8_qd64", "w8_qd40",
                                         "rw8_33r_qd40"))[2]   # median of 5
        run = points["s128_qd8"]["device_whole_run"]  # type: ignore[index]
        mb_s = float(run["read_mb_s"]) + float(run["write_mb_s"])
        unloaded = float(points["r8_qd1"]["jobs"]["tp"]["read"]["lat_ms"])  # type: ignore[index]
        return cls(iops=iops, mb_s=mb_s, unloaded_read_ms=unloaded)


def utilisation(cap: DeviceCap, iops: float, mb_s: float) -> float:
    return max(iops / cap.iops, mb_s / cap.mb_s)


def saturated_shares(cap: DeviceCap, outstanding: Mapping[str, float]) -> Dict[str, float]:
    """Operations/s granted to each closed-loop class at the cap."""

    total = sum(max(0.0, float(q)) for q in outstanding.values())
    if total <= 0:
        raise ValueError("no outstanding requests")
    return {name: cap.iops * max(0.0, float(q)) / total for name, q in outstanding.items()}


def latency_ms(cap: DeviceCap, outstanding_total: float) -> float:
    """Mean request latency at the cap (Little's law), never below unloaded."""

    return max(cap.unloaded_read_ms, outstanding_total / cap.iops * 1000.0)
