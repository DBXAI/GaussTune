#!/usr/bin/env python3
"""
memory_tuner.py — SB iowait penalty model for GaussTune
=========================================================

Loads sb_calib JSON and provides:
  - w_await interpolation for any SB level (diagnostics)
  - SB penalty factor (for use in _sb_benefit_brbe)
  - Standalone report of calib curve

Penalty formula (iowait%-based, dimensionally correct):
  penalty = max(0, iowait_pct_now - iowait_pct_baseline) / 100 * poll_s / sb_mb
  Units: [s per poll-interval per MB of SB] — same as read_benefit in _sb_benefit_brbe.

  Rationale: iowait fraction of wall-time × poll_s = extra iowait seconds per interval.
  Dividing by sb_mb gives the per-MB cost, comparable to saved read-seconds per MB.

bgwriter tuning is NOT done here — bgwriter_delay=200ms is applied once
at experiment startup in stmm_test.py (uniform across all methods).

Integration:
  from memory_tuner import SBPenaltyModel
  model = SBPenaltyModel(calib_json="run-logs/sb_calib9.json")
  w_await = model.w_await_at(sb_mb=3072)   # diagnostics
  penalty = model.io_penalty(sb_mb=3072, iowait_pct_now=15.0,
                              iowait_pct_baseline=5.0, poll_s=15.0)
"""

from __future__ import annotations
import os, json, math, argparse
from datetime import datetime

# ── Constants ─────────────────────────────────────────────────────────────────
W_AWAIT_BASE_MS  = 12.0   # kept for diagnostics/reporting only


# ── SBPenaltyModel ────────────────────────────────────────────────────────────
class SBPenaltyModel:
    """
    Loads sb_calib JSON and provides w_await interpolation + IO penalty factor.

    Used by BRBEController._sb_benefit_brbe() to subtract write-IO cost from
    the read-IO benefit of increasing shared_buffers.
    """

    def __init__(self, calib_json: str | None = None, log_fn=None):
        self.log = log_fn or (lambda msg: print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"))
        # List of (sb_mb, w_await_ms) sorted by sb_mb (used for diagnostics)
        self._curve: list[tuple[int, float]] = []

        if calib_json and os.path.exists(calib_json):
            self._load(calib_json)
        else:
            self.log("[SBPenaltyModel] calib JSON not found — w_await diagnostics disabled")

    def _load(self, path: str):
        with open(path) as f:
            data = json.load(f)
        for r in data.get("levels", []):
            if r.get("tps") is not None:
                io = r.get("iostat", {})
                w = io.get("w_await", 0.0)
                if w > 0:
                    self._curve.append((r["sb_mb"], w))
        self._curve.sort(key=lambda x: x[0])
        self.log(f"[SBPenaltyModel] Loaded {len(self._curve)} points from {path}")

    def w_await_at(self, sb_mb: int) -> float:
        """Interpolate w_await_ms at the given SB level from calib curve (diagnostics only)."""
        if not self._curve:
            return W_AWAIT_BASE_MS
        if sb_mb <= self._curve[0][0]:
            return self._curve[0][1]
        if sb_mb >= self._curve[-1][0]:
            return self._curve[-1][1]
        for i in range(len(self._curve) - 1):
            s0, w0 = self._curve[i]
            s1, w1 = self._curve[i + 1]
            if s0 <= sb_mb <= s1:
                t = (sb_mb - s0) / (s1 - s0)
                return w0 + t * (w1 - w0)
        return W_AWAIT_BASE_MS

    def io_penalty(self, sb_mb: int,
                   iowait_pct_now: float,
                   iowait_pct_baseline: float = 0.0,
                   poll_s: float = 15.0) -> float:
        """
        Write-IO penalty per MB of SB at current iowait level.

        Formula (iowait%-based, dimensionally correct):
          delta_frac = max(0, iowait_pct_now - iowait_pct_baseline) / 100
          penalty    = delta_frac * poll_s / sb_mb

        Units: [s per poll-interval per MB] — same as read_benefit in _sb_benefit_brbe.

        The penalty is subtracted from read-benefit in _sb_benefit_brbe.
        STMM stops growing SB when write-IO cost exceeds read-IO savings.

        iowait_pct_now:      current iowait% (from /proc/stat 1s sample)
        iowait_pct_baseline: TP-only iowait% measured during PRE phase at same SB
        poll_s:              controller poll interval in seconds
        """
        if sb_mb <= 0:
            return 0.0
        delta_frac = max(0.0, iowait_pct_now - iowait_pct_baseline) / 100.0
        return delta_frac * poll_s / max(float(sb_mb), 1.0)

    def report(self):
        """Print calib curve (w_await diagnostics)."""
        if not self._curve:
            print("  [SBPenaltyModel] No calib data loaded.")
            return
        print("\n── SB w_await Calib Curve (diagnostics only) ────────────")
        print(f"  {'SB(MB)':>8}  {'w_await(ms)':>12}  "
              f"{'penalty(iow=10%,base=5%)':>24}  {'penalty(iow=20%,base=5%)':>24}")
        print(f"  {'─'*8}  {'─'*12}  {'─'*24}  {'─'*24}")
        for sb, w in self._curve:
            p_lo = self.io_penalty(sb, 10.0, 5.0)
            p_hi = self.io_penalty(sb, 20.0, 5.0)
            print(f"  {sb:>8}  {w:>12.1f}  {p_lo:>24.6f}  {p_hi:>24.6f}")
        print("─────────────────────────────────────────────────────────\n")


# ── Standalone ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SB penalty model report")
    parser.add_argument("--calib-json", default="run-logs/sb_calib9.json")
    args = parser.parse_args()
    model = SBPenaltyModel(calib_json=args.calib_json)
    model.report()
