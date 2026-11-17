"""TP/AP concurrent TPS model for write-saturated TPCC (2026-10-15).

Chain (all inputs come from the TP-only window A, the AP-only window C, and
one-time device calibration; the concurrent window B is never read):

1. Page cache in B:   cache_B = cache_A - (AP anonymous memory measured in C)
2. TP storage reads per transaction in B (r_B): PPT two-level replay of a
   merged trace (TP accesses of A + AP accesses of C, each time-scaled to its
   B rate), page cache = cache_B, plus never-reused page-cache inflow from WAL
   and AP temp-file writes.  Bias of the replay on A is carried over:
   r_B = r_A(measured) + replay_B - replay_A.
3. Device (capped cloud disk, saturated): outstanding requests
   Q = Qw_A + X_read * L_read(Q) / 1000,
   X_read = TPS * r_B * kappa_A + AP device reads;
   L_read(Q), L_write(Q) are affine curves calibrated on TP-only phases.
4. Session self-flush through the serialized single-page double write:
   flush throughput = K / L_write  (K measured in A), so
   TPS = (K / L_write - AP flushes) / f_A.
5. AP rate in B (alpha, relative to C): time per AP work unit
   T_B = cpu_C + io_C * L_read,B / L_read,C + p_ap_flush * W_dw,B,
   W_dw,B = TP threads queued on the double write / flush throughput.
Steps 2-5 are solved together by fixed-point iteration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Mapping, Optional

import numpy as np

TP_DB, AP_DB = "h5_tpcc_bench", "h5_tpch"
PAGE = 8192


def load_samples(run_dir: Path):
    return [json.loads(line) for line in (run_dir / "samples.jsonl").read_text().splitlines()
            if line.strip() and '"error"' not in line]


def window(rows, start_ns: int, end_ns: int):
    selected = [r for r in rows if start_ns <= r["t_ns"] <= end_ns]
    if len(selected) < 20:
        raise ValueError("window has only %d samples" % len(selected))
    return selected


@dataclass
class WindowStats:
    seconds: float
    tps: float
    device_read_iops: float
    device_write_iops: float
    read_ms: float
    write_ms: float
    queue: float          # sum X*L over reads and writes (Little)
    queue_write: float
    tp_read_8k_per_s: float
    tp_write_bytes_per_s: float
    wal_write_bytes_per_s: float
    ap_read_bytes_per_s: float
    ap_write_bytes_per_s: float
    file_cache_mb: float
    gaussdb_anon_mb: float
    tp_threads_by_kernel_category: Dict[str, float]
    ap_threads_by_kernel_category: Dict[str, float]


def window_stats(rows, start_ns: int, end_ns: int, categorise=None) -> WindowStats:
    w = window(rows, start_ns, end_ns)
    a, b = w[0], w[-1]
    s = (b["t_ns"] - a["t_ns"]) / 1e9
    d = [y - x for x, y in zip(a["dev"], b["dev"])]
    commits = b["db"][TP_DB]["xact_commit"] - a["db"][TP_DB]["xact_commit"]

    def thread_sum(label: str, index: int, name: Optional[str] = None) -> float:
        total = 0
        for tid, row in b["threads"].items():
            if tid in a["threads"] and row[0] == label and (name is None or row[1] == name):
                total += row[index] - a["threads"][tid][index]
        return total / s

    xr, xw = d[0] / s, d[4] / s
    lr = d[3] / max(1, d[0])
    lw = d[7] / max(1, d[4])
    tp_cat: Dict[str, float] = {}
    ap_cat: Dict[str, float] = {}
    if categorise is not None and "kwaits" in a:
        from collections import Counter
        counts = {"tp": Counter(), "ap": Counter()}
        for row in w:
            for key, n in row["kwaits"].items():
                label, status, event, state, wchan, syscall = key.split("|")
                counts[label][categorise(status, event, state, wchan, syscall)] += n
        tp_cat = {k: v / len(w) for k, v in counts["tp"].items()}
        ap_cat = {k: v / len(w) for k, v in counts["ap"].items()}
    return WindowStats(
        seconds=s, tps=commits / s, device_read_iops=xr, device_write_iops=xw,
        read_ms=lr, write_ms=lw, queue=(xr * lr + xw * lw) / 1000, queue_write=xw * lw / 1000,
        tp_read_8k_per_s=thread_sum("tp", 2) / PAGE, tp_write_bytes_per_s=thread_sum("tp", 3),
        wal_write_bytes_per_s=thread_sum("bg", 3, "Wal Writer"),
        ap_read_bytes_per_s=thread_sum("ap", 2), ap_write_bytes_per_s=thread_sum("ap", 5),
        file_cache_mb=float(np.mean([r["mem"]["Active(file)"] + r["mem"]["Inactive(file)"] for r in w])) / 1024,
        gaussdb_anon_mb=float(np.mean([r["rss"]["RssAnon"] for r in w])) / 1024,
        tp_threads_by_kernel_category=tp_cat, ap_threads_by_kernel_category=ap_cat)


@dataclass(frozen=True)
class DeviceCurves:
    write_a: float
    write_b: float
    read_a: float
    read_b: float

    def write_ms(self, q: float) -> float:
        return self.write_a + self.write_b * q

    def read_ms(self, q: float) -> float:
        return self.read_a + self.read_b * q


def fit_device_curves(points) -> DeviceCurves:
    """Affine L(Q) fits on TP-only phases (points: iterable of WindowStats)."""

    q = np.array([p.queue for p in points])
    bw, aw = np.polyfit(q, np.array([p.write_ms for p in points]), 1)
    br, ar = np.polyfit(q, np.array([p.read_ms for p in points]), 1)
    return DeviceCurves(float(aw), float(bw), float(ar), float(br))


def solve_device(curves: DeviceCurves, queue_write: float, read_iops: float) -> float:
    """Q = Qw + Xr * Lr(Q) / 1000, Lr affine -> closed form."""

    denominator = 1.0 - read_iops * curves.read_b / 1000.0
    if denominator <= 0.05:
        raise ValueError("device read load beyond the calibrated region")
    return (queue_write + read_iops * curves.read_a / 1000.0) / denominator


def asdict_stats(stats: WindowStats) -> Dict[str, object]:
    return asdict(stats)


# ---------------------------------------------------------------------------
# Trace side: session self-flushes and the merged TP(A) + AP(C) replay.


def session_flush_rate(records: np.ndarray, start_ns: int, end_ns: int, ppm: int) -> float:
    """Self-flushes per second by session threads (FLUSH events of threads that issued ACCESS)."""

    win = records[(records["start_ns"] >= start_ns) & (records["start_ns"] < end_ns)]
    sessions = np.unique(win["tid"][win["kind"] == 1])
    flushes = win[(win["kind"] == 7) & np.isin(win["tid"], sessions)]
    return len(flushes) / (ppm / 1e6) / ((end_ns - start_ns) / 1e9)


def merge_tp_ap(tp_records: np.ndarray, tp_trace_start: int, ap_records: np.ndarray,
                ap_trace_start: int, ap_oid: int, rho: float, tp_end: int) -> np.ndarray:
    """TP window trace (all threads) + AP session events of the AP-only trace.

    AP timestamps are mapped onto the TP time base and stretched by 1/rho, so
    the AP access rate relative to TP is rho times its AP-only rate per TP-
    window second.  Raises if the stretched AP trace does not cover the TP span.
    """

    if not 0 < rho <= 1:
        raise ValueError("rho must be in (0, 1]")
    access = ap_records[ap_records["kind"] == 1]
    ap_threads = np.unique(access["tid"][access["db_node"] == ap_oid])
    ap = ap_records[np.isin(ap_records["tid"], ap_threads)].copy()
    span = (int(ap["start_ns"].max()) - ap_trace_start) / rho
    if tp_trace_start + span < tp_end:
        raise ValueError("stretched AP trace (%.0fs) shorter than the TP span" % (span / 1e9))
    for field in ("start_ns", "end_ns"):
        values = ap[field].astype(np.int64)
        nonzero = values > 0
        values[nonzero] = tp_trace_start + ((values[nonzero] - ap_trace_start) / rho).astype(np.int64)
        ap[field] = values.astype(ap[field].dtype)
    if np.intersect1d(np.unique(tp_records["tid"]), ap_threads).size:
        raise ValueError("thread ids of the TP and AP traces overlap")
    merged = np.concatenate([tp_records, ap])
    return merged[np.argsort(merged["start_ns"], kind="stable")]


def replay_tp_reads(records: np.ndarray, *, stream: Path, oids: Mapping[str, int], classes,
                    ppm: int, warm_end: int, end: int, sb_mb: int, startup_mb: int,
                    cache_mb: float, inflow_pages_per_s: float, resident_sb, resident_os,
                    session_threads, tp_transactions: float) -> Dict[str, float]:
    from . import kernel_trace as kt
    from jmt.innovation_1_cache.trace import normalize_path_stream

    nodes = tuple(int(oids[c]) for c in classes)
    kt.write_probe_stream(records, target_db_node=nodes[0], path=stream, db_nodes=nodes)
    attribution = kt.ThreadClassAttribution(records, {int(oids[c]): c for c in classes})
    result = kt.replay_two_level_miniature(
        normalize_path_stream(stream, warmup_end_ns=warm_end, measure_end_ns=end, attribution=attribution),
        shared_buffer_pages=sb_mb * 128, os_cache_pages=int(max(0.0, cache_mb) * 128), ppm=ppm,
        startup_shared_buffer_pages=startup_mb * 128, candidate_lists=4,
        resident_sb=resident_sb, resident_os=resident_os,
        synthetic_os_pages_per_second=inflow_pages_per_s, session_threads=session_threads)
    stream.write_bytes(b"")
    stream.unlink()
    tp = result["classes"]["tp"]
    rate = ppm / 1e6
    return {"tp_disk_reads_per_tx": tp["disk"] / rate / tp_transactions,
            "tp_sb_misses_per_tx": (tp["accesses"] - tp["sb_hits"]) / rate / tp_transactions,
            "anomalies": int(result["measured_state_anomalies"]),
            "ap_accesses_per_s_tp_time": (result["classes"].get("ap", {}).get("accesses", 0)
                                          / rate / ((end - warm_end) / 1e9))}


# ---------------------------------------------------------------------------
# Fixed point.


@dataclass
class Inputs:
    terminals: int
    tps_a: float
    reads_per_tx_a: float          # TP storage reads (8 KiB) per tx, measured in A
    device_reads_per_tp_read: float   # kappa_A
    queue_write_a: float
    flush_rate_a: float            # session self-flushes / s in A
    write_ms_a: float
    ap_device_reads_c: float
    ap_read_ms_c: float
    ap_threads: int
    ap_io_threads_c: float         # delayacct block-IO threads in C
    ap_cpu_threads_c: float


def solve(inputs: Inputs, curves: DeviceCurves, r_of_rho, *, iterations: int = 200) -> Dict[str, float]:
    """Joint fixed point of TPS_B, AP rate alpha and TP reads r_B(rho)."""

    k = inputs.flush_rate_a * inputs.write_ms_a / 1000.0
    f = inputs.flush_rate_a / inputs.tps_a
    other = max(0.0, inputs.ap_threads - inputs.ap_io_threads_c - inputs.ap_cpu_threads_c)
    tps, alpha = 0.8 * inputs.tps_a, 0.5
    for _ in range(iterations):
        rho = min(1.0, max(1e-3, alpha * inputs.tps_a / tps))
        r = r_of_rho(rho)
        read_iops = tps * r * inputs.device_reads_per_tp_read + alpha * inputs.ap_device_reads_c
        q = solve_device(curves, inputs.queue_write_a, read_iops)
        lw, lr = curves.write_ms(q), curves.read_ms(q)
        new_tps = k / (f * lw / 1000.0)
        t_ratio = (inputs.ap_cpu_threads_c + other
                   + inputs.ap_io_threads_c * lr / inputs.ap_read_ms_c) / inputs.ap_threads
        new_alpha = min(1.0, 1.0 / t_ratio)
        tps = 0.5 * tps + 0.5 * new_tps
        alpha = 0.5 * alpha + 0.5 * new_alpha
    return {"tps": tps, "alpha": alpha, "rho": rho, "reads_per_tx": r, "queue": q,
            "write_ms": lw, "read_ms": lr, "K": k, "f": f, "read_iops": read_iops}
