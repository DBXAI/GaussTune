#!/usr/bin/env python3
"""
Fair Method Comparison Harness
==============================
Runs Default / Expert / STMM variants through an IDENTICAL 7-step protocol
so drop% is comparable across methods.

Per-method protocol (per workload):
  1. RESET: kill_ap + drop_caches + restart DB at SB=baseline + warmup_s seconds.
     Done ONCE before each method's first phase.
  2. PRE 60s: TP-only at baseline (SB=1024, WM=64). Measures pre_tps.
  3. apply_pre(stats) -> (wm, sb). Method's recommendation step.
  4. RESTART: ALTER SYSTEM SET shared_buffers, work_mem; restart DB; warmup.
     drop_caches=False — OS page cache preserved so the only delta is the
     buffer-pool warm-up.
  5. PRE2 30s: TP-only at (wm, sb). Measures pre2_tps (fair baseline at the
     same SB/WM as AP phase).
  6. AP 360s: 4 concurrent AP workers + TP. Measures ap_tps.
  7. POST 180s: AP killed, TP-only. Measures post_tps.

  drop%      = (pre2_tps - ap_tps) / pre2_tps  × 100
  recovery%  = (post_tps - ap_tps) / (pre2_tps - ap_tps) × 100

Methods follow the protocol uniformly: even Default does step 4 (restart at
unchanged SB=1024/WM=64) so its buffer pool is reset the same way STMM's is.
"""

from __future__ import annotations
import argparse, json, os, re, subprocess, sys, threading, time
from datetime import datetime
from typing import Any

import db_helpers as db
from db_helpers import (
    AP_CONC, AP_DUR, GSQL, OMM_PASS, PRE_AP_S, POST_AP_S, SB_CMD, SB_MB, WM_INIT,
    apply_sb_change, check_cardinality_error, compact_memory, explain_ap_query,
    get_db_stats, gsql_q, kill_ap, launch_ap, measure_tps, omm_run, parse_row,
    read_ap_latency, read_meminfo, restart_db, set_guc, _read_w_await, _read_iowait_pct,
)
from stmm_controller import (
    BRBEController, ProactiveBRBEController, STMMController,
)
from workloads import WORKLOADS


PRE2_S = 30   # TP-only at applied (wm,sb)
WARMUP_RESET_S    = 420   # initial reset warmup (drop_caches=True)
WARMUP_APPLY_S    = 120   # post-apply warmup (drop_caches=False)


def _patch_ap_sql(workload: dict):
    """Set the module-level AP_SQL used by launch_ap()."""
    db.AP_SQL = workload["ap_sql"]


def _patch_log(run_log_path: str):
    """Repoint db_helpers' logger to our run log."""
    db._log_file.close()
    db._log_file = open(run_log_path, "w", buffering=1)
    db.LOG_PATH = run_log_path


def log(msg: str = ""):
    db.log(msg)


# ── Methods ───────────────────────────────────────────────────────────────────

class Method:
    """Each method returns (wm_mb, sb_mb) to apply at step 3.

    The protocol layer feeds methods only generic PRE-phase observations:
    cumulative blks_hit/blks_read deltas, mem_avail, current config, ap_sql.
    Any method-specific introspection (EXPLAIN, histograms, sampling, etc.)
    happens inside the method's own apply_pre body — the protocol does not
    pre-compute EXPLAIN, since static methods (Default, Expert) don't need it.

    Dynamic methods (controller-driven) override `make_ap_controller` to return
    a controller object that bench_methods will tick every STMM_POLL_S during
    step 6 (AP phase). Static methods return None and AP-phase WM stays fixed.
    """
    name: str

    def apply_pre(self, *, ap_sql: str,
                  blks_hit_delta: int, blks_read_delta: int,
                  pre_s: int, mem_avail_mb: int,
                  current_wm: int, current_sb: int) -> tuple[int, int]:
        raise NotImplementedError

    def make_ap_controller(self, wm_init: int, sb_init: int):
        """Override to return a controller with .tick(blks_hit_d, blks_read_d, temp_d, n_ap).
        Return None to disable AP-phase ticking (static methods)."""
        return None


class DefaultMethod(Method):
    name = "Default"

    def apply_pre(self, *, current_wm: int, current_sb: int, **_) -> tuple[int, int]:
        return current_wm, current_sb


class ExpertWMMethod(Method):
    """Static WM, SB unchanged."""
    def __init__(self, wm_mb: int):
        self.wm_mb = wm_mb
        self.name = f"Expert-WM{wm_mb}"

    def apply_pre(self, *, current_sb: int, **_) -> tuple[int, int]:
        return self.wm_mb, current_sb


class ExpertFullMethod(Method):
    def __init__(self, wm_mb: int, sb_mb: int):
        self.wm_mb = wm_mb
        self.sb_mb = sb_mb
        self.name = f"Expert-Full(WM{wm_mb}+SB{sb_mb})"

    def apply_pre(self, **_) -> tuple[int, int]:
        return self.wm_mb, self.sb_mb


class STMMProactiveMethod(Method):
    """ProactiveBRBE: introspect AP query via EXPLAIN (3-tier priority in
    explain_ap_query: workloads.json override → multi-node EXPLAIN scan →
    fallback), feed (rows, width) into _mimo_simulate, then keep ticking
    through AP phase to adjust WM (SB left alone)."""
    name = "STMM+ProactiveBRBE"

    def __init__(self):
        self._ctrl: ProactiveBRBEController | None = None

    def apply_pre(self, *, ap_sql: str,
                  blks_hit_delta: int, blks_read_delta: int,
                  pre_s: int, mem_avail_mb: int,
                  current_wm: int, current_sb: int, **_) -> tuple[int, int]:
        # Method-specific: introspect the AP query plan to get the WM-pressure
        # node's rows × width. Default/Expert don't need this.
        # Note: explain_ap_query reads db.AP_SQL (set in run_method_on_workload
        # via _patch_ap_sql) and follows the 3-tier priority documented in
        # db_helpers.explain_ap_query.
        ap_rows, ap_width = explain_ap_query()

        ctrl = ProactiveBRBEController(
            wm_init_mb=current_wm, sb_init_mb=current_sb,
            poll_s=15, n_ap_workers=AP_CONC)
        total_budget_mb = int((mem_avail_mb + current_sb) * 0.60)
        wm_rec, sb_rec = ctrl.predict_pre_ap(
            ap_rows, ap_width, blks_hit_delta, blks_read_delta,
            n_ap_workers=AP_CONC, total_budget_mb=total_budget_mb, pre_s=pre_s)
        entry = ctrl.log[-1]
        log(f"  [STMM+Proactive] rows={ap_rows} width={ap_width}B "
            f"→ WM_rec={wm_rec}MB  SB_rec={sb_rec}MB  "
            f"(budget={total_budget_mb}MB  input={entry['input_mb']}MB  "
            f"tp_ws={entry['tp_ws_mb']}MB  B_total={entry['B_total']:.4f}  "
            f"iters={entry['iters_used']})")
        # Stash for AP-phase ticking. The controller's internal wm_mb has
        # already been seeded by predict_pre_ap to wm_rec.
        self._ctrl = ctrl
        return wm_rec, sb_rec

    def make_ap_controller(self, wm_init: int, sb_init: int):
        # Sync the controller's view of applied state in case step 4 clamped
        # the recommendation differently (defensive).
        if self._ctrl is not None:
            self._ctrl.wm_mb = float(wm_init)
            self._ctrl.sb_mb = float(sb_init)
        return self._ctrl


METHODS_BY_NAME = {
    "Default":      lambda: DefaultMethod(),
    "Expert-WM":    lambda: ExpertWMMethod(wm_mb=256),
    "Expert-Full":  lambda: ExpertFullMethod(wm_mb=256, sb_mb=1024),
    "STMM+Proactive": lambda: STMMProactiveMethod(),
}


# ── Protocol helpers ──────────────────────────────────────────────────────────

def full_reset(sb_target: int, warmup_s: int):
    """Step 1: drop_caches + restart at SB=baseline + long warmup."""
    kill_ap()
    set_guc("shared_buffers", f"{sb_target}MB")
    set_guc("work_mem", f"{WM_INIT}MB")
    log(f"  [Reset] drop_caches + restart at SB={sb_target}MB + warmup {warmup_s}s ...")
    compact_memory(drop_caches=True)
    ok = restart_db()
    if not ok:
        log("  WARNING: restart_db failed in full_reset")
    gsql_q("SELECT pg_stat_reset();", db="sbtest", timeout=15)
    omm_run(SB_CMD.format(duration=warmup_s), timeout=warmup_s + 30)


def apply_config_restart(wm_mb: int, sb_mb: int, warmup_s: int):
    """Step 4: restart at (wm, sb) without drop_caches."""
    set_guc("shared_buffers", f"{sb_mb}MB")
    set_guc("work_mem", f"{wm_mb}MB")
    log(f"  [Apply] restart at WM={wm_mb}MB SB={sb_mb}MB (no drop_caches) "
        f"+ warmup {warmup_s}s ...")
    compact_memory(drop_caches=False)
    ok = restart_db()
    if not ok:
        log("  WARNING: restart_db failed in apply_config_restart")
    gsql_q("SELECT pg_stat_reset();", db="sbtest", timeout=15)
    omm_run(SB_CMD.format(duration=warmup_s), timeout=warmup_s + 30)


def _read_cpu_jiffies() -> dict:
    """Read /proc/stat cpu aggregate line. Returns dict of fields in jiffies."""
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.split()
                    # cpu user nice system idle iowait irq softirq steal guest guest_nice
                    keys = ["user", "nice", "system", "idle", "iowait",
                            "irq", "softirq", "steal", "guest", "guest_nice"]
                    vals = [int(x) for x in parts[1:1+len(keys)]]
                    return dict(zip(keys, vals))
    except Exception:
        pass
    return {}


def _cpu_stats_summary(start: dict, end: dict) -> dict:
    """Return percentages for user/system/iowait/idle/etc over the window."""
    if not start or not end:
        return {}
    keys = ["user", "nice", "system", "idle", "iowait",
            "irq", "softirq", "steal", "guest", "guest_nice"]
    deltas = {k: max(0, end.get(k, 0) - start.get(k, 0)) for k in keys}
    total = sum(deltas.values())
    if total <= 0:
        return {}
    return {f"cpu_{k}_pct": round(100.0 * v / total, 2) for k, v in deltas.items()}


def _parse_sysbench_summary(out: str) -> dict:
    """Parse sysbench's end-of-run 'General statistics' / 'Latency (ms)' block.

    Returns dict with keys: lat_avg_ms, lat_min_ms, lat_max_ms, lat_p95_ms,
    txn_total, qps, tps_overall. Empty dict if section not found.
    """
    # The block looks like:
    #   General statistics:
    #       total time:                          60.0123s
    #       total number of events:              19245
    #   Latency (ms):
    #            min:                                    2.10
    #            avg:                                    4.96
    #            max:                                  302.18
    #            95th percentile:                       11.04
    #            sum:                                95453.20
    #   Threads fairness:
    result: dict = {}
    in_lat = False
    for line in out.splitlines():
        s = line.strip()
        m = re.match(r"total number of events:\s*([\d.]+)", s)
        if m:
            result["txn_total"] = int(float(m.group(1)))
            continue
        m = re.match(r"queries:\s*\d+\s*\(([\d.]+)\s*per sec", s)
        if m:
            result["qps"] = float(m.group(1))
            continue
        m = re.match(r"transactions:\s*\d+\s*\(([\d.]+)\s*per sec", s)
        if m:
            result["tps_overall"] = float(m.group(1))
            continue
        if s.startswith("Latency (ms)"):
            in_lat = True
            continue
        if in_lat:
            if s.startswith("Threads fairness") or not s:
                in_lat = False
                continue
            m = re.match(r"min:\s*([\d.]+)", s)
            if m: result["lat_min_ms"] = float(m.group(1)); continue
            m = re.match(r"avg:\s*([\d.]+)", s)
            if m: result["lat_avg_ms"] = float(m.group(1)); continue
            m = re.match(r"max:\s*([\d.]+)", s)
            if m: result["lat_max_ms"] = float(m.group(1)); continue
            m = re.match(r"95th percentile:\s*([\d.]+)", s)
            if m: result["lat_p95_ms"] = float(m.group(1)); continue
    return result


def _start_cpu_sampler(samples: list, stop_evt: threading.Event,
                       interval_s: float = 1.0) -> threading.Thread:
    """Background thread sampling /proc/stat every interval_s. Appends
    {"t": wall_time, "iowait_pct": X, "user_pct": Y, "system_pct": Z} after
    computing deltas vs previous sample.
    """
    prev = _read_cpu_jiffies()
    def loop():
        nonlocal prev
        last_t = time.time()
        while not stop_evt.is_set():
            stop_evt.wait(interval_s)
            if stop_evt.is_set():
                break
            cur = _read_cpu_jiffies()
            now = time.time()
            window = _cpu_stats_summary(prev, cur)
            if window:
                samples.append({
                    "t":          round(now, 2),
                    "dt":         round(now - last_t, 2),
                    "iowait_pct": window.get("cpu_iowait_pct", 0.0),
                    "user_pct":   window.get("cpu_user_pct", 0.0),
                    "system_pct": window.get("cpu_system_pct", 0.0),
                    "idle_pct":   window.get("cpu_idle_pct", 0.0),
                })
            prev = cur
            last_t = now
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return th


# ── Huge-page / THP measurements ──────────────────────────────────────────────
#
# On 25GB-dataset machines without explicit huge pages, raising shared_buffers
# can cause TPS regressions because the buffer pool doesn't fit in the dTLB.
# To diagnose whether THP (Transparent Huge Pages) is helping or thrashing, we
# collect:
#   1. /proc/meminfo HugePages_* + AnonHugePages + ShmemHugePages (current state)
#   2. /proc/vmstat thp_* + compact_* counters (deltas show THP activity rate)
#   3. /sys/kernel/mm/transparent_hugepage/enabled,defrag (config snapshot)
#   4. /proc/<gaussdb>/smaps_rollup AnonHugePages (this PG instance's THP usage)
# ──────────────────────────────────────────────────────────────────────────────

_VMSTAT_THP_KEYS = (
    "thp_fault_alloc", "thp_fault_fallback", "thp_fault_fallback_charge",
    "thp_collapse_alloc", "thp_collapse_alloc_failed",
    "thp_split_page", "thp_split_page_failed",
    "thp_split_pmd", "thp_zero_page_alloc",
    "compact_stall", "compact_fail", "compact_success",
    "compact_migrate_scanned", "compact_free_scanned",
    "pgmajfault", "pgfault",
)

_MEMINFO_HP_KEYS = (
    "HugePages_Total", "HugePages_Free", "HugePages_Rsvd", "HugePages_Surp",
    "Hugepagesize", "Hugetlb",
    "AnonHugePages", "ShmemHugePages", "FileHugePages",
    "AnonPages", "Shmem", "Cached",
)


def _read_meminfo_huge() -> dict:
    """Snapshot of huge-page-relevant fields from /proc/meminfo, values in KiB."""
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                k = k.strip()
                if k in _MEMINFO_HP_KEYS:
                    parts = rest.strip().split()
                    if parts and parts[0].isdigit():
                        out[k] = int(parts[0])
    except Exception:
        pass
    return out


def _read_vmstat_thp() -> dict:
    """Snapshot of THP-related counters from /proc/vmstat (cumulative)."""
    out: dict[str, int] = {}
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] in _VMSTAT_THP_KEYS:
                    out[parts[0]] = int(parts[1])
    except Exception:
        pass
    return out


def _read_thp_config() -> dict:
    """Read /sys/kernel/mm/transparent_hugepage/* config knobs."""
    out: dict[str, str] = {}
    for knob in ("enabled", "defrag", "shmem_enabled"):
        path = f"/sys/kernel/mm/transparent_hugepage/{knob}"
        try:
            with open(path) as f:
                # files look like "always [madvise] never" — extract bracketed
                content = f.read().strip()
                m = re.search(r"\[(\w+)\]", content)
                out[f"thp_{knob}"] = m.group(1) if m else content
        except Exception:
            pass
    return out


def _read_gaussdb_smaps_rollup() -> dict:
    """Read the running gaussdb PID's smaps_rollup for memory breakdown.
    Includes AnonHugePages (THP), Rss, Pss, Shared/Private, FilePages, etc.
    Returns empty dict if no gaussdb process is found."""
    from db_helpers import get_gaussdb_pid
    pid = get_gaussdb_pid()
    if not pid:
        return {}
    out: dict[str, int] = {"_gaussdb_pid": pid}
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                # lines like "AnonHugePages:   1024 kB"
                m = re.match(r"(\w+):\s+(\d+)\s+kB", line)
                if not m:
                    continue
                k = m.group(1)
                if k in ("Rss", "Pss", "Shared_Clean", "Shared_Dirty",
                         "Private_Clean", "Private_Dirty", "Referenced",
                         "Anonymous", "AnonHugePages", "ShmemPmdMapped",
                         "Shared_Hugetlb", "Private_Hugetlb", "Swap",
                         "SwapPss", "Locked"):
                    out[f"gaussdb_{k.lower()}_kb"] = int(m.group(2))
    except Exception:
        pass
    return out


def _hp_snapshot() -> dict:
    """Single-shot composite snapshot: meminfo + vmstat + thp config + smaps."""
    snap = {}
    snap.update({f"mi_{k}_kb": v for k, v in _read_meminfo_huge().items()})
    snap.update({f"vm_{k}": v for k, v in _read_vmstat_thp().items()})
    snap.update(_read_thp_config())
    snap.update(_read_gaussdb_smaps_rollup())
    return snap


def _hp_phase_delta(start: dict, end: dict) -> dict:
    """Return per-phase delta of vmstat counters + final values for non-counters.
    Skips non-numeric fields (THP config strings, PIDs).

    Adds derived ratios that proxy for "huge-page addressing success/failure":
      - thp_alloc_success_rate: alloc / (alloc + fallback)   higher = better TLB coverage
      - thp_alloc_fallback_rate: fallback / (alloc + fallback)
      - compact_success_rate:  success / (success + fail)
      - gaussdb_thp_coverage:  AnonHugePages / Rss            fraction of gaussdb RSS on 2MB pages
      - gaussdb_small_page_kb: Rss - AnonHugePages            bytes still on 4KB (TLB-heavy)
    """
    out: dict = {}
    skip_prefixes = ("thp_enabled", "thp_defrag", "thp_shmem_enabled")
    for k, v in end.items():
        if k.startswith(skip_prefixes) or k == "_gaussdb_pid":
            out[k] = v
            continue
        if not isinstance(v, (int, float)):
            continue
        if k.startswith("vm_"):
            v0 = start.get(k, 0)
            out[k + "_delta"] = v - v0
        else:
            out[k] = v

    # Derived: THP alloc success/failure rates during this phase
    alloc    = out.get("vm_thp_fault_alloc_delta", 0)
    fallback = out.get("vm_thp_fault_fallback_delta", 0)
    fb_charge = out.get("vm_thp_fault_fallback_charge_delta", 0)
    total_thp_faults = alloc + fallback + fb_charge
    if total_thp_faults > 0:
        out["thp_alloc_success_rate"]  = round(alloc / total_thp_faults, 4)
        out["thp_alloc_fallback_rate"] = round((fallback + fb_charge) / total_thp_faults, 4)
        out["thp_alloc_attempts"]      = total_thp_faults

    # Derived: compaction success rate (these counters reflect how hard the
    # kernel had to work to find 2MB-contiguous regions to back THP).
    c_ok = out.get("vm_compact_success_delta", 0)
    c_no = out.get("vm_compact_fail_delta", 0)
    if c_ok + c_no > 0:
        out["compact_success_rate"] = round(c_ok / (c_ok + c_no), 4)
        out["compact_attempts"]     = c_ok + c_no

    # Derived: gaussdb's THP coverage (fraction of RSS sitting on 2MB pages)
    # Higher coverage → larger TLB reach → fewer TLB misses per query.
    gdb_rss = out.get("gaussdb_rss_kb", 0)
    gdb_thp = out.get("gaussdb_anonhugepages_kb", 0)
    if gdb_rss > 0:
        out["gaussdb_thp_coverage"]  = round(gdb_thp / gdb_rss, 4)
        out["gaussdb_small_page_kb"] = max(0, gdb_rss - gdb_thp)

    return out


def _summarize_cpu_timeline(samples: list) -> dict:
    """Aggregate per-second CPU samples to avg/p50/p95 per field."""
    if not samples:
        return {}
    out: dict = {}
    for key in ("iowait_pct", "user_pct", "system_pct", "idle_pct"):
        vals = sorted(s[key] for s in samples)
        n = len(vals)
        out[f"{key}_avg"] = round(sum(vals) / n, 2)
        out[f"{key}_p50"] = round(vals[n // 2], 2)
        out[f"{key}_p95"] = round(vals[min(n - 1, int(n * 0.95))], 2)
        out[f"{key}_max"] = round(vals[-1], 2)
    return out


def measure_phase(label: str, duration_s: int) -> tuple[float, dict, list]:
    """Run sysbench for duration_s.

    Returns (tps_median, phase_stats, cpu_timeline). phase_stats includes:
      - TPS: tps_median, tps_avg (from per-5s samples, last 80%)
      - Latency: lat_avg_ms, lat_min_ms, lat_max_ms, lat_p95_ms (from sysbench summary)
      - Throughput: txn_total, qps, tps_overall (from sysbench summary)
      - CPU: iowait_pct_{avg,p50,p95,max}, user_pct_..., system_pct_..., idle_pct_...
      - HugePages/THP: mi_AnonHugePages_kb, gaussdb_anonhugepages_kb, vm_thp_*_delta,
        vm_compact_*_delta, thp_enabled, thp_defrag (see _hp_snapshot for full list)
    """
    cpu_samples: list = []
    cpu_stop = threading.Event()
    cpu_th = _start_cpu_sampler(cpu_samples, cpu_stop)

    db_before = get_db_stats()
    hp_start = _hp_snapshot()
    t_start = time.time()
    out, _ = omm_run(SB_CMD.format(duration=duration_s), timeout=duration_s + 30)
    t_elapsed = max(0.01, time.time() - t_start)
    hp_end = _hp_snapshot()
    db_after = get_db_stats()

    cpu_stop.set()
    cpu_th.join(timeout=3)

    # Per-5s TPS samples from --report-interval=5
    samples = []
    for line in out.splitlines():
        m = re.search(r"thds:\s*\d+\s+tps:\s*([\d.]+)", line)
        if m:
            samples.append(float(m.group(1)))
    if not samples:
        log(f"  [{label}] NO TPS SAMPLES")
        return 0.0, {}, cpu_samples

    skip = max(1, int(len(samples) * 0.2))
    body = sorted(samples[skip:])
    median = body[len(body) // 2]
    avg = sum(body) / len(body)

    stats = _parse_sysbench_summary(out)
    stats.update(_summarize_cpu_timeline(cpu_samples))
    stats.update(_hp_phase_delta(hp_start, hp_end))
    stats["tps_avg"] = round(avg, 2)
    stats["tps_median"] = round(median, 2)
    stats["tps_samples_n"] = len(samples)

    dh = max(0, db_after[0] - db_before[0])
    dr = max(0, db_after[1] - db_before[1])
    stats["blks_hit_per_s"]  = round(dh / t_elapsed, 1)
    stats["blks_read_per_s"] = round(dr / t_elapsed, 1)
    stats["hit_ratio"]       = round(dh / max(1, dh + dr), 4)

    lat_p95 = stats.get("lat_p95_ms", "?")
    iow = stats.get("iowait_pct_avg", "?")
    thp_alloc = stats.get("vm_thp_fault_alloc_delta", "?")
    thp_fb = stats.get("vm_thp_fault_fallback_delta", "?")
    gdb_thp = stats.get("gaussdb_anonhugepages_kb", "?")
    log(f"  [{label}] {duration_s}s  n={len(samples)} (skip={skip})  "
        f"tps med={median:.1f} avg={avg:.1f}  "
        f"lat_p95={lat_p95}ms  iowait_avg={iow}%  "
        f"thp_alloc={thp_alloc} fb={thp_fb}  gdb_thp={gdb_thp}kB")
    return round(median, 2), stats, cpu_samples


STMM_POLL_S = 15   # AP-phase controller tick interval


def measure_ap_phase(duration_s: int, controller=None,
                     wm_state: list | None = None,
                     sb_state: list | None = None) -> tuple[float, dict, list, list]:
    """AP+TP for duration_s.

    Three concurrent activities:
      1. Sysbench TP load (foreground) — drives the TP TPS we report
      2. AP workers (background) — 4 processes that issue AP_SQL repeatedly
      3. Tick thread (every STMM_POLL_S): samples DB stats. If controller is
         provided, calls controller.tick(...) and applies recommended WM via
         ALTER DATABASE work_mem. Records (t, wm, sb, dh, dr, dt, blks_read/s)
         in wm_timeline. SB suggestions are IGNORED in AP phase (no restart).
      4. CPU sampler (every 1s) — iowait/user/system/idle %

    Returns (tps_median, ap_stats, wm_timeline, cpu_timeline). ap_stats includes
    sysbench latency summary, CPU summary, AP query latency (from AP_LAT_LOG),
    and total AP-phase blks_read/blks_hit deltas.
    """
    launch_ap(duration_s)
    wm_timeline: list[dict] = []
    cpu_samples: list = []

    stop_evt = threading.Event()
    cpu_stop = threading.Event()
    prev_stats = list(get_db_stats())
    ap_start_stats = list(prev_stats)

    cpu_th = _start_cpu_sampler(cpu_samples, cpu_stop)
    hp_start = _hp_snapshot()

    def tick_thread():
        last_t = time.time()
        while not stop_evt.is_set():
            stop_evt.wait(STMM_POLL_S)
            if stop_evt.is_set():
                break
            try:
                hit, read, temp = get_db_stats()
                now = time.time()
                dt_wall = max(0.01, now - last_t)
                dh = hit  - prev_stats[0]
                dr = read - prev_stats[1]
                dtemp = temp - prev_stats[2]
                prev_stats[0], prev_stats[1], prev_stats[2] = hit, read, temp
                last_t = now

                new_wm = wm_state[0] if wm_state is not None else None
                suggest_sb = None
                iowait_pct = _read_iowait_pct()
                if controller is not None:
                    new_wm, suggest_sb = controller.tick(
                        dh, dr, dtemp, AP_CONC, iowait_pct_now=iowait_pct)
                    if wm_state is not None and new_wm != wm_state[0]:
                        log(f"  [AP-tick] WM {wm_state[0]} → {new_wm}MB  "
                            f"(dh={dh} dr={dr} dtemp={dtemp} iowait={iowait_pct:.1f}%)")
                        set_guc("work_mem", f"{new_wm}MB")
                        wm_state[0] = new_wm
                wm_timeline.append({
                    "t":                round(now, 2),
                    "dt":               round(dt_wall, 2),
                    "wm_mb":            new_wm,
                    "sb_mb":            sb_state[0] if sb_state is not None else None,
                    "blks_hit":         dh,
                    "blks_read":        dr,
                    "temp_bytes":       dtemp,
                    "blks_read_per_s":  round(dr / dt_wall, 1),
                    "blks_hit_per_s":   round(dh / dt_wall, 1),
                    "hit_ratio":        round(dh / max(1, dh + dr), 4),
                    "iowait_pct_now":   round(iowait_pct, 2),
                })
            except Exception as e:
                log(f"  [AP-tick] error: {e}")

    t = threading.Thread(target=tick_thread, daemon=True)
    t.start()

    out, _ = omm_run(SB_CMD.format(duration=duration_s), timeout=duration_s + 30)

    stop_evt.set()
    t.join(timeout=STMM_POLL_S + 5)
    cpu_stop.set()
    cpu_th.join(timeout=3)

    # Final AP-phase DB deltas
    end_stats = get_db_stats()
    hp_end = _hp_snapshot()
    ap_dh_total = end_stats[0] - ap_start_stats[0]
    ap_dr_total = end_stats[1] - ap_start_stats[1]
    ap_dt_total = end_stats[2] - ap_start_stats[2]

    samples = []
    for line in out.splitlines():
        m = re.search(r"thds:\s*\d+\s+tps:\s*([\d.]+)", line)
        if m:
            samples.append(float(m.group(1)))
    kill_ap()
    # AP query latency from launch_ap's log
    ap_lat = read_ap_latency()

    # Sysbench end-of-run summary (TP)
    sb_summary = _parse_sysbench_summary(out)
    cpu_summary = _summarize_cpu_timeline(cpu_samples)
    hp_summary  = _hp_phase_delta(hp_start, hp_end)

    if not samples:
        log("  [AP] NO TPS SAMPLES")
        ap_stats: dict[str, Any] = {
            **sb_summary, **cpu_summary, **hp_summary, **ap_lat,
            "ap_blks_hit_total":   ap_dh_total,
            "ap_blks_read_total":  ap_dr_total,
            "ap_temp_bytes_total": ap_dt_total,
            "ap_blks_read_per_s":  round(ap_dr_total / max(1, duration_s), 1),
            "ap_hit_ratio":        round(ap_dh_total / max(1, ap_dh_total + ap_dr_total), 4),
        }
        return 0.0, ap_stats, wm_timeline, cpu_samples

    skip = max(1, int(len(samples) * 0.2))
    body = sorted(samples[skip:])
    median = body[len(body) // 2]
    avg = sum(body) / len(body)

    ap_stats = {
        **sb_summary,
        **cpu_summary,
        **hp_summary,
        **ap_lat,
        "tps_avg":             round(avg, 2),
        "tps_median":          round(median, 2),
        "tps_samples_n":       len(samples),
        "ap_blks_hit_total":   ap_dh_total,
        "ap_blks_read_total":  ap_dr_total,
        "ap_temp_bytes_total": ap_dt_total,
        "ap_blks_read_per_s":  round(ap_dr_total / max(1, duration_s), 1),
        "ap_hit_ratio":        round(ap_dh_total / max(1, ap_dh_total + ap_dr_total), 4),
    }

    lat_p95 = sb_summary.get("lat_p95_ms", "?")
    iow = cpu_summary.get("iowait_pct_avg", "?")
    thp_succ = hp_summary.get("thp_alloc_success_rate", "?")
    thp_fb = hp_summary.get("thp_alloc_fallback_rate", "?")
    gdb_cov = hp_summary.get("gaussdb_thp_coverage", "?")
    log(f"  [AP] {duration_s}s  n={len(samples)} (skip={skip})  "
        f"tps med={median:.1f} avg={avg:.1f}  "
        f"tp_lat_p95={lat_p95}ms  iowait_avg={iow}%  "
        f"ap_blks_read/s={ap_stats['ap_blks_read_per_s']}  "
        f"ap_lat_p95={ap_lat.get('ap_lat_p95_ms', '?')}ms  "
        f"thp_succ={thp_succ} fb={thp_fb}  gdb_cov={gdb_cov}")
    return round(median, 2), ap_stats, wm_timeline, cpu_samples


# ── Per-method per-workload run ───────────────────────────────────────────────

def run_method_on_workload(method: Method, workload: dict, t0) -> dict:
    name = method.name
    wl   = workload["name"]
    log(f"\n{'#' * 80}")
    log(f"METHOD: {name}   WORKLOAD: {wl}")
    log(f"{'#' * 80}")
    _patch_ap_sql(workload)

    # Step 1: full reset
    full_reset(sb_target=SB_MB, warmup_s=WARMUP_RESET_S)

    # Step 2: PRE 60s at baseline
    log(f"  [PRE  ] {PRE_AP_S}s TP-only at SB={SB_MB}MB WM={WM_INIT}MB ...")
    pre_stats_before = get_db_stats()
    pre_t0 = time.time()
    pre_tps, pre_phase, pre_cpu = measure_phase("PRE", PRE_AP_S)
    pre_stats_after = get_db_stats()
    blks_hit_delta  = pre_stats_after[0] - pre_stats_before[0]
    blks_read_delta = pre_stats_after[1] - pre_stats_before[1]
    pre_dur = time.time() - pre_t0
    pre_phase["blks_hit_per_s"]  = round(blks_hit_delta  / max(1, pre_dur), 1)
    pre_phase["blks_read_per_s"] = round(blks_read_delta / max(1, pre_dur), 1)
    pre_phase["hit_ratio"] = round(blks_hit_delta /
                                   max(1, blks_hit_delta + blks_read_delta), 4)

    # Step 3: method.apply_pre(stats) — protocol passes only generic PRE-phase
    # observations. Methods that need query introspection (EXPLAIN, sampling)
    # do it themselves inside apply_pre.
    mem_avail = read_meminfo().get("mem_avail_mb", 8192)
    wm_rec, sb_rec = method.apply_pre(
        ap_sql=workload["ap_sql"],
        blks_hit_delta=blks_hit_delta, blks_read_delta=blks_read_delta,
        pre_s=PRE_AP_S, mem_avail_mb=mem_avail,
        current_wm=WM_INIT, current_sb=SB_MB,
    )
    log(f"  [{name}] recommendation: WM={wm_rec}MB  SB={sb_rec}MB")

    # Step 4: restart at (wm, sb) — ALWAYS, even if unchanged
    apply_config_restart(wm_mb=wm_rec, sb_mb=sb_rec, warmup_s=WARMUP_APPLY_S)

    # Step 5: PRE2 30s at applied config — fair drop baseline
    log(f"  [PRE2 ] {PRE2_S}s TP-only at WM={wm_rec}MB SB={sb_rec}MB ...")
    pre2_tps, pre2_phase, pre2_cpu = measure_phase("PRE2", PRE2_S)

    # Step 6: AP 360s — if method provides an AP-phase controller, tick it.
    wm_state = [wm_rec]
    sb_state = [sb_rec]
    ap_ctrl = method.make_ap_controller(wm_init=wm_rec, sb_init=sb_rec)
    # Record TP-only iowait baseline at the applied SB for write-IO penalty model.
    # Measured after PRE2 (TP-only at applied config) so it reflects actual write
    # pressure at sb_rec — ensures delta_iowait = 0 at AP start, rises only with
    # AP-induced disk contention.
    if ap_ctrl is not None and hasattr(ap_ctrl, "set_iowait_baseline"):
        iowait_base = _read_iowait_pct()
        ap_ctrl.set_iowait_baseline(iowait_base)
        log(f"  [{name}] iowait baseline = {iowait_base:.1f}% (TP-only at SB={sb_rec}MB)")
    if ap_ctrl is not None:
        log(f"  [AP   ] {AP_DUR}s AP+TP ({AP_CONC} AP workers)  "
            f"controller={type(ap_ctrl).__name__} ticking every {STMM_POLL_S}s ...")
    else:
        log(f"  [AP   ] {AP_DUR}s AP+TP ({AP_CONC} AP workers)  static (no tick) ...")
    ap_tps, ap_stats, wm_timeline, ap_cpu = measure_ap_phase(
        AP_DUR, controller=ap_ctrl, wm_state=wm_state, sb_state=sb_state)
    wm_final = wm_state[0]

    # Step 7: POST 180s TP-only
    log(f"  [POST ] {POST_AP_S}s TP-only ...")
    post_tps, post_phase, post_cpu = measure_phase("POST", POST_AP_S)

    # Cardinality self-correction: compare planner's row estimate against the
    # actual n_returned_rows recorded in dbe_perf.statement_history during the
    # AP phase. If error > 15%, update workloads.json so the NEXT run uses the
    # corrected (override) value via explain_ap_query priority 1.
    try:
        check_cardinality_error()
    except Exception as e:
        log(f"  [CardCheck] skipped: {e}")

    # Derived metrics
    drop_pct = round((pre2_tps - ap_tps) / pre2_tps * 100, 1) if pre2_tps > 0 else None
    rec_pct  = (round((post_tps - ap_tps) / (pre2_tps - ap_tps) * 100, 1)
                if pre2_tps > ap_tps else None)

    result = {
        "method":      name,
        "workload":    wl,
        "wm_applied":  wm_rec,
        "sb_applied":  sb_rec,
        "wm_final":    wm_final,
        "pre_tps":     pre_tps,
        "pre2_tps":    pre2_tps,
        "ap_tps":      ap_tps,
        "post_tps":    post_tps,
        "drop_pct":    drop_pct,
        "recovery_pct": rec_pct,
        "blks_hit_delta_pre":  blks_hit_delta,
        "blks_read_delta_pre": blks_read_delta,
        "pre_dur_s":   round(pre_dur, 1),
        "elapsed_min": round((time.time() - t0) / 60.0, 1),
        "wm_timeline": wm_timeline,
        # Per-phase rich stats (sysbench latency + CPU iowait/user/system + IO):
        "pre_phase":   pre_phase,
        "pre2_phase":  pre2_phase,
        "ap_phase":    ap_stats,
        "post_phase":  post_phase,
        # Per-second CPU timelines (sampled every 1s):
        "pre_cpu":     pre_cpu,
        "pre2_cpu":    pre2_cpu,
        "ap_cpu":      ap_cpu,
        "post_cpu":    post_cpu,
    }
    log(f"  → {name}/{wl}: pre={pre_tps}  pre2={pre2_tps}  ap={ap_tps}  post={post_tps}  "
        f"drop={drop_pct}%  rec={rec_pct}%  wm_final={wm_final}MB")
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", nargs="+",
                    default=list(METHODS_BY_NAME.keys()),
                    help="Method names to compare. Default: all.")
    ap.add_argument("--workloads", nargs="+",
                    default=[w["name"] for w in WORKLOADS],
                    help="Workload names. Default: all in workloads.json.")
    ap.add_argument("--out", required=True,
                    help="Output JSON path (e.g. run-logs/bench_v0.json)")
    ap.add_argument("--log", required=True,
                    help="Log path (e.g. run-logs/bench_v0.log)")
    args = ap.parse_args()

    _patch_log(args.log)
    t0 = time.time()
    log(f"Bench Methods Harness — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Methods   : {args.methods}")
    log(f"Workloads : {args.workloads}")
    log(f"Protocol  : reset(420s) → PRE({PRE_AP_S}s) → apply → restart({WARMUP_APPLY_S}s)"
        f" → PRE2({PRE2_S}s) → AP({AP_DUR}s) → POST({POST_AP_S}s)")
    log(f"Output    : {args.out}")

    # Apply bgwriter_delay=200ms once for all methods (upstream protocol).
    # OpenGauss default 2000ms causes write-IO spikes at larger SB; 200ms (PG
    # default) lets bgwriter continuously flush, smoothing w_await curve.
    log("Setting bgwriter_delay=200ms (uniform for all methods)...")
    set_guc("bgwriter_delay", "200")

    results: list[dict] = []
    for wl in args.workloads:
        workload = next((w for w in WORKLOADS if w["name"] == wl), None)
        if workload is None:
            log(f"WARN: workload '{wl}' not in workloads.json — skipping")
            continue
        for m_name in args.methods:
            if m_name not in METHODS_BY_NAME:
                log(f"WARN: method '{m_name}' not registered — skipping")
                continue
            method = METHODS_BY_NAME[m_name]()
            try:
                r = run_method_on_workload(method, workload, t0)
                results.append(r)
            except Exception as e:
                import traceback
                log(f"ERROR in {m_name}/{wl}: {e}\n{traceback.format_exc()}")
            # Save after each (method, workload) pair
            with open(args.out, "w") as f:
                json.dump({
                    "started":  datetime.fromtimestamp(t0).isoformat(),
                    "methods":  args.methods,
                    "workloads": args.workloads,
                    "results":  results,
                }, f, indent=2)

    log(f"\nAll done. Total: {(time.time() - t0) / 60.0:.1f} min")
    log(f"Results → {args.out}")
    _print_summary(results)


def _print_summary(results: list[dict]):
    log("\n" + "=" * 96)
    log("SUMMARY")
    log("=" * 96)
    log(f"  {'Workload':<10}  {'Method':<24}  {'WM':>5}  {'SB':>5}  "
        f"{'pre':>7}  {'pre2':>7}  {'ap':>7}  {'post':>7}  {'drop%':>6}  {'rec%':>6}")
    log("  " + "-" * 94)
    for r in results:
        log(f"  {r['workload']:<10}  {r['method']:<24}  "
            f"{r['wm_applied']:>5}  {r['sb_applied']:>5}  "
            f"{r['pre_tps']:>7}  {r['pre2_tps']:>7}  "
            f"{r['ap_tps']:>7}  {r['post_tps']:>7}  "
            f"{(r['drop_pct'] if r['drop_pct'] is not None else 'NA'):>6}  "
            f"{(r['recovery_pct'] if r['recovery_pct'] is not None else 'NA'):>6}")


if __name__ == "__main__":
    main()
