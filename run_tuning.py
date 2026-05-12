#!/usr/bin/env python3
"""
GaussTune Tuning Experiment
- Large dataset: 10 tables × 2M rows (~4.5 GB) > shared_buffers → memory IS the bottleneck
- AP SQLs: 3 forms (heavy sort, hash join+agg, multi-level agg)
- Grid search over shared_buffers × work_mem
- Measures: TP TPS, TP p95, AP queries/min, cache_hit_pct
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

# ── AP SQL definitions ────────────────────────────────────────────────────────
# Calibrated to sbtest1 (2M rows, 422MB heap):
#   sort_heavy:     full-table sort, M1 ~ 768MB  → spills at WM < 768MB
#   hashjoin_agg:   2-table filtered JOIN+agg,   ~ 4-6s, fits in 256MB
#   multilevel_agg: single-table group-by UNION, lightweight, always fast
AP_SQLS = {
    # Full sort of one table (422MB data) — M1 at ~768MB
    "sort_heavy": """
        SELECT k, c, pad
        FROM sbtest1
        ORDER BY c DESC, pad ASC, k DESC
    """,
    # 2-table hash join with heavy aggregation — filtered to ~10% rows, join on k
    "hashjoin_agg": """
        SELECT a.k, COUNT(*) AS cnt, AVG(a.k) AS avg_k, MAX(LENGTH(a.c)) AS max_clen
        FROM sbtest1 a
        JOIN sbtest2 b ON a.k = b.k
        WHERE a.k % 1000 < 100
        GROUP BY a.k
        ORDER BY cnt DESC, avg_k DESC
        LIMIT 5000
    """,
    # Multi-level aggregation: group-by UNION across 3 tables
    "multilevel_agg": """
        SELECT bucket, SUM(cnt) AS total_cnt, AVG(avg_k) AS global_avg_k
        FROM (
            SELECT k % 1000 AS bucket, COUNT(*) AS cnt, AVG(k) AS avg_k
            FROM sbtest1
            GROUP BY k % 1000
            UNION ALL
            SELECT k % 1000 AS bucket, COUNT(*) AS cnt, AVG(k) AS avg_k
            FROM sbtest2
            GROUP BY k % 1000
            UNION ALL
            SELECT k % 1000 AS bucket, COUNT(*) AS cnt, AVG(k) AS avg_k
            FROM sbtest3
            GROUP BY k % 1000
        ) sub
        GROUP BY bucket
        ORDER BY total_cnt DESC
    """,
}

# ── Grid search space ─────────────────────────────────────────────────────────
# Dataset = 5GB (10 × 508MB), all SB values < 5GB → memory stays as bottleneck
# sort_heavy M1 threshold ~ 768MB → WM grid spans below and above threshold
# shared_buffers: 512MB–3GB  (all < 5GB dataset, varies cache pressure on TP)
SB_VALUES  = ["512MB", "1GB", "2GB", "3GB"]
WM_VALUES  = ["64MB", "256MB", "512MB", "768MB", "1GB"]
TP_THREADS = 16
TP_WARMUP  = 30   # seconds
TP_MEASURE = 120  # seconds
AP_CONC    = 2    # AP workers during mixed measurement

def omm_run(cmd, timeout=60):
    r = subprocess.run(["su", "-", "omm", "-c", cmd],
        input=OMM_PASS + "\n", capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr

def gsql_sql(sql, db="sbtest"):
    tmp = "/tmp/gt_query.sql"
    with open(tmp, "w") as f:
        f.write(sql)
    os.chmod(tmp, 0o644)
    out, err = omm_run(f"{GSQL} -d {db} -f {tmp}", timeout=600)
    return out, err

def set_guc(param, value):
    gsql_sql(f"ALTER SYSTEM SET {param} = '{value}'; SELECT pg_reload_conf();", db="postgres")
    time.sleep(3)

def restart_db():
    omm_run(
        "export GAUSSHOME=/opt/openGauss/app; export PATH=$GAUSSHOME/bin:$PATH; "
        "export LD_LIBRARY_PATH=$GAUSSHOME/lib; "
        "gs_ctl restart -D /opt/openGauss/data",
        timeout=120
    )
    time.sleep(12)
    # verify
    for _ in range(5):
        out, _ = omm_run(f"{GSQL} -d postgres -c \"SELECT 1;\"", timeout=10)
        if "1" in out:
            return True
        time.sleep(3)
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

def get_ap_explain(sql_name, wm_mb):
    sql = f"SET work_mem='{wm_mb}MB';\nEXPLAIN (ANALYZE, BUFFERS) {AP_SQLS[sql_name]};"
    out, _ = gsql_sql(sql)
    result = {"sql": sql_name, "work_mem_mb": wm_mb}
    m = re.search(r"Sort Method: (\S+.*)", out)
    result["sort_method"] = m.group(1).strip() if m else "n/a"
    m = re.search(r"Disk: (\d+)kB", out)
    result["spill_kb"] = int(m.group(1)) if m else 0
    m = re.search(r"Total runtime: ([\d.]+) ms", out)
    result["total_ms"] = float(m.group(1)) if m else None
    return result

def launch_ap_workers(ap_sql_name, wm_mb, count):
    sql_oneline = re.sub(r"\s+", " ", AP_SQLS[ap_sql_name].strip())
    script = f"""#!/bin/bash
while true; do
  {GSQL} -d sbtest -c "SET work_mem='{wm_mb}MB'; {sql_oneline}" >/dev/null 2>&1
done
"""
    path = f"/tmp/ap_worker_{ap_sql_name}.sh"
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    procs = []
    for _ in range(count):
        p = subprocess.Popen(["su", "-", "omm", "-c", path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.stdin.write((OMM_PASS + "\n").encode())
        p.stdin.flush()
        procs.append(p)
    time.sleep(4)
    return procs

def kill_workers(procs):
    for p in procs:
        p.terminate()
        try: p.wait(timeout=5)
        except: p.kill()

def count_ap_completions(ap_sql_name, wm_mb, duration_s):
    """Run AP SQL repeatedly for duration_s, count completions."""
    sql_oneline = re.sub(r"\s+", " ", AP_SQLS[ap_sql_name].strip())
    script = f"""#!/bin/bash
COUNT=0
END=$((SECONDS + {duration_s}))
while [ $SECONDS -lt $END ]; do
  {GSQL} -d sbtest -c "SET work_mem='{wm_mb}MB'; {sql_oneline}" >/dev/null 2>&1
  COUNT=$((COUNT + 1))
done
echo "AP_COUNT:$COUNT"
"""
    path = "/tmp/ap_counter.sh"
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    out, _ = omm_run(f"bash {path}", timeout=duration_s + 30)
    m = re.search(r"AP_COUNT:(\d+)", out)
    return int(m.group(1)) if m else 0

# ── Phase 0: AP SQL work_mem scan (find spill thresholds) ─────────────────────
def phase0_ap_scan():
    print("\n" + "="*60)
    print("PHASE 0: AP SQL work_mem scan (find spill thresholds)")
    print("="*60)
    results = {}
    for sql_name in AP_SQLS:
        print(f"\n  SQL: {sql_name}")
        rows = []
        for wm in [4, 16, 32, 64, 128, 256, 512]:
            r = get_ap_explain(sql_name, wm)
            rows.append(r)
            flag = "NO SPILL ✓" if r["spill_kb"] == 0 else f"spill {r['spill_kb']} kB"
            print(f"    WM={wm:>4}MB | {r['sort_method']:<35} | {flag} | {r['total_ms']} ms")
            if r["spill_kb"] == 0:
                break
        results[sql_name] = rows
    return results

# ── Phase 1: TP-only baseline (at default config) ─────────────────────────────
def phase1_tp_baseline():
    print("\n" + "="*60)
    print("PHASE 1: TP-only Baseline (shared_buffers=2GB, work_mem=256MB)")
    print("="*60)
    set_guc("shared_buffers", "2GB")
    set_guc("work_mem", "256MB")
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
    print(f"  ── Baseline Avg TPS: {avg_tps}")
    return avg_tps

# ── Phase 2: Grid search ──────────────────────────────────────────────────────
def phase2_grid_search(baseline_tps, ap_scan_results):
    print("\n" + "="*60)
    print(f"PHASE 2: Grid Search  shared_buffers={SB_VALUES}  work_mem={WM_VALUES}")
    print(f"  AP concurrency={AP_CONC}, TP threads={TP_THREADS}, measure={TP_MEASURE}s")
    print(f"  Baseline TPS={baseline_tps}")
    print("="*60)

    # Pick the AP SQL with most interesting spill behavior for mixed test
    ap_sql_name = "hashjoin_agg"
    all_rows = []
    combos = list(itertools.product(SB_VALUES, WM_VALUES))
    total = len(combos)

    for idx, (sb, wm) in enumerate(combos, 1):
        wm_mb = int(wm.replace("GB","024").replace("MB","")) if "GB" in wm else int(wm.replace("MB",""))
        wm_mb = int(wm[:-2]) * 1024 if wm.endswith("GB") else int(wm[:-2])
        print(f"\n  [{idx}/{total}] shared_buffers={sb}, work_mem={wm} ...")

        # Apply config and restart (shared_buffers requires restart)
        set_guc("shared_buffers", sb)
        set_guc("work_mem", wm)
        ok = restart_db()
        if not ok:
            print("    !! DB restart failed, skipping")
            continue

        # Warmup TP only
        run_sysbench(TP_THREADS, TP_WARMUP)

        # Launch AP workers
        workers = launch_ap_workers(ap_sql_name, wm_mb, AP_CONC)

        # Measure TP under AP load
        sb_before = get_db_stats()
        tp_out = run_sysbench(TP_THREADS, TP_MEASURE)
        sb_after = get_db_stats()
        kill_workers(workers)

        m = parse_sysbench(tp_out)
        tps = m.get("tps", 0)
        p95 = m.get("p95_ms")
        hit = cache_hit_pct(sb_before, sb_after)
        recovery = round(100.0 * tps / baseline_tps, 1) if baseline_tps and tps else None

        row = {
            "shared_buffers": sb, "work_mem": wm,
            "tps": tps, "tps_recovery_pct": recovery,
            "p95_ms": p95, "cache_hit_pct": hit,
        }
        all_rows.append(row)
        print(f"    TPS={tps:.1f}, recovery={recovery}%, p95={p95}ms, cache_hit={hit}%")

    # Print ranked results
    ranked = sorted(all_rows, key=lambda r: r.get("tps", 0), reverse=True)
    print(f"\n  ─── Grid Search Results (ranked by TPS) ───")
    print(f"  {'SB':<8} {'WM':<8} {'TPS':>8} {'Recov%':>8} {'p95ms':>7} {'CacheHit%':>10}")
    print(f"  {'-'*55}")
    for r in ranked:
        print(f"  {r['shared_buffers']:<8} {r['work_mem']:<8} {r['tps']:>8.1f} "
              f"{str(r.get('tps_recovery_pct','?')):>8} {str(r.get('p95_ms','?')):>7} "
              f"{str(r.get('cache_hit_pct','?')):>10}")

    out_path = f"{RESULTS_DIR}/tuning_grid_search.json"
    with open(out_path, "w") as f:
        json.dump({
            "baseline_tps": baseline_tps,
            "ap_sql": ap_sql_name,
            "ap_concurrency": AP_CONC,
            "tp_threads": TP_THREADS,
            "grid": SB_VALUES,
            "wm_grid": WM_VALUES,
            "rows": all_rows,
            "ranked": ranked,
        }, f, indent=2)
    print(f"\n  Saved: {out_path}")
    return ranked

# ── Phase 3: Best-config deep comparison ─────────────────────────────────────
def phase3_best_vs_default(baseline_tps, best_config, ap_scan_results):
    print("\n" + "="*60)
    print("PHASE 3: Best Config vs Default — Multiple AP SQL types")
    print("="*60)

    configs = [
        {"name": "Default",  "sb": "2GB",                "wm": "256MB"},
        {"name": "Best",     "sb": best_config["shared_buffers"], "wm": best_config["work_mem"]},
    ]
    ap_concurrencies = [1, 2, 4]
    results = []

    for cfg in configs:
        print(f"\n  ── Config: {cfg['name']} (SB={cfg['sb']}, WM={cfg['wm']}) ──")
        set_guc("shared_buffers", cfg["sb"])
        set_guc("work_mem", cfg["wm"])
        restart_db()
        wm_mb = int(cfg["wm"][:-2]) * 1024 if cfg["wm"].endswith("GB") else int(cfg["wm"][:-2])

        for ap_sql_name in AP_SQLS:
            for ap_conc in ap_concurrencies:
                print(f"    AP={ap_sql_name}, conc={ap_conc}: warmup+measure ...")
                run_sysbench(TP_THREADS, TP_WARMUP)
                workers = launch_ap_workers(ap_sql_name, wm_mb, ap_conc)
                sb_before = get_db_stats()
                tp_out = run_sysbench(TP_THREADS, TP_MEASURE)
                sb_after = get_db_stats()
                kill_workers(workers)
                m = parse_sysbench(tp_out)
                tps = m.get("tps", 0)
                hit = cache_hit_pct(sb_before, sb_after)
                recovery = round(100.0 * tps / baseline_tps, 1) if baseline_tps and tps else None
                row = {
                    "config": cfg["name"], "shared_buffers": cfg["sb"], "work_mem": cfg["wm"],
                    "ap_sql": ap_sql_name, "ap_concurrency": ap_conc,
                    "tps": tps, "tps_recovery_pct": recovery,
                    "p95_ms": m.get("p95_ms"), "cache_hit_pct": hit,
                }
                results.append(row)
                print(f"      TPS={tps:.1f}, recovery={recovery}%, cache_hit={hit}%")

    # Summary table
    print(f"\n  ─── Phase 3 Summary ───")
    print(f"  {'Config':<10} {'AP SQL':<16} {'AP':>3} {'TPS':>8} {'Recov%':>8} {'CacheHit%':>10}")
    print(f"  {'-'*60}")
    for r in results:
        print(f"  {r['config']:<10} {r['ap_sql']:<16} {r['ap_concurrency']:>3} "
              f"{r['tps']:>8.1f} {str(r.get('tps_recovery_pct','?')):>8} "
              f"{str(r.get('cache_hit_pct','?')):>10}")

    out_path = f"{RESULTS_DIR}/tuning_best_vs_default.json"
    with open(out_path, "w") as f:
        json.dump({"baseline_tps": baseline_tps, "configs": configs, "rows": results}, f, indent=2)
    print(f"  Saved: {out_path}")
    return results


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\nGaussTune Tuning Experiment — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Dataset: 10 tables × 2M rows | Grid: {SB_VALUES} × {WM_VALUES}")

    ap_scan = phase0_ap_scan()

    baseline_tps = phase1_tp_baseline()

    ranked = phase2_grid_search(baseline_tps, ap_scan)

    best = ranked[0] if ranked else {"shared_buffers": "2GB", "work_mem": "64MB"}
    print(f"\nBest config from grid: SB={best['shared_buffers']}, WM={best['work_mem']}, "
          f"TPS={best['tps']:.1f} ({best.get('tps_recovery_pct')}%)")

    phase3_best_vs_default(baseline_tps, best, ap_scan)

    # Restore defaults
    set_guc("shared_buffers", "4GB")
    set_guc("work_mem", "64MB")
    restart_db()

    out_path = f"{RESULTS_DIR}/tuning_summary_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(out_path, "w") as f:
        json.dump({"baseline_tps": baseline_tps, "best_config": best,
                   "ap_scan": ap_scan, "ranked": ranked}, f, indent=2, default=str)

    print(f"\n{'='*60}")
    print("ALL TUNING EXPERIMENTS COMPLETE")
    print(f"  Baseline TPS : {baseline_tps}")
    print(f"  Best config  : SB={best['shared_buffers']}, WM={best['work_mem']}")
    print(f"  Best TPS     : {best['tps']:.1f} ({best.get('tps_recovery_pct')}%)")
    print(f"{'='*60}")
