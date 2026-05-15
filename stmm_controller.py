#!/usr/bin/env python3
"""
STMM (Self-Tuning Memory Manager) for OpenGauss — adapted from DB2 STMM
Paper: "Adaptive Self-Tuning Memory in DB2" (VLDB 2006)
       Storm, Garcia-Arellano, Lightstone, Diao, Surendra

Core algorithm (Section 3):
  1. Cost-benefit analysis: common metric = saved system time / MB
       - shared_buffers:  saved disk-read seconds per MB from SBPX simulation
       - work_mem (sort): saved sort-spill I/O seconds per MB
  2. MIMO control (Section 3.2.2):
       - Model benefit_i(k) = slope_i × size_i(k) + offset_i via least-squares
       - Integral control law:
           gain_i    = (p-1) / slope_i      [p=0.8 → converge in ~18 intervals]
           size_i(k) = size_i(k-1) + gain_i × (benefit_i(k) - avg_benefit(k))
  3. Oscillation Dampening (OD): fixed 10% step when MIMO model unavailable
  4. Greedy transfer: take from lowest-cost donor, give to highest-benefit consumer
  5. Limits: maxInc = 0.5×size, maxDec = 0.2×size; minResize = 0.5% of size

Adaptations for OpenGauss:
  - work_mem: online-adjustable (ALTER SYSTEM + pg_reload_conf) → "fast consumer"
  - shared_buffers: requires restart → "slow consumer", adjusted only when
    benefit gap persists ≥ SB_TRIGGER_INTERVALS and DB can be safely restarted
  - Benefit proxies (no DB internal access):
      WM benefit  = temp_bytes_spilled × DISK_COST_S_PER_BYTE / wm_mb
      SB benefit  = blks_read × PAGE_READ_COST_S / sb_mb
"""

from __future__ import annotations
import math
import numpy as np
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from memory_tuner import SBPenaltyModel

# ── Paper constants ────────────────────────────────────────────────────────────
POLE           = 0.8   # p; convergence in -4/ln(p) ≈ 18 intervals
N_WINDOW       = 40    # sliding-window size for benefit_slope regression
MAX_INC_RATIO  = 0.5   # maxInc = 50% of current size (DB2 Section 3.3.1)
MAX_DEC_RATIO  = 0.2   # maxDec = 20% of current size
MIN_RESIZE_PCT = 0.005 # ignore resizes < 0.5% of current size

# ── Disk cost assumptions ─────────────────────────────────────────────────────
DISK_WRITE_MB_S  = 100.0   # MB/s for temp-file writes (sort spill)
DISK_READ_MB_S   = 100.0   # MB/s for buffer pool page reads
PAGE_SIZE_KB     = 8       # OpenGauss default page size

DISK_WRITE_COST_S_PER_MB = 1.0 / DISK_WRITE_MB_S   # seconds per MB of spill
DISK_READ_COST_S_PER_MB  = 1.0 / DISK_READ_MB_S    # seconds per MB of page reads

# ── Slow-consumer (SB) trigger ────────────────────────────────────────────────
SB_INIT_MB           = 6144   # baseline SB (Phase A sweet spot for TP-only)
SB_TRIGGER_INTERVALS = 4      # grow after 4 intervals of AP+spill (4×15s = 60s)
SB_STEP_MB           = 1024   # grow/shrink SB by 1 GB per suggestion
SB_MIN_MB            = 6144   # never shrink below baseline
SB_MAX_MB            = 8000   # safe ceiling: RAM(14700) - 4×WM(4096) - OS(2048) - overhead(512)
SB_HIT_RATIO_MIN     = 0.95   # if TP cache hit ratio < this, SB too small for working set

# ── WM bounds ─────────────────────────────────────────────────────────────────
WM_MIN_MB      = 64
WM_MAX_MB      = 1024  # cap at Expert level for run 30 (sort); raised to 4096 for run 31
WM_STEP_MIN    = 64    # larger OD step → faster convergence (was 32)
WM_STEP_FINE   = 8     # finer internal MIMO step granularity
RECOVERY_INTS  = 3     # intervals of zero-benefit before WM recovery kicks in


@dataclass
class ConsumerHistory:
    """Circular buffer of (size_mb, benefit) pairs for one memory consumer."""
    name: str
    maxlen: int = N_WINDOW
    sizes:    deque = field(default_factory=deque)
    benefits: deque = field(default_factory=deque)

    def add(self, size_mb: float, benefit: float):
        self.sizes.append(size_mb)
        self.benefits.append(benefit)
        if len(self.sizes) > self.maxlen:
            self.sizes.popleft()
            self.benefits.popleft()

    def __len__(self):
        return len(self.sizes)


def _least_squares_slope(xs, ys) -> tuple[float, float]:
    """Return (slope, offset) from batch least-squares (DB2 eq. in §3.2.2.1)."""
    xs, ys = np.array(xs, dtype=float), np.array(ys, dtype=float)
    mean_x, mean_y = xs.mean(), ys.mean()
    num = np.sum((xs - mean_x) * (ys - mean_y))
    den = np.sum((xs - mean_x) ** 2)
    if abs(den) < 1e-12:
        return 0.0, mean_y
    slope = num / den
    offset = mean_y - slope * mean_x
    return slope, offset


def _f_statistic(xs, ys, slope, offset) -> float:
    """F-statistic for null-hypothesis test (§3.2.2.2)."""
    xs, ys = np.array(xs, dtype=float), np.array(ys, dtype=float)
    predicted = slope * xs + offset
    sse = np.sum((ys - predicted) ** 2)
    sst = np.sum((ys - ys.mean()) ** 2)
    if sst < 1e-15:
        return 0.0
    n = len(xs)
    r2 = 1.0 - sse / sst
    if r2 >= 1.0 or n <= 2:
        return float("inf")
    return (r2 / 1) / ((1 - r2) / (n - 2))


class MIMOModel:
    """Per-consumer MIMO linear model with accuracy validation."""

    SLOPE_SMALL_NEG = -1e-6  # assigned when slope ≈ 0 (§3.2.2.2)
    F_THRESHOLD     = 2.0    # minimum F to accept model (rough 70% confidence)

    def __init__(self, name: str):
        self.name  = name
        self.slope: Optional[float]  = None
        self.offset: Optional[float] = None
        self.valid  = False

    def fit(self, history: ConsumerHistory) -> bool:
        """Fit model from history; return True if model is acceptable."""
        if len(history) < 5:
            self.valid = False
            return False
        slope, offset = _least_squares_slope(history.sizes, history.benefits)
        f = _f_statistic(list(history.sizes), list(history.benefits), slope, offset)

        # Null hypothesis: reject if F too small
        if f < self.F_THRESHOLD:
            self.valid = False
            return False

        # Sign check: slope should be ≤ 0 (more memory → less marginal benefit)
        if slope > 0:
            self.valid = False
            return False

        if abs(slope) < 1e-10:
            slope = self.SLOPE_SMALL_NEG

        self.slope  = slope
        self.offset = offset
        self.valid  = True
        return True

    def gain(self) -> float:
        """Integral control gain = (p-1)/slope."""
        if not self.valid or self.slope is None or abs(self.slope) < 1e-15:
            return 0.0
        return (POLE - 1.0) / self.slope   # positive (slope<0 → gain>0)

    def delta_size(self, benefit: float, avg_benefit: float) -> float:
        """Size change from integral control law: gain × (benefit - avg_benefit)."""
        return self.gain() * (benefit - avg_benefit)


class STMMController:
    """
    Self-Tuning Memory Manager (adapted from DB2 STMM, VLDB 2006).

    Manages two consumers:
      - work_mem  (fast, online)
      - shared_buffers (slow, needs restart — only suggest; caller decides)

    Call `tick(stats)` every poll interval; returns (new_wm_mb, suggest_sb_mb).
    suggest_sb_mb is None if no SB change is recommended yet.
    """

    def __init__(self,
                 wm_init_mb: int = 64,
                 sb_init_mb: int = 6144,
                 poll_s: int = 30):
        self.wm_mb   = float(wm_init_mb)
        self.sb_mb   = float(sb_init_mb)
        self.poll_s  = poll_s

        self._wm_hist  = ConsumerHistory("work_mem")
        self._sb_hist  = ConsumerHistory("shared_buffers")
        self._wm_model = MIMOModel("work_mem")
        self._sb_model = MIMOModel("shared_buffers")

        # OD controller state
        self._od_wm_direction = +1   # +1 = increase, -1 = decrease
        self._od_last_wm_delta = 0.0
        self._od_oscillation = 0

        # Zero-benefit recovery counter
        self._zero_benefit_count = 0
        # Smoothed benefit for bursty spill signals (EMA)
        self._smoothed_wm_ben = 0.0

        # SB slow-consumer state: grow when AP+spill, shrink when AP idle
        self._sb_grow_count   = 0
        self._sb_shrink_count = 0

        # Diagnostic log (last N entries)
        self.log: list[dict] = []

    # ── Benefit computation ────────────────────────────────────────────────────

    def _wm_benefit(self, temp_bytes_delta: int, n_ap: int) -> float:
        """
        work_mem benefit = saved sort-spill seconds / MB.
        Uses exponential smoothing (EMA, α=0.5) to handle bursty temp_bytes signals:
          - When spill detected: instant benefit is blended into smoothed estimate
          - When AP active but no new spill: smoothed value decays slowly (×0.9)
          - When AP idle: smoothed value decays quickly (×0.3) → recovery
        """
        spill_mb = max(0.0, temp_bytes_delta / 1024 / 1024)

        if spill_mb >= 0.1:
            # New spill: compute instant benefit and blend into smoothed
            saved_s = 0.5 * spill_mb * DISK_WRITE_COST_S_PER_MB
            instant = saved_s / max(self.wm_mb, 1.0)
            self._smoothed_wm_ben = 0.5 * self._smoothed_wm_ben + 0.5 * instant
        elif n_ap > 0:
            # AP active but no new spill this interval: slow decay (keep signal alive)
            self._smoothed_wm_ben *= 0.9
        else:
            # No AP: fast decay toward 0 (×0.1 per interval → below 1e-4 in ~4 intervals)
            self._smoothed_wm_ben *= 0.1
            if self._smoothed_wm_ben < 1e-8:
                self._smoothed_wm_ben = 0.0

        return self._smoothed_wm_ben

    def _sb_benefit(self, blks_read_delta: int, blks_hit_delta: int) -> float:
        """
        shared_buffers benefit = saved page-read seconds / MB.
        Uses measured cache miss rate as SBPX analog.
        """
        total = blks_read_delta + blks_hit_delta
        if total <= 0:
            return 0.0
        miss_rate = blks_read_delta / total
        page_size_mb = PAGE_SIZE_KB / 1024.0
        read_mb = blks_read_delta * page_size_mb
        saved_s = read_mb * DISK_READ_COST_S_PER_MB
        return saved_s / max(self.sb_mb, 1.0)

    # ── OD controller ─────────────────────────────────────────────────────────

    def _od_step_wm(self, benefit: float) -> float:
        """Oscillation-Dampening: fixed 10% step, direction guided by benefit."""
        step = max(self.wm_mb * 0.10, WM_STEP_MIN)
        if benefit < 1e-9:
            delta = -step   # no benefit → shrink back
        else:
            delta = +step * self._od_wm_direction

        # Detect oscillation: reversed twice → halve step (dampening)
        if self._od_last_wm_delta != 0 and (delta * self._od_last_wm_delta < 0):
            self._od_oscillation += 1
            if self._od_oscillation >= 2:
                step *= 0.5
                self._od_oscillation = 0
        else:
            self._od_oscillation = max(0, self._od_oscillation - 1)

        self._od_last_wm_delta = delta
        return delta

    # ── Memory transfer (greedy, §3.3) ────────────────────────────────────────

    def _apply_transfer(self, wm_delta: float, fine: bool = False) -> float:
        """Clamp delta to transfer limits and boundary constraints."""
        max_inc = MAX_INC_RATIO * self.wm_mb
        max_dec = MAX_DEC_RATIO * self.wm_mb

        wm_delta = max(-max_dec, min(max_inc, wm_delta))

        # Ignore insignificant resizes
        if abs(wm_delta) < MIN_RESIZE_PCT * self.wm_mb:
            wm_delta = 0.0

        new_wm = self.wm_mb + wm_delta
        new_wm = max(WM_MIN_MB, min(WM_MAX_MB, new_wm))
        # MIMO uses finer granularity; OD uses coarse 32MB steps
        step = WM_STEP_FINE if fine else WM_STEP_MIN
        new_wm = round(new_wm / step) * step
        return max(WM_MIN_MB, new_wm)

    # ── Main tick ─────────────────────────────────────────────────────────────

    def tick(self,
             blks_hit_delta:  int,
             blks_read_delta: int,
             temp_bytes_delta: int,
             n_ap: int) -> tuple[int, Optional[int]]:
        """
        One tuning interval.  Returns (new_wm_mb, suggest_sb_mb).
        suggest_sb_mb is None unless a SB restart is warranted.
        """
        wm_ben = self._wm_benefit(temp_bytes_delta, n_ap)
        sb_ben = self._sb_benefit(blks_read_delta, blks_hit_delta)

        self._wm_hist.add(self.wm_mb, wm_ben)
        self._sb_hist.add(self.sb_mb, sb_ben)

        # Rebuild MIMO models
        self._wm_model.fit(self._wm_hist)
        self._sb_model.fit(self._sb_hist)

        # Track zero-benefit intervals for recovery.
        # Only count when AP is truly idle: benefit below noise floor AND n_ap=0.
        if wm_ben < 1e-4 and n_ap == 0:
            self._zero_benefit_count += 1
        else:
            self._zero_benefit_count = 0

        # Recovery: if no AP and benefit has been 0 for RECOVERY_INTS intervals, shrink WM
        if self._zero_benefit_count >= RECOVERY_INTS and self.wm_mb > WM_MIN_MB:
            # Gradual return: shrink by 20% toward WM_MIN, use coarse rounding
            # fine=False prevents WM_STEP_FINE=8 from rounding -3.2 back to 0
            wm_delta = -(self.wm_mb - WM_MIN_MB) * MAX_DEC_RATIO
            new_wm = self._apply_transfer(wm_delta, fine=False)
            controller = "RECOVER"
        elif self._wm_model.valid:
            avg_ben = (wm_ben + sb_ben) / 2.0
            wm_delta = self._wm_model.delta_size(wm_ben, avg_ben)
            new_wm = self._apply_transfer(wm_delta, fine=True)
            controller = "MIMO"
        else:
            wm_delta = self._od_step_wm(wm_ben)
            new_wm = self._apply_transfer(wm_delta, fine=False)
            controller = "OD"

        # SB slow-consumer: grow SB when AP+spill active, shrink when AP idle+recovered
        suggest_sb = None
        if n_ap > 0 and wm_ben > 1e-4 and self.sb_mb < SB_MAX_MB:
            # AP active + spill detected → grow SB to protect TP pages from scan eviction
            self._sb_grow_count += 1
            self._sb_shrink_count = 0
        elif n_ap == 0 and self._zero_benefit_count >= RECOVERY_INTS and self.sb_mb > SB_INIT_MB:
            # AP idle + WM recovered → shrink SB back toward baseline
            self._sb_shrink_count += 1
            self._sb_grow_count = 0
        else:
            self._sb_grow_count   = max(0, self._sb_grow_count - 1)
            self._sb_shrink_count = max(0, self._sb_shrink_count - 1)

        if self._sb_grow_count >= SB_TRIGGER_INTERVALS:
            new_sb_mb = min(SB_MAX_MB, int(self.sb_mb) + SB_STEP_MB)
            suggest_sb = new_sb_mb
            self._sb_grow_count = 0
        elif self._sb_shrink_count >= SB_TRIGGER_INTERVALS:
            new_sb_mb = max(SB_INIT_MB, int(self.sb_mb) - SB_STEP_MB)
            suggest_sb = new_sb_mb
            self._sb_shrink_count = 0

        # Log
        self.log.append({
            "wm_mb":      int(self.wm_mb),
            "sb_mb":      int(self.sb_mb),
            "wm_ben":     round(wm_ben, 6),
            "sb_ben":     round(sb_ben, 6),
            "wm_delta":   round(new_wm - self.wm_mb, 1),
            "new_wm_mb":  int(new_wm),
            "controller": controller,
            "mimo_valid": self._wm_model.valid,
            "slope":      round(self._wm_model.slope or 0, 8),
            "suggest_sb": suggest_sb,
        })

        self.wm_mb = new_wm
        return int(new_wm), suggest_sb

    def summary(self) -> str:
        if not self.log:
            return "No intervals logged yet."
        last = self.log[-1]
        n = len(self.log)
        sb_note = f"  →suggest_sb={last['suggest_sb']}MB" if last.get("suggest_sb") else ""
        return (f"Interval {n}: WM {last['wm_mb']}→{last['new_wm_mb']}MB  "
                f"SB={last['sb_mb']}MB  "
                f"wm_ben={last['wm_ben']:.4f}  sb_ben={last['sb_ben']:.4f}  "
                f"ctrl={last['controller']}  slope={last['slope']:.2e}{sb_note}")


# ─────────────────────────────────────────────────────────────────────────────
# Bi-Resource Benefit Estimator (BRBE)
# Joint WM+SB trade-off model for TP+AP mixed workloads.
#
# Model intuition (extends DB2 STMM §3.1 to two interacting resources):
#
#   B_WM(wm) = α(wm) × spill_rate × DISK_COST / wm
#     α(wm)  = spill reducibility ∈ [0,1]: how much spill can be eliminated
#              by adding more WM.  Estimated via EMA of (new_spill_on_WM_increase).
#              When WM is large enough to sort in memory, α→0.
#
#   B_SB(sb, ap_scan_mb) = miss_rate × PAGE_READ_COST / sb × protection(sb, ap)
#     protection(sb, ap_scan_mb) = max(0, 1 - ap_scan_mb / sb)
#       Captures ring-buffer effect: AP Seq Scans use ring buffers and don't
#       dirty SB. But AP hash joins / nested-loop index scans DO use SB.
#       protection→0 when AP scan pressure exceeds SB capacity, meaning
#       extra SB is wasted filling with AP data rather than protecting TP pages.
#
#   Allocation decision: allocate next ΔM to the resource with higher marginal
#   benefit.  Since both resources have decreasing marginal benefit (concave),
#   the greedy marginal comparison is optimal for a fixed total pool M.
#
# The BRBE is used here as a predictor to suggest SB changes to the caller.
# WM is still controlled by the base STMM OD+MIMO controller.
# ─────────────────────────────────────────────────────────────────────────────

# Fraction of AP reads that bypass SB via ring buffer (Seq Scan).
# Set based on workload: 1.0 = pure Seq Scan (full ring-buffer bypass),
#                         0.0 = pure random access (no bypass).
RING_BUFFER_BYPASS_RATIO = 0.8   # empirical: most AP reads are Seq Scan

# AP scan data size estimate (MB per worker per interval).
# Used to estimate ap_scan_mb when not directly measurable.
AP_SCAN_MB_ESTIMATE = 400.0      # ~half of sbtest1 (2M rows × ~200B = 400MB)

# Proactive sort-model constant (proposql.pdf §"以Sort为例", OpenGauss tuplesort.c)
SORT_TUPLE_OVERHEAD_B = 24   # maxalign(TupleHeader, 8) added per sort entry


class BRBEController(STMMController):
    """
    Bi-Resource Benefit Estimator — extends STMMController with a joint
    WM/SB trade-off prediction model.

    Adds:
      - `_brbe_suggest_sb()`: returns a new SB recommendation (MB) or None
        based on comparing marginal WM vs SB benefit.
      - Overrides `_sb_benefit()` to apply the AP protection factor.
      - Tracks spill reducibility α via EMA.

    Usage: same as STMMController; returns the same (new_wm_mb, suggest_sb_mb)
    interface but with principled SB decisions.
    """

    # EMA smoothing for spill reducibility α
    ALPHA_INIT = 1.0     # start optimistic: assume spill is reducible
    ALPHA_DECAY = 0.85   # decay factor when WM increases but spill persists
    ALPHA_RECOVER = 0.95 # partial recovery when spill drops after WM increase

    # EMA smoothing for SB read reducibility β
    BETA_INIT    = 1.0   # start optimistic: assume reads are SB-reducible
    BETA_DECAY   = 0.85  # decay when SB increases but blks_read persists
    BETA_RECOVER = 0.95  # recover when blks_read drops after SB increase

    def __init__(self,
                 wm_init_mb: int = 64,
                 sb_init_mb: int = 6144,
                 poll_s: int = 30,
                 total_mem_mb: int = 15360,
                 n_ap_workers: int = 4,
                 calib_json: str | None = "run-logs/sb_calib9.json"):
        super().__init__(wm_init_mb=wm_init_mb, sb_init_mb=sb_init_mb, poll_s=poll_s)
        self._total_mem_mb = float(total_mem_mb)
        self._n_ap_workers = n_ap_workers
        self._alpha = self.ALPHA_INIT
        self._beta  = self.BETA_INIT
        self._prev_spill_mb   = 0.0
        self._prev_wm_mb      = float(wm_init_mb)
        self._prev_blks_read  = 0
        self._prev_sb_mb      = float(sb_init_mb)
        self._penalty_model      = SBPenaltyModel(calib_json=calib_json,
                                                log_fn=lambda m: self.log.append({"event": m}))
        self._iowait_pct_baseline: float = 0.0  # TP-only iowait% from PRE phase

    def set_iowait_baseline(self, pct: float):
        """Record TP-only iowait% measured during PRE phase as write-IO penalty baseline."""
        self._iowait_pct_baseline = pct

    def _update_alpha(self, spill_mb: float):
        """Update spill reducibility based on whether WM increase reduced spill."""
        wm_increased = self.wm_mb > self._prev_wm_mb + 1.0
        if wm_increased:
            if spill_mb < self._prev_spill_mb - 0.01:
                self._alpha = min(1.0, self._alpha * self.ALPHA_RECOVER + 0.05)
            else:
                self._alpha = max(0.0, self._alpha * self.ALPHA_DECAY)
        self._prev_spill_mb = spill_mb
        self._prev_wm_mb    = self.wm_mb

    def _update_beta(self, blks_read_delta: int):
        """Update SB read reducibility based on whether SB increase reduced blks_read."""
        sb_increased = self.sb_mb > self._prev_sb_mb + 1.0
        if sb_increased:
            if blks_read_delta < self._prev_blks_read * 0.99:
                # Reads dropped after SB increase: β validated
                self._beta = min(1.0, self._beta * self.BETA_RECOVER + 0.05)
            else:
                # Reads persisted despite SB increase: reads not SB-reducible
                self._beta = max(0.0, self._beta * self.BETA_DECAY)
        self._prev_blks_read = blks_read_delta
        self._prev_sb_mb     = self.sb_mb

    def _sb_benefit_brbe(self, blks_read_delta: int, blks_hit_delta: int,
                          n_ap: int, iowait_pct_now: float = 0.0) -> float:
        """
        Net marginal benefit of SB = read-IO savings − write-IO penalty.

        Read benefit:
          β × blks_read × PAGE_MB × DISK_READ_COST / sb_mb
          Units: [s per poll-interval per MB]

        Write penalty (iowait%-based):
          penalty = max(0, iowait_pct_now - iowait_baseline) / 100 * poll_s / sb_mb
          Units: [s per poll-interval per MB] — same as read benefit.

          iowait_baseline is the TP-only iowait% set by set_iowait_baseline() during PRE.
          The delta is purely from AP-phase disk pressure (bgwriter/checkpoint competition).
          When iowait_pct_now=0 (not measured), penalty=0 — no regression vs current code.
        """
        if blks_read_delta <= 0:
            return 0.0
        page_size_mb = PAGE_SIZE_KB / 1024.0
        read_mb      = blks_read_delta * page_size_mb
        read_benefit = self._beta * read_mb * DISK_READ_COST_S_PER_MB / max(self.sb_mb, 1.0)

        penalty = 0.0
        if iowait_pct_now > 0.0:
            penalty = self._penalty_model.io_penalty(
                int(self.sb_mb), iowait_pct_now,
                self._iowait_pct_baseline, self.poll_s)

        return max(0.0, read_benefit - penalty)

    def _brbe_marginal_wm(self, wm_ben: float) -> float:
        """Marginal benefit of adding 1MB to WM: α × B_WM."""
        return self._alpha * wm_ben

    def _brbe_marginal_sb(self, sb_ben_brbe: float) -> float:
        """Marginal benefit of adding 1MB to SB: β × plain_sb_ben / sb_mb (already computed)."""
        return sb_ben_brbe

    def _brbe_suggest_sb(self, wm_ben: float, sb_ben_brbe: float,
                          n_ap: int,
                          blks_hit_delta: int = 0,
                          blks_read_delta: int = 0) -> Optional[int]:
        """
        Compare marginal benefits of WM vs SB. If SB marginal benefit persistently
        exceeds WM's, suggest a SB increase; if WM is dominant and AP is gone,
        suggest SB decrease.

        Path 1 (n_ap=0): if TP cache hit ratio < SB_HIT_RATIO_MIN, the TP working
        set does not fit in current SB → recommend growth regardless of AP.
        Path 2 (n_ap>0): standard BRBE trade-off (mb_sb > mb_wm).

        Returns new SB (MB) or None.
        """
        mb_wm = self._brbe_marginal_wm(wm_ben)
        mb_sb = self._brbe_marginal_sb(sb_ben_brbe)

        total     = blks_hit_delta + blks_read_delta
        hit_ratio = blks_hit_delta / total if total > 0 else 1.0

        # Path 1: TP-only, cache hit ratio too low → working set > SB.
        # Extrapolate true working set: ws ≈ current_sb / hit_ratio, capped at SB_MAX_MB.
        if n_ap == 0 and self.sb_mb < SB_MAX_MB and hit_ratio < SB_HIT_RATIO_MIN and hit_ratio > 0:
            ws_mb = min(self.sb_mb / hit_ratio, SB_MAX_MB)
            steps = math.ceil((ws_mb - SB_INIT_MB) / SB_STEP_MB)
            target_sb = min(SB_MAX_MB, SB_INIT_MB + steps * SB_STEP_MB)
            if target_sb > self.sb_mb:
                self._sb_grow_count += 1
                self._sb_shrink_count = 0
                if self._sb_grow_count >= SB_TRIGGER_INTERVALS:
                    self._sb_grow_count = 0
                    return target_sb
                return None
        # Path 2: AP active, SB marginal benefit > WM marginal benefit
        elif mb_sb > mb_wm and n_ap > 0 and self.sb_mb < SB_MAX_MB:
            self._sb_grow_count += 1
            self._sb_shrink_count = 0
        # Shrink SB: AP gone, WM benefit gone → SB not needed
        elif n_ap == 0 and self._zero_benefit_count >= RECOVERY_INTS and self.sb_mb > SB_INIT_MB:
            self._sb_shrink_count += 1
            self._sb_grow_count = 0
        else:
            self._sb_grow_count   = max(0, self._sb_grow_count - 1)
            self._sb_shrink_count = max(0, self._sb_shrink_count - 1)

        if self._sb_grow_count >= SB_TRIGGER_INTERVALS:
            new_sb = min(SB_MAX_MB, int(self.sb_mb) + SB_STEP_MB)
            self._sb_grow_count = 0
            return new_sb
        elif self._sb_shrink_count >= SB_TRIGGER_INTERVALS:
            new_sb = max(SB_INIT_MB, int(self.sb_mb) - SB_STEP_MB)
            self._sb_shrink_count = 0
            return new_sb
        return None

    def tick(self,
             blks_hit_delta:  int,
             blks_read_delta: int,
             temp_bytes_delta: int,
             n_ap: int,
             iowait_pct_now: float = 0.0) -> tuple[int, Optional[int]]:
        """Override tick() to use BRBE SB benefit and trade-off logic."""
        spill_mb = max(0.0, temp_bytes_delta / 1024 / 1024)
        self._update_alpha(spill_mb)
        self._update_beta(blks_read_delta)

        wm_ben = self._wm_benefit(temp_bytes_delta, n_ap)
        sb_ben_brbe = self._sb_benefit_brbe(blks_read_delta, blks_hit_delta, n_ap, iowait_pct_now)
        sb_ben_plain = self._sb_benefit(blks_read_delta, blks_hit_delta)

        self._wm_hist.add(self.wm_mb, wm_ben)
        self._sb_hist.add(self.sb_mb, sb_ben_plain)

        self._wm_model.fit(self._wm_hist)
        self._sb_model.fit(self._sb_hist)

        if wm_ben < 1e-4 and n_ap == 0:
            self._zero_benefit_count += 1
        else:
            self._zero_benefit_count = 0

        if self._zero_benefit_count >= RECOVERY_INTS and self.wm_mb > WM_MIN_MB:
            wm_delta = -(self.wm_mb - WM_MIN_MB) * MAX_DEC_RATIO
            # Use floor (not round) so small deltas still make progress toward WM_MIN.
            # round() rounds -3.2 → 0 change at WM=80; floor guarantees ≥1 step reduction.
            import math
            new_wm_raw = max(WM_MIN_MB, min(WM_MAX_MB, self.wm_mb + wm_delta))
            new_wm = max(WM_MIN_MB, math.floor(new_wm_raw / WM_STEP_FINE) * WM_STEP_FINE)
            controller = "RECOVER"
        elif self._wm_model.valid:
            avg_ben = (wm_ben + sb_ben_plain) / 2.0
            wm_delta = self._wm_model.delta_size(wm_ben, avg_ben)
            new_wm = self._apply_transfer(wm_delta, fine=True)
            controller = "MIMO"
        else:
            wm_delta = self._od_step_wm(wm_ben)
            new_wm = self._apply_transfer(wm_delta, fine=False)
            controller = "OD"

        # BRBE SB suggestion
        suggest_sb = self._brbe_suggest_sb(wm_ben, sb_ben_brbe, n_ap,
                                            blks_hit_delta, blks_read_delta)

        self.log.append({
            "wm_mb":      int(self.wm_mb),
            "sb_mb":      int(self.sb_mb),
            "wm_ben":     round(wm_ben, 6),
            "sb_ben":     round(sb_ben_plain, 6),
            "sb_ben_brbe": round(sb_ben_brbe, 6),
            "alpha":      round(self._alpha, 4),
            "beta":       round(self._beta, 4),
            "mb_wm":      round(self._brbe_marginal_wm(wm_ben), 6),
            "mb_sb":      round(self._brbe_marginal_sb(sb_ben_brbe), 6),
            "wm_delta":   round(new_wm - self.wm_mb, 1),
            "new_wm_mb":  int(new_wm),
            "controller": controller,
            "mimo_valid": self._wm_model.valid,
            "slope":      round(self._wm_model.slope or 0, 8),
            "suggest_sb": suggest_sb,
        })

        self.wm_mb = new_wm
        return int(new_wm), suggest_sb

    def summary(self) -> str:
        if not self.log:
            return "No intervals logged yet."
        last = self.log[-1]
        n = len(self.log)
        sb_note = f"  →suggest_sb={last['suggest_sb']}MB" if last.get("suggest_sb") else ""
        return (f"Interval {n}: WM {last['wm_mb']}→{last['new_wm_mb']}MB  "
                f"SB={last['sb_mb']}MB  "
                f"wm_ben={last['wm_ben']:.4f}  sb_ben={last['sb_ben']:.4f}  "
                f"α={last['alpha']:.3f}  β={last.get('beta',1.0):.3f}  "
                f"mb_wm={last['mb_wm']:.4f}  mb_sb={last['mb_sb']:.4f}  "
                f"ctrl={last['controller']}{sb_note}")


class ProactiveBRBEController(BRBEController):
    """
    Proactive variant of BRBEController (proposql.pdf §算子级代价建模 + §共享内存推荐).

    Before AP injection, uses:
      1. Sort cost model (source-code constants): given AP query rows + tuple width,
         compute the exact work_mem threshold for one-pass sort (no spill).
      2. SB working-set model: TP hot-page footprint (from pg_stat_database blks_hit
         during PRE) + AP scan footprint → minimum SB to hold both during AP.

    Call predict_pre_ap() at end of PRE phase to get (wm_mb, sb_mb) recommendations.
    During AP, tick() runs as normal BRBEController for fine-tuning.
    """

    def __init__(self, wm_init_mb: int = 64, sb_init_mb: int = 6144,
                 poll_s: int = 30, n_ap_workers: int = 4):
        super().__init__(wm_init_mb=wm_init_mb, sb_init_mb=sb_init_mb, poll_s=poll_s,
                         n_ap_workers=n_ap_workers)
        self._proactive_wm: int = wm_init_mb
        self._proactive_sb: int = sb_init_mb
        self._ap_phase_active: bool = False  # set True by test harness during AP window

    def start_ap_phase(self):
        """Call just before AP injection. HOLD maintains WM ≥ proactive floor."""
        self._ap_phase_active = True

    def end_ap_phase(self):
        """Call after AP ends. Allows normal WM recovery during POST phase."""
        self._ap_phase_active = False

    @staticmethod
    def _wm_threshold_for_sort(rows: int, tuple_width_b: int) -> int:
        """
        Minimum work_mem (MB) for a one-pass sort (no temp-file spill).

        Model (proposql.pdf §Sort, OpenGauss tuplesort.c):
          sort_entry_b  = tuple_width_b + SORT_TUPLE_OVERHEAD_B
          input_bytes   = rows × sort_entry_b
          one-pass cond : input_bytes ≤ work_mem × 1024

        Returns MB clamped to [WM_MIN_MB, WM_MAX_MB].
        """
        sort_entry_b = tuple_width_b + SORT_TUPLE_OVERHEAD_B
        input_bytes  = rows * sort_entry_b
        wm_mb = math.ceil(input_bytes / (1024 * 1024))
        # Round UP to OD step granularity so _apply_transfer doesn't snap below threshold.
        wm_mb = math.ceil(wm_mb / WM_STEP_MIN) * WM_STEP_MIN
        return max(WM_MIN_MB, min(WM_MAX_MB, wm_mb))

    @staticmethod
    def _mimo_simulate(ap_rows: int,
                       ap_tuple_width_b: int,
                       n_ap_workers: int,
                       blks_hit_per_interval: float,
                       blks_read_per_interval: float,
                       total_budget_mb: int,
                       current_sb_mb: int = SB_INIT_MB,
                       n_iter: int = 50) -> tuple[int, int, int, float, float, float]:
        """
        Simulate DB2 MIMO to find offline WM+SB allocation before AP starts.

        Two cases:
          Case A — WM sufficient (input_mb ≤ WM_MAX_MB): wm_ben=0, no MIMO needed.
            Return wm = sort threshold, sb = TP working-set estimate.
            The TP working set: current_sb / hit_ratio (cache miss tells us how
            much of the working set is NOT in SB). Capped at SB_MAX_MB.

          Case B — WM insufficient (input_mb > WM_MAX_MB): run MIMO until
            wm_ben(wm*) ≈ sb_ben(sb*) fixed point under soft budget constraint.

        SB penalty is NOT applied offline (no iowait signal during PRE phase).
        Online tick() will fine-tune SB using live iowait% via _sb_benefit_brbe.

        Returns (wm_mb, sb_mb, iters_used, input_mb, B_total, tp_ws_mb).
        """
        sort_entry_b = ap_tuple_width_b + SORT_TUPLE_OVERHEAD_B
        input_mb = ap_rows * sort_entry_b / (1024.0 ** 2)
        B_total  = blks_read_per_interval * (PAGE_SIZE_KB / 1024.0) * DISK_READ_COST_S_PER_MB

        # TP working-set estimate: current_sb / hit_ratio (miss rate tells gap)
        total_blks = blks_hit_per_interval + blks_read_per_interval
        if total_blks > 0:
            hit_ratio = max(blks_hit_per_interval / total_blks, 0.5)
            tp_ws_mb = current_sb_mb / hit_ratio
        else:
            tp_ws_mb = float(current_sb_mb)

        wm_init = max(float(WM_MIN_MB), min(float(WM_MAX_MB), input_mb))
        wm_out_raw = max(WM_MIN_MB, min(WM_MAX_MB,
                         int(math.ceil(wm_init / WM_STEP_MIN)) * WM_STEP_MIN))

        # Case A: WM at or below WM_MAX covers the sort input → no spill.
        # SB recommendation = TP working-set estimate (no MIMO needed).
        if input_mb <= WM_MAX_MB:
            tp_ws_capped = max(float(current_sb_mb), min(float(SB_MAX_MB), tp_ws_mb))
            sb_out = max(current_sb_mb, min(SB_MAX_MB,
                         int(math.ceil(tp_ws_capped / SB_STEP_MB)) * SB_STEP_MB))
            return wm_out_raw, sb_out, 1, input_mb, B_total, tp_ws_mb

        # Case B: WM_MAX insufficient for one-pass sort → run MIMO.
        wm = wm_init
        sb = max(float(current_sb_mb), min(float(SB_MAX_MB), tp_ws_mb))
        if n_ap_workers * wm + sb > total_budget_mb:
            sb = max(float(current_sb_mb), float(total_budget_mb) - n_ap_workers * wm)

        iters_used = 0
        for i in range(n_iter):
            iters_used = i + 1
            spill_mb = n_ap_workers * max(0.0, input_mb - wm)
            wm_ben   = (0.5 * spill_mb * DISK_WRITE_COST_S_PER_MB / max(wm, 1.0)
                        if spill_mb > 0.0 else 0.0)

            sb_ben = B_total / max(sb, 1.0)   # no offline penalty (no iowait signal)
            avg_ben = (wm_ben + sb_ben) / 2.0

            if spill_mb > 0.0:
                slope_wm = (-0.5 * DISK_WRITE_COST_S_PER_MB * n_ap_workers
                            * input_mb / (wm ** 2))
                gain_wm  = (POLE - 1.0) / slope_wm
                wm_delta = gain_wm * (wm_ben - avg_ben)
                wm_delta = max(-MAX_DEC_RATIO * wm, min(MAX_INC_RATIO * wm, wm_delta))
                wm_new   = max(WM_MIN_MB, min(WM_MAX_MB, wm + wm_delta))
            else:
                wm_new = wm

            if sb_ben > 0.0:
                slope_sb = -B_total / max(sb ** 2, 1.0)
                gain_sb  = (POLE - 1.0) / slope_sb
                sb_delta = gain_sb * (sb_ben - avg_ben)
                sb_delta = max(-MAX_DEC_RATIO * sb, min(MAX_INC_RATIO * sb, sb_delta))
                sb_new   = max(float(current_sb_mb), min(float(SB_MAX_MB), sb + sb_delta))
            else:
                sb_new = sb

            if n_ap_workers * wm_new + sb_new > total_budget_mb:
                sb_new = max(float(current_sb_mb),
                             float(total_budget_mb) - n_ap_workers * wm_new)

            if abs(wm_new - wm) < 0.5 and abs(sb_new - sb) < 0.5:
                wm, sb = wm_new, sb_new
                break
            wm, sb = wm_new, sb_new

        wm_out = max(WM_MIN_MB, min(WM_MAX_MB,
                     int(math.ceil(wm / WM_STEP_MIN)) * WM_STEP_MIN))
        sb_out = max(current_sb_mb, min(SB_MAX_MB,
                     (int(sb) // SB_STEP_MB) * SB_STEP_MB))
        return wm_out, sb_out, iters_used, input_mb, B_total, tp_ws_mb

    def predict_pre_ap(self,
                       ap_rows: int,
                       ap_tuple_width_b: int,
                       blks_hit_delta: int,
                       blks_read_delta: int,
                       n_ap_workers: int = 4,
                       total_budget_mb: int = 10240,
                       pre_s: int = 60) -> tuple[int, int]:
        """
        Called at end of PRE phase. Returns (wm_recommended_mb, sb_recommended_mb).

        Runs _mimo_simulate() with analytically-derived benefit functions to find the
        DB2 MIMO fixed point offline before AP starts, eliminating the reactive warmup.

        blks_read_delta: total blks_read observed during PRE phase (parametrizes SB
                         benefit curve B_total/sb).
        """
        # Convert PRE-phase total blks_read to per-interval rate
        n_intervals = max(1.0, pre_s / float(self.poll_s))
        blks_hit_per_interval  = blks_hit_delta  / n_intervals
        blks_read_per_interval = blks_read_delta / n_intervals

        wm_alloc, sb_alloc, iters_used, input_mb, B_total, tp_ws_mb = self._mimo_simulate(
            ap_rows=ap_rows,
            ap_tuple_width_b=ap_tuple_width_b,
            n_ap_workers=n_ap_workers,
            blks_hit_per_interval=blks_hit_per_interval,
            blks_read_per_interval=blks_read_per_interval,
            total_budget_mb=total_budget_mb,
            current_sb_mb=int(self.sb_mb),
        )
        self._proactive_wm = wm_alloc
        self._proactive_sb = sb_alloc

        # Reset MIMO history — PRE-phase data (WM=init_mb, ben=0) would poison MIMO
        # and keep the model invalid at AP start, forcing OD to step down from wm_rec.
        self._wm_hist        = ConsumerHistory("work_mem")
        self._sb_hist        = ConsumerHistory("shared_buffers")
        self._wm_model       = MIMOModel("work_mem")
        self._sb_model       = MIMOModel("shared_buffers")
        self._od_wm_direction   = +1
        self._od_last_wm_delta  = 0.0
        self._od_oscillation    = 0
        self._zero_benefit_count = 0

        self.log.append({
            "event":              "proactive_predict",
            "ap_rows":            ap_rows,
            "ap_tuple_width_b":   ap_tuple_width_b,
            "input_mb":           round(input_mb, 1),
            "tp_ws_mb":           round(tp_ws_mb, 1),
            "B_total":            round(B_total, 6),
            "blks_hit_delta":     int(blks_hit_delta),
            "blks_read_delta":    int(blks_read_delta),
            "iters_used":         iters_used,
            "total_budget_mb":    total_budget_mb,
            "wm_recommended_mb":  wm_alloc,
            "sb_recommended_mb":  sb_alloc,
        })
        return wm_alloc, sb_alloc

    def _od_step_wm(self, benefit: float) -> float:
        """OD step clamped to never drop WM below the proactive prediction floor."""
        delta = super()._od_step_wm(benefit)
        if self.wm_mb + delta < self._proactive_wm:
            return self._proactive_wm - self.wm_mb
        return delta

    def tick(self,
             blks_hit_delta:  int,
             blks_read_delta: int,
             temp_bytes_delta: int,
             n_ap: int,
             iowait_pct_now: float = 0.0) -> tuple[int, Optional[int]]:
        """Override tick() to hold WM at the proactive floor while AP is active.

        wm_ben=0 during AP means no sort spill — WM is already sufficient.
        RECOVER interprets this as "memory wasted, shrink back", but that is wrong
        when WM is at the proactive threshold: shrinking causes spill, OD climbs
        back, and the cycle repeats every ~120s.

        Fix: if AP is active and RECOVER would push WM below _proactive_wm,
        clamp to _proactive_wm (HOLD state) instead of oscillating.
        """
        new_wm, suggest_sb = super().tick(blks_hit_delta, blks_read_delta,
                                           temp_bytes_delta, n_ap,
                                           iowait_pct_now=iowait_pct_now)

        # HOLD: keep WM at proactive floor throughout AP window.
        # Use _ap_phase_active flag (set by test harness) instead of n_ap alone, because
        # fast AP queries + inter-query sleep make n_ap intermittently 0 even mid-AP.
        if (n_ap > 0 or self._ap_phase_active) and new_wm < self._proactive_wm:
            new_wm = self._proactive_wm
            self.wm_mb = float(self._proactive_wm)
            if self.log and isinstance(self.log[-1], dict) and 'new_wm_mb' in self.log[-1]:
                self.log[-1]['new_wm_mb'] = self._proactive_wm
                self.log[-1]['controller'] = 'HOLD'

        return new_wm, suggest_sb

    def summary(self) -> str:
        if not self.log:
            return "No intervals logged yet."
        last = self.log[-1]
        if last.get("event") == "proactive_predict":
            return (f"[Proactive] rows={last['ap_rows']} width={last['ap_tuple_width_b']}B "
                    f"input={last['input_mb']}MB tp_ws={last['tp_ws_mb']}MB "
                    f"B_total={last['B_total']:.4f} iters={last['iters_used']} "
                    f"→ WM_rec={last['wm_recommended_mb']}MB SB_rec={last['sb_recommended_mb']}MB")
        return super().summary()
