#!/usr/bin/env python3
"""
GaussTune v3 — Memory-bottleneck tuning with diverse AP query forms.

Story:
  Memory IS the bottleneck. Default SB (2GB) < dataset (4GB) → cache misses
  dominate TP degradation. Tuning SB to near-dataset-size AND WM to the AP
  query's no-spill threshold jointly minimizes TPS degradation.

Three AP query forms (distinct memory profiles):
  sort_light  : partial sort (id<=300K) by c+k   (light,  M1 ~64-128MB)
  sort_heavy  : full-table sort 2M rows by c+pad+k (medium, M1=160MB, known)
  window_rank : RANK() OVER all 2M rows            (heavy,  M1 ~400-512MB)

Experiment phases:
  0 — WM scan for each AP form: find M1 (first work_mem with no spill)
  1 — TP-only baseline at SB=4GB (extended reference point)
  2 — Grid search: SB=4GB × WM∈{64,128,160,256,512}MB per AP form
  3 — Comparison: Default (SB=2GB, WM=64MB) vs Tuned (SB=4GB, WM=M1) per AP form
"""
import subprocess, time, os, json, re, itertools
from datetime import datetime

RESULTS_DIR = "/home/node/GaussTune/refine-logs/results"
os.makedirs(RESULTS_DIR, exist_ok=True)
OMM_PASS = "1997"
GSQL = "/opt/openGauss/app/bin/gsql"
SB_BASE = (
    "LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu "
    "sysbench oltp_read_write "
    "--db-driver=pgsql --pgsql-host=/tmp --pgsql-port=5432 "
    "--pgsql-user=omm --pgsql-password= --pgsql-db=sbtest "
    "--tables=10 --table-size=2000000 "
    "--db-ps-mode=disable"
)

# ── AP SQL definitions (3 memory profiles) ────────────────────────────────────
# All queries target sbtest1 (2M rows, ~400MB).
#
# sort_light  : sort first 300K rows (id<=300000) by c+k.
#               Sort data ≈ 300K × 184B = 56MB.  M1 expected ~64-128MB.
#
# sort_heavy  : full-table sort of 2M rows by c+pad+k.  M1=160MB (known from
#               exp2_workmem_threshold.json).  Reused from prior experiments.
#
# window_rank : RANK() OVER (ORDER BY c DESC, pad ASC, k DESC) on all 2M rows.
#               Window executor stores full tuples → spill ≈ 386MB at WM=64MB.
#               M1 expected ~400-512MB (significantly heavier than sort_heavy).

AP_SQLS = {
    "sort_light": (
        "SELECT id, k, c, pad "
        "FROM sbtest1 "
        "WHERE id <= 300000 "
        "ORDER BY c DESC, k ASC, id DESC"
    ),
    "sort_heavy": (
        "SELECT k, c, pad "
        "FROM sbtest1 "
        "ORDER BY c DESC, pad ASC, k DESC"
    ),
    "window_rank": (
        "SELECT id, k, "
        "RANK() OVER (ORDER BY c DESC, pad ASC, k DESC) AS rk "
        "FROM sbtest1 "
        "ORDER BY rk "
        "LIMIT 100000"
    ),
}

# No queries require special plan hints in this version
JOIN_SORT_PREFIX = ""

TP_THREADS = 16
TP_WARMUP  = 60    # seconds (longer warmup for better 4GB buffer coverage)
TP_MEASURE = 120   # seconds
AP_CONC    = 2     # AP concurrent workers

# Known baseline TPS from previous run (SB=2GB, TP-only, 16 threads)
BASELINE_TPS_2GB = 1465.6

# ── Helpers ──────────────────────────────────────────────────────────────────
def omm_run(cmd, timeout=60):
    r = subprocess.run(
        ["su", "-", "omm", "-c", cmd],
        input=OMM_PASS + "\n", capture_output=True, text=True, timeout=timeout
    )
    return r.stdout, r.stderr

def gsql_sql(sql, db="sbtest"):
    tmp = "/tmp/gt_v3.sql"
    with open(tmp, "w") as f:
        f.write(sql)
    os.chmod(tmp, 0o644)
    return omm_run(f"{GSQL} -d {db} -f {tmp}", timeout=600)

def set_guc(param, value):
    gsql_sql(f"ALTER SYSTEM SET {param} = '{value}'; SELECT pg_reload_conf();", db="postgres")
    time.sleep(3)

def restart_db():
    omm_run(
        "export GAUSSHOME=/opt/openGauss/app; export PATH=$GAUSSHOME/bin:$PATH; "
        "export LD_LIBRARY_PATH=$GAUSSHOME/lib; "
        "gs_ctl restart -D /opt/openGauss/data",
        timeout=180
    )
    time.sleep(15)
    for _ in range(16):  # up to 64s check window
        out, _ = omm_run(f"{GSQL} -d postgres -c \"SELECT 1;\"", timeout=10)
        if "1" in out:
            return True
        time.sleep(4)
    return False

def get_db_stats():
    out, _ = gsql_sql(
        "SELECT blks_hit, blks_read FROM pg_stat_database WHERE datname='sbtest';",
        db="postgres"
    )
    nums = re.findall(r"\d+", out)
    if len(nums) >= 2:
        return {"blks_hit": int(nums[-2]), "blks_read": int(nums[-1])}
    return {"blks_hit": 0, "blks_read": 0}

def cache_hit_pct(before, after):
    dh = after["blks_hit"]  - before["blks_hit"]
    dr = after["blks_read"] - before["blks_read"]
    total = dh + dr
    return round(100.0 * dh / total, 2) if total > 0 else 100.0

def parse_sysbench(output):
    m = {}
    for pat, key in [
        (r"transactions:\s+\d+\s+\(([\d.]+) per sec\.\)", "tps"),
        (r"95th percentile:\s+([\d.]+)", "p95_ms"),
    ]:
        mo = re.search(pat, output)
        if mo:
            m[key] = float(mo.group(1))
    return m

def run_sysbench(threads, duration_s):
    cmd = f"{SB_BASE} --threads={threads} --time={duration_s} run"
    out, _ = omm_run(cmd, timeout=duration_s + 90)
    return out

def wm_to_mb(wm_str):
    return int(wm_str[:-2]) * 1024 if wm_str.endswith("GB") else int(wm_str[:-2])

# ── Phase 0: WM scan (spill threshold detection) ─────────────────────────────
def detect_spill(explain_out):
    """Return (spill_kb, sort_method, hash_batches) from EXPLAIN ANALYZE output."""
    spill_kb = 0
    # Sort spill
    m = re.search(r"Sort Method:.*Disk:\s*(\d+)kB", explain_out)
    if m:
        spill_kb = max(spill_kb, int(m.group(1)))
    # Hash spill (Batches > 1)
    m2 = re.search(r"Batches:\s*(\d+)", explain_out)
    batches = int(m2.group(1)) if m2 else 1
    if batches > 1:
        # estimate spill from "Disk: XkB" in Hash node
        m3 = re.search(r"Hash\b.*?Disk:\s*(\d+)kB", explain_out, re.DOTALL)
        if m3:
            spill_kb = max(spill_kb, int(m3.group(1)))
        else:
            spill_kb = max(spill_kb, 1)  # flag non-zero even without exact size

    sort_m = re.search(r"Sort Method:\s*(.+?)(?:\s{2,}|\n)", explain_out)
    sort_method = sort_m.group(1).strip() if sort_m else "n/a"

    runtime_m = re.search(r"(?:Total runtime|Execution time):\s*([\d.]+)\s*ms", explain_out)
    runtime_ms = float(runtime_m.group(1)) if runtime_m else None

    return spill_kb, sort_method, batches, runtime_ms

def phase0_wm_scan():
    """Scan work_mem for each AP form to find M1 (first WM with spill_kb == 0)."""
    print("\n" + "=" * 65)
    print("PHASE 0: WM scan — find M1 (no-spill threshold) per AP form")
    print("=" * 65)
    scan_values_mb = [4, 8, 16, 32, 48, 64, 96, 128, 160, 192, 256, 384, 512, 768, 1024]
    results = {}

    for sql_name, sql_body in AP_SQLS.items():
        print(f"\n  SQL: {sql_name}")
        rows = []
        prefix = ""  # no plan hints needed for current AP queries
        m1_found = None
        min_start = WM_SCAN_START.get(sql_name, 4)

        for wm_mb in [v for v in scan_values_mb if v >= min_start]:
            explain_sql = (
                f"{prefix}"
                f"SET work_mem='{wm_mb}MB'; "
                f"EXPLAIN (ANALYZE, BUFFERS) {sql_body};"
            )
            out, err = gsql_sql(explain_sql)
            spill_kb, sort_method, batches, runtime_ms = detect_spill(out)

            status = "NO SPILL ✓" if spill_kb == 0 else f"spill {spill_kb:,} kB (batches={batches})"
            print(f"    WM={wm_mb:>4}MB | {sort_method[:35]:<35} | {status} | {runtime_ms} ms")

            rows.append({
                "work_mem_mb": wm_mb,
                "sort_method": sort_method,
                "spill_kb": spill_kb,
                "hash_batches": batches,
                "runtime_ms": runtime_ms,
            })

            if spill_kb == 0 and m1_found is None:
                m1_found = wm_mb
                print(f"    *** M1 threshold = {wm_mb}MB ***")
                break  # stop scanning once in-memory

        if m1_found is None:
            m1_found = scan_values_mb[-1]
            print(f"    *** M1 not reached within scan range; using {m1_found}MB ***")

        results[sql_name] = {"m1_mb": m1_found, "rows": rows}

    out_path = f"{RESULTS_DIR}/v3_wm_scan.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved: {out_path}")
    return results

# ── Phase 1: TP-only baseline at SB=4GB ──────────────────────────────────────
def phase1_baseline_4gb():
    print("\n" + "=" * 65)
    print("PHASE 1: TP-only baseline at SB=4GB, WM=64MB")
    print("=" * 65)
    set_guc("shared_buffers", "4GB")
    set_guc("work_mem", "64MB")
    restart_db()
    tps_list = []
    for run in range(1, 4):
        print(f"  Run {run}/3: warmup {TP_WARMUP}s + measure {TP_MEASURE}s ...")
        run_sysbench(TP_THREADS, TP_WARMUP)
        sb_before = get_db_stats()
        out = run_sysbench(TP_THREADS, TP_MEASURE)
        sb_after = get_db_stats()
        m = parse_sysbench(out)
        tps = m.get("tps", 0)
        p95 = m.get("p95_ms")
        hit = cache_hit_pct(sb_before, sb_after)
        tps_list.append(tps)
        print(f"    TPS={tps:.1f}, p95={p95}ms, cache_hit={hit}%")
    avg_tps = round(sum(tps_list) / len(tps_list), 1)
    print(f"  ── Baseline SB=4GB Avg TPS: {avg_tps}")
    return avg_tps

# ── AP worker management ──────────────────────────────────────────────────────
def launch_ap_workers(sql_name, wm_mb, count):
    sql_oneline = re.sub(r"\s+", " ", AP_SQLS[sql_name].strip())
    full_cmd = f"SET work_mem='{wm_mb}MB'; {sql_oneline}"
    # Write pid file so kill_workers can reach the bash process directly
    script = (
        f"#!/bin/bash\n"
        f"echo $$ > /tmp/ap_v3_{sql_name}_$$.pid\n"
        f"while true; do {GSQL} -d sbtest -c \"{full_cmd}\" >/dev/null 2>&1; done\n"
    )
    path = f"/tmp/ap_v3_{sql_name}.sh"
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    procs = []
    for _ in range(count):
        p = subprocess.Popen(
            ["su", "-", "omm", "-c", path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        p.stdin.write((OMM_PASS + "\n").encode())
        p.stdin.flush()
        procs.append(p)
    time.sleep(6)
    return procs

def kill_workers(procs):
    """Kill AP workers: terminate the su wrapper AND kill bash+gsql descendants via omm."""
    # 1. Cancel any active queries in the DB
    cancel_sql = ("SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                  "WHERE state='active' AND query LIKE '%sbtest%' AND pid != pg_backend_pid();")
    try:
        gsql_sql(cancel_sql, db="postgres")
    except Exception:
        pass

    # 2. Kill the bash loop processes via su omm (they survive su-wrapper termination)
    import glob
    pid_files = glob.glob("/tmp/ap_v3_*.pid")
    if pid_files:
        pids_str = " ".join(
            open(pf).read().strip() for pf in pid_files if open(pf).read().strip().isdigit()
        )
        if pids_str:
            omm_run(f"kill -9 {pids_str} 2>/dev/null", timeout=10)
        for pf in pid_files:
            try: os.unlink(pf)
            except: pass

    # 3. Terminate the su wrapper processes
    for p in procs:
        p.terminate()
        try: p.wait(timeout=5)
        except: p.kill()

    # 4. Wait for any lingering gsql to finish current query
    time.sleep(8)

def measure_mixed(sb, wm, sql_name):
    """Set SB+WM, restart, warmup TP, launch AP workers, measure TP for TP_MEASURE s."""
    wm_mb = wm_to_mb(wm)
    set_guc("shared_buffers", sb)
    set_guc("work_mem", wm)
    ok = restart_db()
    if not ok:
        print("    !! DB restart failed, skipping")
        return None
    run_sysbench(TP_THREADS, TP_WARMUP)
    workers = launch_ap_workers(sql_name, wm_mb, AP_CONC)
    sb_before = get_db_stats()
    tp_out = run_sysbench(TP_THREADS, TP_MEASURE)
    sb_after = get_db_stats()
    kill_workers(workers)
    m = parse_sysbench(tp_out)
    tps = m.get("tps", 0)
    p95 = m.get("p95_ms")
    hit = cache_hit_pct(sb_before, sb_after)
    return {"shared_buffers": sb, "work_mem": wm, "ap_sql": sql_name,
            "ap_concurrency": AP_CONC, "tps": tps, "p95_ms": p95, "cache_hit_pct": hit}

# ── Phase 2: Grid search per AP form ─────────────────────────────────────────
# Extends prior grid with SB=4GB and SB=5GB (SB=2GB,3GB already measured).
# For each AP form we run the new SB values across all WM values.

# Known SB=2GB results from previous grid (for ap_sql = hashjoin_agg, AP_CONC=2)
# We'll re-use these to anchor the "default" baseline row.
KNOWN_2GB = {
    "64MB":  {"tps": 713.5, "p95_ms": 48.34,  "cache_hit_pct": 100.0},
    "256MB": {"tps": 682.9, "p95_ms": 49.21,  "cache_hit_pct": 100.0},
    "512MB": {"tps": 689.0, "p95_ms": 41.10,  "cache_hit_pct": 100.0},
    "768MB": {"tps": 664.7, "p95_ms": 43.39,  "cache_hit_pct": 100.0},
    "1GB":   {"tps": 615.0, "p95_ms": 51.02,  "cache_hit_pct": 100.0},
}

SB_NEW    = ["4GB"]   # new SB value to measure (dataset ~4GB, so 5GB adds nothing)
WM_GRID   = ["64MB", "128MB", "160MB", "256MB", "512MB"]

# Per-query WM scan start (avoid very-slow tiny-WM executions for heavy queries)
WM_SCAN_START = {
    "sort_light":   16,   # 300K rows sort, M1 ~64-128MB
    "sort_heavy":   64,   # 2M rows sort, M1=160MB (known from exp2)
    "window_rank": 128,   # RANK() over 2M rows, M1 ~400-512MB
}

def phase2_grid_new_sb(wm_scan_results, baseline_tps_4gb):
    print("\n" + "=" * 65)
    print("PHASE 2: Grid search at new SB values (4GB, 5GB) per AP form")
    print(f"  AP_CONC={AP_CONC}, TP_THREADS={TP_THREADS}, MEASURE={TP_MEASURE}s")
    print("=" * 65)

    all_results = {}   # sql_name → list of row dicts
    combos = list(itertools.product(SB_NEW, WM_GRID, AP_SQLS.keys()))
    total = len(combos)

    for idx, (sb, wm, sql_name) in enumerate(combos, 1):
        print(f"\n  [{idx}/{total}] SB={sb}, WM={wm}, AP={sql_name} ...")
        row = measure_mixed(sb, wm, sql_name)
        if row is None:
            continue

        # Compute recovery vs same-SB baseline
        baseline = baseline_tps_4gb if sb in ("4GB", "5GB") else BASELINE_TPS_2GB
        row["baseline_tps"] = baseline
        row["tps_recovery_pct"] = round(100.0 * row["tps"] / baseline, 1) if baseline else None

        all_results.setdefault(sql_name, []).append(row)
        print(f"    TPS={row['tps']:.1f}, recovery={row['tps_recovery_pct']}%, "
              f"p95={row['p95_ms']}ms, cache_hit={row['cache_hit_pct']}%")

    out_path = f"{RESULTS_DIR}/v3_grid_new_sb.json"
    with open(out_path, "w") as f:
        json.dump({"baseline_tps_2gb": BASELINE_TPS_2GB,
                   "baseline_tps_4gb": baseline_tps_4gb,
                   "wm_scan": wm_scan_results,
                   "results": all_results}, f, indent=2)
    print(f"\n  Saved: {out_path}")
    return all_results

# ── Phase 3: Default vs Tuned comparison per AP form ─────────────────────────
def phase3_default_vs_tuned(wm_scan_results, baseline_tps_4gb):
    """
    For each AP form, compare 4 configs:
      Default : SB=2GB, WM=64MB  (memory-limited, WM possibly below M1)
      WM-Tuned: SB=2GB, WM=M1   (WM raised to AP's no-spill threshold)
      SB-Tuned: SB=4GB, WM=64MB (SB raised to cover dataset)
      Joint   : SB=4GB, WM=M1   (both SB and WM jointly optimized)

    Recovery is computed against TP-only baseline at the matching SB level.
    """
    print("\n" + "=" * 65)
    print("PHASE 3: Default vs Tuned comparison per AP form")
    print(f"  AP_CONC={AP_CONC}, TP_THREADS={TP_THREADS}, MEASURE={TP_MEASURE}s")
    print("=" * 65)

    comparison = []

    for sql_name in AP_SQLS:
        m1_mb = wm_scan_results[sql_name]["m1_mb"]
        m1_str = f"{m1_mb}MB"
        print(f"\n  AP form: {sql_name}  (M1={m1_mb}MB)")

        configs = [
            {"label": "Default",   "sb": "2GB", "wm": "64MB",  "ref_tps": BASELINE_TPS_2GB},
            {"label": "WM-Tuned",  "sb": "2GB", "wm": m1_str,  "ref_tps": BASELINE_TPS_2GB},
            {"label": "SB-Tuned",  "sb": "4GB", "wm": "64MB",  "ref_tps": baseline_tps_4gb},
            {"label": "Joint",     "sb": "4GB", "wm": m1_str,  "ref_tps": baseline_tps_4gb},
        ]

        for cfg in configs:
            print(f"    {cfg['label']} (SB={cfg['sb']}, WM={cfg['wm']}) ...", end=" ", flush=True)
            result = measure_mixed(cfg["sb"], cfg["wm"], sql_name)
            if result is None:
                print("FAILED")
                continue
            recovery = round(100.0 * result["tps"] / cfg["ref_tps"], 1) if cfg["ref_tps"] else None
            row = {"label": cfg["label"], "sb": cfg["sb"], "wm": cfg["wm"],
                   "sql": sql_name, "tps": result["tps"], "p95_ms": result["p95_ms"],
                   "cache_hit_pct": result["cache_hit_pct"],
                   "ref_tps": cfg["ref_tps"], "recovery_pct": recovery}
            comparison.append(row)
            print(f"TPS={result['tps']:.1f}, recovery={recovery}%, cache_hit={result['cache_hit_pct']}%")

    # Print summary table
    print(f"\n  ── Phase 3 Summary ──")
    print(f"  {'AP SQL':<18} {'Config':<12} {'SB':>5} {'WM':>6} {'TPS':>8} {'Recov%':>8} {'p95ms':>7}")
    print(f"  {'-' * 70}")
    for r in comparison:
        tps_str = f"{r['tps']:.1f}" if r.get("tps") else "→phase2"
        rec_str = str(r.get("recovery_pct", r.get("recovery", "?")))
        p95_str = str(r.get("p95_ms", "?"))
        print(f"  {r['sql']:<18} {r['label']:<12} {r['sb']:>5} {r['wm']:>6} "
              f"{tps_str:>8} {rec_str:>8} {p95_str:>7}")

    out_path = f"{RESULTS_DIR}/v3_comparison.json"
    with open(out_path, "w") as f:
        json.dump({"comparison": comparison, "baseline_2gb": BASELINE_TPS_2GB,
                   "baseline_4gb": baseline_tps_4gb}, f, indent=2)
    print(f"\n  Saved: {out_path}")
    return comparison


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    t0 = datetime.now()
    print(f"\nGaussTune v3 — {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"System: 14GB RAM | Dataset ~4GB (10×2M rows) | AP_CONC={AP_CONC}")
    print(f"AP forms: {list(AP_SQLS.keys())}")

    # Phase 0: WM scan
    wm_scan = phase0_wm_scan()

    # Phase 1: baseline at SB=4GB
    baseline_4gb = phase1_baseline_4gb()

    # Phase 2: grid at new SB values
    grid_results = phase2_grid_new_sb(wm_scan, baseline_4gb)

    # Phase 3: Default vs Tuned comparison
    comparison = phase3_default_vs_tuned(wm_scan, baseline_4gb)

    # Restore defaults
    set_guc("shared_buffers", "4GB")
    set_guc("work_mem", "64MB")
    restart_db()

    elapsed = (datetime.now() - t0).total_seconds() / 60
    print(f"\n{'=' * 65}")
    print("ALL v3 EXPERIMENTS COMPLETE")
    print(f"  Baseline TPS (SB=2GB): {BASELINE_TPS_2GB}")
    print(f"  Baseline TPS (SB=4GB): {baseline_4gb}")
    print(f"  WM scan M1 thresholds:")
    for sql_name, r in wm_scan.items():
        print(f"    {sql_name}: M1 = {r['m1_mb']}MB")
    print(f"  Elapsed: {elapsed:.1f} min")
    print(f"  Results: {RESULTS_DIR}/v3_*.json")
    print(f"{'=' * 65}")
