#!/usr/bin/env python3
"""
show_bench.py — human-readable summary of bench_methods.py results JSON.

Usage:
    python3 show_bench.py run-logs/bench_v1.json
    python3 show_bench.py run-logs/bench_v1.json --phase ap
    python3 show_bench.py run-logs/bench_v1.json --wm-timeline
    python3 show_bench.py run-logs/bench_v1.json --method "STMM+Proactive"
"""

import argparse, json, sys, statistics

# ── helpers ───────────────────────────────────────────────────────────────────

def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s)-1, int(len(s)*p/100))], 2)

def _avg(vals):
    return round(statistics.mean(vals), 2) if vals else None

def _cpu_from_timeline(samples: list) -> dict:
    if not samples:
        return {}
    out = {}
    for key in ("iowait_pct", "user_pct", "system_pct", "idle_pct"):
        vals = [s[key] for s in samples if key in s]
        if not vals:
            continue
        out[f"{key}_avg"] = _avg(vals)
        out[f"{key}_p50"] = _pct(vals, 50)
        out[f"{key}_p95"] = _pct(vals, 95)
        out[f"{key}_max"] = _pct(vals, 100)
    return out

def _phase_stats(r: dict, phase: str) -> dict:
    """Return stats dict for the given phase.
    Handles both old format (AP fields at top-level) and new (ap_phase subdict).
    """
    key = f"{phase}_phase"
    if key in r:
        return r[key]
    if phase == "ap":
        ap_keys = [
            "tps_overall", "qps", "txn_total", "lat_min_ms", "lat_avg_ms",
            "lat_max_ms", "lat_p95_ms",
            "iowait_pct_avg", "iowait_pct_p50", "iowait_pct_p95", "iowait_pct_max",
            "user_pct_avg", "system_pct_avg", "idle_pct_avg",
            "ap_blks_hit_total", "ap_blks_read_total", "ap_temp_bytes_total",
            "ap_blks_read_per_s", "ap_hit_ratio", "tps_avg", "tps_median",
            "ap_lat_p95_ms", "ap_lat_avg_ms",
        ]
        return {k: r[k] for k in ap_keys if k in r}
    return {}

def _cpu_stats(r: dict, phase: str) -> dict:
    tl = r.get(f"{phase}_cpu", [])
    if tl:
        return _cpu_from_timeline(tl)
    pd = _phase_stats(r, phase)
    return {k: pd[k] for k in pd if k.startswith("iowait_") or k.startswith("user_")
            or k.startswith("system_") or k.startswith("idle_")}

def _hit_ratio(r: dict, phase: str):
    """Return buffer hit ratio for a phase. Falls back to top-level blks_delta for PRE."""
    pd = _phase_stats(r, phase)
    if "hit_ratio" in pd:
        return pd["hit_ratio"]
    if phase == "ap":
        return pd.get("ap_hit_ratio")
    if phase == "pre":
        dh = r.get("blks_hit_delta_pre", 0)
        dr = r.get("blks_read_delta_pre", 0)
        return round(dh / max(1, dh + dr), 4) if (dh + dr) > 0 else None
    return None

def _fmt(v, unit="", missing="—", prec=1):
    if v is None:
        return missing
    if isinstance(v, float):
        return f"{v:.{prec}f}{unit}"
    return f"{v}{unit}"

def _fmt4(v, missing="—"):
    if v is None:
        return missing
    return f"{v:.4f}"


# ── single-result display ─────────────────────────────────────────────────────

PHASES = ["pre", "pre2", "ap", "post"]
PHASE_LABELS = {"pre": "PRE (60s)", "pre2": "PRE2 (30s)",
                "ap": "AP (360s)", "post": "POST (180s)"}

def show_result(r: dict, show_phases=None, show_wm_tl=False):
    name = r.get("method", "?")
    wl   = r.get("workload", "?")
    print(f"\n{'━'*80}")
    print(f"  {name}  /  {wl}")
    print(f"{'━'*80}")
    print(f"  Config : WM={r.get('wm_applied')}MB  SB={r.get('sb_applied')}MB"
          f"  →  WM_final={r.get('wm_final')}MB")
    print()

    # ── per-phase table ──
    hdr = (f"  {'Phase':<12}  {'TPS(med)':>9}  {'hit_ratio':>9}  {'iowait_avg':>10}"
           f"  {'iowait_p95':>10}  {'lat_p95(ms)':>11}  {'lat_avg(ms)':>11}")
    sep = (f"  {'─'*12}  {'─'*9}  {'─'*9}  {'─'*10}"
           f"  {'─'*10}  {'─'*11}  {'─'*11}")
    print(hdr)
    print(sep)

    tps_map = {"pre": r.get("pre_tps"), "pre2": r.get("pre2_tps"),
               "ap":  r.get("ap_tps"),  "post": r.get("post_tps")}
    for ph in PHASES:
        tps     = tps_map.get(ph)
        cpu     = _cpu_stats(r, ph)
        pd      = _phase_stats(r, ph)
        hit     = _hit_ratio(r, ph)
        iow_avg = cpu.get("iowait_pct_avg")
        iow_p95 = cpu.get("iowait_pct_p95")
        lat_p95 = pd.get("lat_p95_ms")
        lat_avg = pd.get("lat_avg_ms")
        print(f"  {PHASE_LABELS[ph]:<12}  {_fmt(tps):>9}  {_fmt4(hit):>9}"
              f"  {_fmt(iow_avg,'%'):>10}  {_fmt(iow_p95,'%'):>10}"
              f"  {_fmt(lat_p95,'ms'):>11}  {_fmt(lat_avg,'ms'):>11}")

    # ── outcome ──
    print()
    print(f"  drop%     = {_fmt(r.get('drop_pct'),'%')}   (pre2→ap TPS degradation)")
    print(f"  recovery% = {_fmt(r.get('recovery_pct'),'%')}   (post vs pre2 baseline)")

    # ── AP IO / sort detail ──
    ap = _phase_stats(r, "ap")
    if ap:
        print()
        print(f"  ── AP I/O ──")
        temp_gb = (ap.get("ap_temp_bytes_total") or 0) / 1024**3
        print(f"  sort spill   : {temp_gb:.2f} GB")
        print(f"  blks_read/s  : {_fmt(ap.get('ap_blks_read_per_s'))}")
        if ap.get("ap_lat_p95_ms"):
            print(f"  AP lat_p95   : {ap['ap_lat_p95_ms']}ms")
            print(f"  AP lat_avg   : {ap.get('ap_lat_avg_ms','—')}ms")

    # ── WM timeline (optional) ──
    if show_wm_tl:
        wt = r.get("wm_timeline", [])
        if wt:
            print()
            print(f"  ── WM/IO timeline (every ~15s during AP) ──")
            print(f"  {'t_rel':>6}  {'WM':>5}  {'SB':>6}  {'blks_r/s':>9}"
                  f"  {'hit_ratio':>9}  {'iowait':>7}  {'temp(MB)':>9}")
            print(f"  {'─'*6}  {'─'*5}  {'─'*6}  {'─'*9}  {'─'*9}  {'─'*7}  {'─'*9}")
            t0 = wt[0]["t"]
            for e in wt:
                t_rel   = round(e["t"] - t0)
                temp_mb = round((e.get("temp_bytes") or 0) / 1024**2, 1)
                print(f"  {t_rel:>6}  {e.get('wm_mb','?'):>5}  {e.get('sb_mb','?'):>6}"
                      f"  {_fmt(e.get('blks_read_per_s')):>9}"
                      f"  {_fmt4(e.get('hit_ratio')):>9}"
                      f"  {_fmt(e.get('iowait_pct_now'),'%'):>7}  {temp_mb:>9}")

    # ── full per-phase dump (optional) ──
    if show_phases:
        for ph in show_phases:
            pd  = _phase_stats(r, ph)
            cpu = _cpu_stats(r, ph)
            merged = {**pd, **cpu}
            if not merged:
                continue
            print()
            print(f"  ── {PHASE_LABELS.get(ph, ph)} full stats ──")
            for k, v in sorted(merged.items()):
                print(f"    {k:<42} {v:.4f}" if isinstance(v, float) else f"    {k:<42} {v}")


# ── summary table ─────────────────────────────────────────────────────────────

def show_summary(results: list):
    print(f"\n{'═'*108}")
    print("  SUMMARY")
    print(f"{'═'*108}")
    print(f"  {'Method':<28}  {'WM':>4}  {'SB':>5}  "
          f"{'pre2_tps':>8}  {'ap_tps':>7}  {'drop%':>6}  {'rec%':>6}  "
          f"{'hit(pre2)':>9}  {'hit(ap)':>7}  "
          f"{'iow_pre2':>8}  {'iow_ap':>7}  "
          f"{'spill_GB':>9}")
    print(f"  {'─'*28}  {'─'*4}  {'─'*5}  "
          f"{'─'*8}  {'─'*7}  {'─'*6}  {'─'*6}  "
          f"{'─'*9}  {'─'*7}  "
          f"{'─'*8}  {'─'*7}  "
          f"{'─'*9}")
    for r in results:
        ap   = _phase_stats(r, "ap")
        p2c  = _cpu_stats(r, "pre2")
        apc  = _cpu_stats(r, "ap")
        hit_pre2 = _hit_ratio(r, "pre2")
        hit_ap   = _hit_ratio(r, "ap")
        temp_gb  = (ap.get("ap_temp_bytes_total") or 0) / 1024**3
        print(f"  {r.get('method','?'):<28}  {r.get('wm_applied'):>4}  {r.get('sb_applied'):>5}  "
              f"{_fmt(r.get('pre2_tps')):>8}  {_fmt(r.get('ap_tps')):>7}  "
              f"{_fmt(r.get('drop_pct'),'%'):>6}  {_fmt(r.get('recovery_pct'),'%'):>6}  "
              f"{_fmt4(hit_pre2):>9}  {_fmt4(hit_ap):>7}  "
              f"{_fmt(p2c.get('iowait_pct_avg'),'%'):>8}  "
              f"{_fmt(apc.get('iowait_pct_avg'),'%'):>7}  "
              f"{temp_gb:>9.2f}")
    print()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Summarise bench_methods.py result JSON")
    ap.add_argument("json", help="Path to bench JSON (e.g. run-logs/bench_v1.json)")
    ap.add_argument("--phase", nargs="+", choices=PHASES,
                    help="Print full per-phase stats for these phases")
    ap.add_argument("--wm-timeline", action="store_true",
                    help="Print AP-phase WM/IO/iowait timeline for each result")
    ap.add_argument("--method", nargs="+",
                    help="Filter to these methods only")
    ap.add_argument("--workload", nargs="+",
                    help="Filter to these workloads only")
    args = ap.parse_args()

    with open(args.json) as f:
        data = json.load(f)

    results = data.get("results", [])
    if not results:
        print("No results in JSON yet.")
        sys.exit(0)

    if args.method:
        results = [r for r in results if r.get("method") in args.method]
    if args.workload:
        results = [r for r in results if r.get("workload") in args.workload]

    print(f"Bench run started : {data.get('started','?')}")
    print(f"Methods   : {data.get('methods')}")
    print(f"Workloads : {data.get('workloads')}")
    print(f"Completed : {len(results)} result(s)")

    show_summary(results)

    for r in results:
        show_result(r, show_phases=args.phase, show_wm_tl=args.wm_timeline)

    print()


if __name__ == "__main__":
    main()
