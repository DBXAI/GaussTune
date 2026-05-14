#!/usr/bin/env python3
"""
memory_tuner.py — SB w_await penalty model for GaussTune
=========================================================

Loads sb_calib JSON and provides:
  - w_await interpolation for any SB level
  - SB penalty factor (for use in _sb_benefit_brbe)
  - Standalone report of calib curve

bgwriter tuning is NOT done here — bgwriter_delay=200ms is applied once
at experiment startup in stmm_test.py (uniform across all methods).

Integration:
  from memory_tuner import SBPenaltyModel
  model = SBPenaltyModel(calib_json="run-logs/sb_calib6.json")
  w_await = model.w_await_at(sb_mb=3072)
  penalty = model.io_penalty(sb_mb=3072, w_await_now=25.0)
"""

import os, json, math, argparse
from datetime import datetime

# ── Constants ─────────────────────────────────────────────────────────────────
W_AWAIT_BASE_MS  = 12.0   # calib6 SB=1024MB baseline w_await
IO_PENALTY_WEIGHT = 1.0   # tunable: scale of write-penalty vs read-benefit


# ── SBPenaltyModel ────────────────────────────────────────────────────────────
class SBPenaltyModel:
    """
    Loads sb_calib JSON and provides w_await interpolation + IO penalty factor.

    Used by BRBEController._sb_benefit_brbe() to subtract write-IO cost from
    the read-IO benefit of increasing shared_buffers.
    """

    def __init__(self, calib_json: str | None = None, log_fn=None):
        self.log = log_fn or (lambda msg: print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"))
        # List of (sb_mb, w_await_ms) sorted by sb_mb
        self._curve: list[tuple[int, float]] = []

        if calib_json and os.path.exists(calib_json):
            self._load(calib_json)
        else:
            self.log("[SBPenaltyModel] calib JSON not found — penalty model disabled")

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
        """Interpolate w_await_ms at the given SB level from calib curve."""
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

    def io_penalty(self, sb_mb: int, w_await_now: float) -> float:
        """
        Write-IO penalty per MB of SB.

        Formula:
          w_await_calib = interpolated calib value at sb_mb (pure-TP baseline)
          w_await_actual = w_await_calib × (w_await_now / W_AWAIT_BASE_MS)
                         → scales up when current disk is busier than calib
          excess_ratio = max(0, w_await_actual - W_AWAIT_BASE_MS) / W_AWAIT_BASE_MS
          penalty = excess_ratio × IO_PENALTY_WEIGHT / sb_mb

        The penalty is subtracted from read-benefit in _sb_benefit_brbe,
        so STMM naturally stops growing SB when write cost exceeds read gain.
        """
        if not self._curve or sb_mb <= 0:
            return 0.0
        w_calib_base = self._curve[0][1]   # w_await at smallest tested SB
        w_calib_sb   = self.w_await_at(sb_mb)
        # Scale calib value by current disk pressure relative to calib baseline
        load_factor  = w_await_now / w_calib_base if w_calib_base > 0 else 1.0
        w_actual     = w_calib_sb * load_factor
        excess       = max(0.0, w_actual - W_AWAIT_BASE_MS)
        return (excess / W_AWAIT_BASE_MS) * IO_PENALTY_WEIGHT / sb_mb

    def report(self):
        """Print calib curve."""
        if not self._curve:
            print("  [SBPenaltyModel] No data loaded.")
            return
        print("\n── SB w_await Penalty Curve ─────────────────────────────")
        print(f"  {'SB(MB)':>8}  {'w_await(ms)':>12}  {'penalty@now=12ms':>18}  {'penalty@now=25ms':>18}")
        print(f"  {'─'*8}  {'─'*12}  {'─'*18}  {'─'*18}")
        for sb, w in self._curve:
            p12 = self.io_penalty(sb, 12.0)
            p25 = self.io_penalty(sb, 25.0)
            print(f"  {sb:>8}  {w:>12.1f}  {p12:>18.6f}  {p25:>18.6f}")
        print("─────────────────────────────────────────────────────────\n")


# ── Standalone ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SB penalty model report")
    parser.add_argument("--calib-json", default="run-logs/sb_calib6.json")
    args = parser.parse_args()
    model = SBPenaltyModel(calib_json=args.calib_json)
    model.report()

