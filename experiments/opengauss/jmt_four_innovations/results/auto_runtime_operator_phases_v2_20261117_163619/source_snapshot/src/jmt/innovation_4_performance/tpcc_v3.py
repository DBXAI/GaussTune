"""TPCC TP+AP concurrent TPS, model v3b (2026-10-16).

PPT chain (page 20/32: TPS = N / L, L driven by the disk path) for a
saturated, rate-capped cloud disk, with the MVCC baseline measured directly.

Measured inputs (no TP+AP concurrency):
  H  TP + idle snapshot holder in the AP database.  Per second: commits,
     device reads/writes and latencies, TP storage reads, session
     self-flushes (trace), cleanup-horizon lag (xids since the holder).
  C  AP only, started fresh: AP device operations, AP thread time split
     (CPU / block-IO / other), AP buffer victims (trace), AP memory.
Frozen constants: device cap, device curves L_r(Q), L_w(Q), omega (device
writes per session self-flush: single double-write + data page), pi_dirty
(AP / TP probability that a buffer victim must be self-flushed).

Unknowns TPS_B and alpha (AP work rate in B relative to C) solve:
  lag range of B   = xi * TPS_B * [age0, age1]   (xi = xids per commit)
  H(lag)           = H seconds whose horizon lag is in that range
  r_B              = r_H + replay(TP of H + AP of C at rho, cache_B) - replay(H, cache_H)
  X_r              = TPS_B * kappa_H * r_B + alpha * X_ap_read,C
  Q                = [(cap - X_r) L_w(Q) + X_r L_r(Q)] / 1000          (device saturated)
  X_sf             = K_H / L_w(Q)            (serialized single double write)
  TPS_B            = (cap - omega X_sf - alpha X_ap,C) / (kappa_H r_B + w'_H)
       w'_H = device writes per tx in H excluding omega * self-flushes
  alpha            = n_ap / [cpu_C + other_C + io_C L_r/L_r,C + V_C pi p_H W_dw]
       V_C = AP buffer victims/s in C, p_H = TP self-flushes per TP victim,
       W_dw = W_dw,H * L_w / L_w,H (wait per self-flush on the double write)
  rho              = alpha * TPS_H / TPS_B
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List

import numpy as np

from . import kernel_trace as kt
from jmt.innovation_4_performance.tpcc_dw_model import DeviceCurves

TP_DB = "h5_tpcc_bench"


def horizon_series(run_dir: Path):
    rows = [json.loads(l) for l in (run_dir / "dbstats.jsonl").read_text().splitlines() if l.strip()]
    rows = [r for r in rows if "error" not in r]
    t = np.array([r["t_ns"] for r in rows], dtype=float)
    xmin = np.array([int(r["snapshot"].split(":")[0]) for r in rows], dtype=float)
    lag = xmin - np.array([r["oldest_xmin"] for r in rows], dtype=float)
    return t, xmin, lag


def step_value(t_db: np.ndarray, values: np.ndarray, t: float) -> float:
    """Last observation at or before t (no interpolation across a horizon release)."""

    index = int(np.searchsorted(t_db, t, side="right")) - 1
    return float(values[max(0, index)])


def per_second(run_dir: Path, run, rows, window: str, start_ns: int, end_ns: int) -> List[Dict[str, float]]:
    t_db, xmin_db, lag_db = horizon_series(run_dir)
    oids = run["database_oids"]
    rate = run["sample_ppm"] / 1e6
    rec, _ = kt.read_epoch(Path(run["trace_store"]), run[window]["epoch"])
    win = rec[(rec["start_ns"] >= start_ns) & (rec["start_ns"] < end_ns)]
    acc = win[win["kind"] == 1]
    tp = np.unique(acc["tid"][acc["db_node"] == oids["tp"]])
    ap = np.unique(acc["tid"][acc["db_node"] == oids["ap"]])
    fl = win[win["kind"] == 7]
    vp = win[win["kind"] == 3]
    times = {"sf_tp": fl["start_ns"][np.isin(fl["tid"], tp)], "sf_ap": fl["start_ns"][np.isin(fl["tid"], ap)],
             "v_tp": vp["start_ns"][np.isin(vp["tid"], tp)], "v_ap": vp["start_ns"][np.isin(vp["tid"], ap)]}
    times = {k: np.sort(v.astype(np.int64)) for k, v in times.items()}
    del rec, win, acc, fl, vp
    w = [r for r in rows if start_ns <= r["t_ns"] <= end_ns]
    out = []
    for a, b in zip(w[:-1], w[1:]):
        com = b["db"][TP_DB]["xact_commit"] - a["db"][TP_DB]["xact_commit"]
        dv = [y - x for x, y in zip(a["dev"], b["dev"])]
        tpr = sum(b["threads"][t][2] - a["threads"][t][2] for t, v in b["threads"].items()
                  if v[0] == "tp" and t in a["threads"]) / 8192
        row = dict(t=b["t_ns"], lag=step_value(t_db, lag_db, a["t_ns"]),
                   xmin=step_value(t_db, xmin_db, a["t_ns"]), sec=(b["t_ns"] - a["t_ns"]) / 1e9,
                   com=com, dr=dv[0], dw=dv[4], dr_ms=dv[3], dw_ms=dv[7], tpr=tpr)
        for k, v in times.items():
            row[k] = (np.searchsorted(v, b["t_ns"]) - np.searchsorted(v, a["t_ns"])) / rate
        out.append(row)
    return out


@dataclass
class HAggregate:
    seconds: int
    tps: float
    r: float                 # TP storage reads (8 KiB) per tx
    kappa: float             # device reads per TP storage read
    writes_tx: float
    w_prime: float           # device writes per tx excluding omega * self-flushes
    k_dw: float              # self-flushes/s * L_w (s)
    lw: float
    lr: float
    p_dirty: float           # TP self-flushes per TP victim
    lag_lo: float
    lag_hi: float


def aggregate(rows: List[Dict[str, float]], omega: float) -> HAggregate:
    s = {k: sum(x[k] for x in rows) for k in ("sec", "com", "dr", "dw", "dr_ms", "dw_ms", "tpr", "sf_tp", "sf_ap", "v_tp")}
    lw = s["dw_ms"] / max(1.0, s["dw"])
    x_sf = (s["sf_tp"] + s["sf_ap"]) / s["sec"]
    return HAggregate(
        seconds=len(rows), tps=s["com"] / s["sec"], r=s["tpr"] / s["com"], kappa=s["dr"] / max(1.0, s["tpr"]),
        writes_tx=s["dw"] / s["com"], w_prime=(s["dw"] - omega * (s["sf_tp"] + s["sf_ap"])) / s["com"],
        k_dw=x_sf * lw / 1000.0, lw=lw, lr=s["dr_ms"] / max(1.0, s["dr"]),
        p_dirty=s["sf_tp"] / max(1.0, s["v_tp"]),
        lag_lo=min(x["lag"] for x in rows), lag_hi=max(x["lag"] for x in rows))


def h_at(rows: List[Dict[str, float]], lo: float, hi: float, omega: float, min_seconds: int = 60) -> HAggregate:
    """H aggregate over seconds with lag in [lo, hi]; widened symmetrically if too few."""

    ordered = sorted(rows, key=lambda x: x["lag"])
    sel = [x for x in ordered if lo <= x["lag"] <= hi]
    if len(sel) < min_seconds:
        centre = 0.5 * (lo + hi)
        sel = sorted(ordered, key=lambda x: abs(x["lag"] - centre))[:min_seconds]
    return aggregate(sel, omega)


def device_queue(curves: DeviceCurves, cap: float, read_iops: float) -> float:
    xw = max(0.0, cap - read_iops)
    num = (xw * curves.write_a + read_iops * curves.read_a) / 1000.0
    den = 1.0 - (xw * curves.write_b + read_iops * curves.read_b) / 1000.0
    if den <= 0.05:
        raise ValueError("device outside calibrated region")
    return num / den


@dataclass
class ApInputs:
    threads: int
    cpu_c: float
    io_c: float
    other_c: float
    victims_c: float
    device_reads_c: float
    device_ops_c: float
    read_ms_c: float
    w_dw_ms_h: float
    lw_h: float
    lr_h: float = 0.0          # device read latency over the whole H window


def ap_memory_mb(mem_curve: np.ndarray, alpha: float, ages: tuple, steps: int = 50) -> float:
    """Mean AP memory over B: AP memory of C at the same AP progress (age * alpha).

    mem_curve: array of (age_s, mb) from the AP-only run, AP started fresh.
    """

    t = np.linspace(ages[0], ages[1], steps)
    return float(np.mean(np.interp(alpha * t, mem_curve[:, 0], mem_curve[:, 1])))


def solve(h_rows: List[Dict[str, float]], ap: ApInputs, curves: DeviceCurves, *, cap: float, omega: float,
          pi_dirty: float, xi: float, ages: tuple, tps_ref: float, cache_h_mb: float, mem_curve: np.ndarray,
          delta_r: Callable[[float, float], float], iterations: int = 400) -> Dict[str, float]:
    tps, alpha = 0.8 * tps_ref, 0.3
    state: Dict[str, float] = {}
    for _ in range(iterations):
        lo, hi = xi * tps * ages[0], xi * tps * ages[1]
        h = h_at(h_rows, lo, hi, omega)
        rho = min(1.0, max(1e-3, alpha * tps_ref / tps))
        ap_mem = ap_memory_mb(mem_curve, alpha, ages)
        cache_b = cache_h_mb - ap_mem
        r_b = max(h.r, h.r + delta_r(rho, cache_b))
        # Reads added by the smaller page cache are random single-page misses:
        # one device read each (no readahead merging, unlike part of r_H).
        tp_dev_reads_tx = h.kappa * h.r + (r_b - h.r)
        read_iops = tps * tp_dev_reads_tx + alpha * ap.device_reads_c
        q = device_queue(curves, cap, read_iops)
        lw, lr = curves.write_ms(q), curves.read_ms(q)
        # The serialized single-page double write issues synchronous single-
        # page O_SYNC|O_DIRECT requests (pwrite page, pread + pwrite tag page,
        # single_double_write.cpp:755-772); on the rate-capped device these
        # wait like reads, so the self-flush service time scales with L_read.
        x_sf_h = h.k_dw * 1000.0 / h.lw
        x_sf = x_sf_h * h.lr / lr
        new_tps = (cap - omega * x_sf - alpha * ap.device_ops_c) / (tp_dev_reads_tx + h.w_prime)
        w_dw = ap.w_dw_ms_h * lr / (ap.lr_h or h.lr)
        dw_units = ap.victims_c * pi_dirty * h.p_dirty * w_dw / 1000.0
        t_units = ap.cpu_c + ap.other_c + ap.io_c * lr / ap.read_ms_c + dw_units
        new_alpha = min(1.0, ap.threads / t_units)
        tps = 0.7 * tps + 0.3 * max(1.0, new_tps)
        alpha = 0.7 * alpha + 0.3 * new_alpha
        state = {"tps": tps, "alpha": alpha, "rho": rho, "r_b": r_b, "queue": q, "write_ms": lw, "read_ms": lr,
                 "ap_memory_mb": ap_mem, "cache_b_mb": cache_b,
                 "x_selfflush": x_sf, "w_dw_ms": w_dw, "ap_dw_units": dw_units, "lag_lo": lo, "lag_hi": hi,
                 "h": asdict(h)}
    return state


def ap_inputs_dict(ap: ApInputs) -> Dict[str, float]:
    return asdict(ap)
