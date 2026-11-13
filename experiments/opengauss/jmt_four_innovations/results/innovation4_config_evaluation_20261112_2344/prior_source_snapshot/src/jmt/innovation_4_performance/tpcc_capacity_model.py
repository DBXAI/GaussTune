"""TPCC TP+AP concurrent TPS, model v3 (2026-10-16).

PPT chain with a capped cloud disk and an explicit MVCC baseline.

Inputs, all from windows that contain no TP+AP concurrency:
  H  TP + idle snapshot holder in the AP database (MVCC effect of a long AP
     snapshot at the same snapshot ages as B, no AP I/O or memory)
  C  AP only, AP started fresh (same AP ages as B)
  tp_stop  short idle gap (TP stopped, AP not started): memory baseline
plus the frozen device curves L_read(Q), L_write(Q) and one calibrated
constant ``pi_dirty`` (AP vs TP probability that a buffer victim is dirty).

1. Page cache in B:  cache_B = cache_H - AP memory (gaussdb anon in C minus
   the idle baseline).
2. TP storage reads per transaction r_B: two-level replay of the merged
   trace (TP accesses of H + AP accesses of C at relative rate rho) with
   page cache cache_B, bias of the replay on H carried over additively.
3. Saturated device (H at the cap, PPT fixed point with a capped g()):
       TPS_B = (X_dev,H - alpha * X_AP,C) / (ops_tx,H + (r_B - r_H) * kappa_H)
   ops_tx,H = device operations per transaction in H (reads, WAL,
   double-write and page-writer writes); kappa = device reads per TP read.
4. AP rate alpha (relative to C), per unit of AP work:
       T_B / T_C = [cpu_C + other_C + io_C * L_r,B / L_r,C + m_C * p_ap * W_dw,B] / n_ap
   p_ap = pi_dirty * p_tp,H (TP self-flushes per TP buffer victim in H),
   W_dw,B = W_dw,H * L_w,B / L_w,H (wait per self-flush on the serialized
   single-page double write), L_r,B and L_w,B from the device curves at the
   queue Q_B = Qw_H + X_read,B * L_r(Q_B).
   rho = alpha * TPS_H / TPS_B (AP accesses per TP access, relative to the
   H and C traces).
Unsaturated H (device utilisation < 0.95) uses the read/write-latency path
   TPS_B = N / (N/TPS_H + r_B L_r,B - r_H L_r,H + T_wal,H (L_w,B/L_w,H - 1));
this regime has not been validated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Dict

from jmt.innovation_4_performance.tpcc_dw_model import DeviceCurves, solve_device


@dataclass
class Inputs:
    terminals: int
    tps_h: float
    reads_per_tx_h: float          # TP storage reads (8 KiB) per tx in H
    kappa_h: float                 # device reads per TP storage read in H
    device_ops_h: float            # device reads + writes per second in H
    utilisation_h: float
    queue_write_h: float           # sum X_w * L_w in H
    read_ms_h: float
    write_ms_h: float
    wal_wait_ms_h: float           # TP WAL-flush wait per tx in H
    p_tp_dirty_h: float            # TP self-flushes per TP buffer victim in H
    w_dw_ms_h: float               # wait per self-flush on the single double write in H
    ap_threads: int
    ap_cpu_c: float                # threads on CPU in C
    ap_io_c: float                 # threads in block-IO wait in C
    ap_other_c: float
    ap_misses_c: float             # AP buffer misses per second in C
    ap_device_reads_c: float
    ap_device_ops_c: float         # AP reads + AP writes as device operations in C
    ap_read_ms_c: float
    pi_dirty: float


def solve(inputs: Inputs, curves: DeviceCurves, r_of_rho: Callable[[float], float],
          iterations: int = 300) -> Dict[str, float]:
    saturated = inputs.utilisation_h >= 0.95
    ops_tx_h = inputs.device_ops_h / inputs.tps_h
    p_ap = inputs.pi_dirty * inputs.p_tp_dirty_h
    tps, alpha = 0.8 * inputs.tps_h, 0.5
    state: Dict[str, float] = {}
    for _ in range(iterations):
        rho = min(1.0, max(1e-3, alpha * inputs.tps_h / tps))
        r_b = r_of_rho(rho)
        read_iops = tps * r_b * inputs.kappa_h + alpha * inputs.ap_device_reads_c
        q = solve_device(curves, inputs.queue_write_h, read_iops)
        lr, lw = curves.read_ms(q), curves.write_ms(q)
        if saturated:
            ops_tx = ops_tx_h + (r_b - inputs.reads_per_tx_h) * inputs.kappa_h
            new_tps = (inputs.device_ops_h - alpha * inputs.ap_device_ops_c) / ops_tx
        else:
            ops_tx = float("nan")
            t_tx = (inputs.terminals * 1000.0 / inputs.tps_h + r_b * lr
                    - inputs.reads_per_tx_h * inputs.read_ms_h
                    + inputs.wal_wait_ms_h * (lw / inputs.write_ms_h - 1.0))
            new_tps = inputs.terminals * 1000.0 / t_tx
        w_dw = inputs.w_dw_ms_h * lw / inputs.write_ms_h
        dw_units = inputs.ap_misses_c * p_ap * w_dw / 1000.0
        t_ratio = (inputs.ap_cpu_c + inputs.ap_other_c + inputs.ap_io_c * lr / inputs.ap_read_ms_c
                   + dw_units) / inputs.ap_threads
        new_alpha = min(1.0, 1.0 / t_ratio)
        tps = 0.7 * tps + 0.3 * new_tps
        alpha = 0.7 * alpha + 0.3 * new_alpha
        state = {"tps": tps, "alpha": alpha, "rho": rho, "reads_per_tx": r_b, "queue": q,
                 "read_ms": lr, "write_ms": lw, "ops_per_tx": ops_tx, "ap_device_ops": alpha * inputs.ap_device_ops_c,
                 "w_dw_ms": w_dw, "ap_dw_threads_per_unit": dw_units, "p_ap_dirty": p_ap,
                 "regime": "capacity" if saturated else "latency"}
    return state


def inputs_dict(inputs: Inputs) -> Dict[str, float]:
    return asdict(inputs)
