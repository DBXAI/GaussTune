#!/usr/bin/env python3
"""
GaussTune Experiment Runner
Experiments 1, 2, 3 on OpenGauss + sysbench
"""
import subprocess, time, os, json, re
from datetime import datetime

RESULTS_DIR = "/home/node/GaussTune/refine-logs/results"
os.makedirs(RESULTS_DIR, exist_ok=True)

OMM_PASS = "1997"
SB_BASE = (
    "LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu "
    "sysbench oltp_read_write "
    "--db-driver=pgsql --pgsql-host=/tmp --pgsql-port=5432 "
    "--pgsql-user=omm --pgsql-password= --pgsql-db=sbtest "
    "--tables=10 --table-size=100000"
)
AP_SQL = """
SELECT id, k, c, pad
FROM (
    SELECT id, k, c, pad FROM sbtest1
    UNION ALL SELECT id, k, c, pad FROM sbtest2
    UNION ALL SELECT id, k, c, pad FROM sbtest3
    UNION ALL SELECT id, k, c, pad FROM sbtest4
    UNION ALL SELECT id, k, c, pad FROM sbtest5
) t
ORDER BY c, pad, k DESC
"""

def gsql(sql, db="sbtest"):
    r = subprocess.run(
        ["su", "-", "omm", "-c", f'/opt/openGauss/app/bin/gsql -d {db} -c "{sql}"'],
        input=OMM_PASS + "\n", capture_output=True, text=True
    )
    return r.stdout

def gsql_file(sql_str, db="sbtest"):
    """Write SQL to temp file and run — avoids shell quoting issues."""
    tmp = "/tmp/gausstune_query.sql"
    with open(tmp, "w") as f:
        f.write(sql_str)
    os.chmod(tmp, 0o644)
    r = subprocess.run(
        ["su", "-", "omm", "-c", f"/opt/openGauss/app/bin/gsql -d {db} -f {tmp}"],
        input=OMM_PASS + "\n", capture_output=True, text=True
    )
    return r.stdout, r.stderr

def set_work_mem(mb):
    gsql(f"ALTER SYSTEM SET work_mem = '{mb}MB'; SELECT pg_reload_conf();", db="postgres")
    time.sleep(1)

def get_bgwriter_stats():
    out = gsql("SELECT blks_hit, blks_read FROM pg_stat_bgwriter;", db="postgres")
    nums = re.findall(r"\d+", out)
    if len(nums) >= 2:
        hit, read = int(nums[-2]), int(nums[-1])
        total = hit + read
        ratio = round(100.0 * hit / total, 2) if total > 0 else 100.0
        return {"blks_hit": hit, "blks_read": read, "cache_hit_pct": ratio}
    return {}

def get_mem_usage_mb():
    r = subprocess.run(
        ["su", "-", "omm", "-c", "ps -C gaussdb -o rss= | head -1"],
        input=OMM_PASS + "\n", capture_output=True, text=True
    )
    try:
        return round(int(r.stdout.strip().split()[-1]) / 1024, 1)
    except Exception:
        return None

def parse_sysbench(output):
    metrics = {}
    for pat, key in [
        (r"transactions:\s+\d+\s+\(([\d.]+) per sec\.\)", "tps"),
        (r"95th percentile:\s+([\d.]+)", "p95_ms"),
        (r"avg:\s+([\d.]+)", "avg_ms"),
        (r"min:\s+([\d.]+)", "min_ms"),
    ]:
        m = re.search(pat, output)
        if m:
            metrics[key] = float(m.group(1))
    return metrics

def run_sysbench(threads, duration_s):
    cmd = f"{SB_BASE} --threads={threads} --time={duration_s} run"
    r = subprocess.run(
        ["su", "-", "omm", "-c", cmd],
        input=OMM_PASS + "\n", capture_output=True, text=True,
        timeout=duration_s + 60
    )
    return r.stdout

def run_ap_explain(work_mem_mb):
    sql = f"""SET work_mem = '{work_mem_mb}MB';
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
{AP_SQL};"""
    out, err = gsql_file(sql)
    result = {"work_mem_mb": work_mem_mb, "raw": out}
    m = re.search(r"Sort Method: (\S+.*)", out)
    if m:
        result["sort_method"] = m.group(1).strip()
    m = re.search(r"Disk: (\d+)kB", out)
    result["spill_kb"] = int(m.group(1)) if m else 0
    m = re.search(r"actual time=[\d.]+\.\.([\d.]+)", out)
    if m:
        result["sort_ms"] = float(m.group(1))
    m = re.search(r"Total runtime: ([\d.]+) ms", out)
    if m:
        result["total_ms"] = float(m.group(1))
    return result

def run_ap_plain(work_mem_mb):
    """Run AP SQL 3x at given work_mem, return avg exec time."""
    sql = f"SET work_mem = '{work_mem_mb}MB';\n{AP_SQL};"
    times = []
    for _ in range(3):
        t0 = time.time()
        gsql_file(sql)
        times.append(round((time.time() - t0) * 1000, 1))
    return {"work_mem_mb": work_mem_mb, "avg_ms": round(sum(times)/len(times), 1), "runs": times}

# ─────────────────────────────────────────────────────────────────────────────
# EXPERIMENT 1: TP-only baseline
# ─────────────────────────────────────────────────────────────────────────────
def exp1_tp_baseline():
    print("\n" + "="*60)
    print("EXPERIMENT 1: TP-only Baseline (16 threads, 300s × 3)")
    print("="*60)
    # Reset work_mem to default
    set_work_mem(64)
    results = []
    for run_id in range(1, 4):
        print(f"\n  Run {run_id}/3 — warmup 30s + measure 300s ...")
        # warmup
        run_sysbench(threads=16, duration_s=30)
        stats_before = get_bgwriter_stats()
        mem_before = get_mem_usage_mb()
        # measure
        out = run_sysbench(threads=16, duration_s=300)
        stats_after = get_bgwriter_stats()
        mem_after = get_mem_usage_mb()
        metrics = parse_sysbench(out)
        metrics["run"] = run_id
        metrics["cache_hit_pct_before"] = stats_before.get("cache_hit_pct")
        metrics["cache_hit_pct_after"] = stats_after.get("cache_hit_pct")
        metrics["gaussdb_rss_mb"] = mem_after
        metrics["spill"] = 0  # TP-only, no sort spill
        results.append(metrics)
        print(f"    TPS={metrics.get('tps','?')}, p95={metrics.get('p95_ms','?')}ms, "
              f"cache_hit_after={metrics.get('cache_hit_pct_after','?')}%, "
              f"gaussdb_rss={mem_after}MB")
    # Summary
    tps_vals = [r["tps"] for r in results if "tps" in r]
    avg_tps = round(sum(tps_vals)/len(tps_vals), 1) if tps_vals else None
    p95_vals = [r["p95_ms"] for r in results if "p95_ms" in r]
    avg_p95 = round(sum(p95_vals)/len(p95_vals), 1) if p95_vals else None
    print(f"\n  ─── Exp1 Summary ───")
    print(f"  Avg TPS: {avg_tps} | Avg p95: {avg_p95} ms")
    out_path = f"{RESULTS_DIR}/exp1_tp_baseline.json"
    with open(out_path, "w") as f:
        json.dump({"summary": {"avg_tps": avg_tps, "avg_p95_ms": avg_p95},
                   "runs": results}, f, indent=2)
    print(f"  Saved: {out_path}")
    return avg_tps, avg_p95

# ─────────────────────────────────────────────────────────────────────────────
# EXPERIMENT 2: work_mem threshold characterization
# ─────────────────────────────────────────────────────────────────────────────
def exp2_workmem_threshold():
    print("\n" + "="*60)
    print("EXPERIMENT 2: work_mem Threshold Characterization (AP Sort SQL)")
    print("="*60)
    wm_values = [4, 8, 16, 32, 64, 96, 128, 160, 192, 256]
    all_results = []
    for wm in wm_values:
        print(f"\n  work_mem = {wm} MB ...")
        explain_res = run_ap_explain(wm)
        plain_res = run_ap_plain(wm)
        row = {
            "work_mem_mb": wm,
            "sort_method": explain_res.get("sort_method", "unknown"),
            "spill_kb": explain_res.get("spill_kb", 0),
            "explain_total_ms": explain_res.get("total_ms"),
            "avg_exec_ms": plain_res.get("avg_ms"),
        }
        all_results.append(row)
        spill_str = f"{row['spill_kb']} kB" if row['spill_kb'] > 0 else "0 (no spill)"
        print(f"    Sort: {row['sort_method']} | Spill: {spill_str} | Avg exec: {row['avg_exec_ms']} ms")
        if row['spill_kb'] == 0 and wm >= 64:
            print(f"    *** M1 threshold reached at work_mem = {wm} MB ***")
            break
    # Determine thresholds
    m1_thresh = None
    for r in all_results:
        if r['spill_kb'] == 0:
            m1_thresh = r['work_mem_mb']
            break
    print(f"\n  ─── Exp2 Summary ───")
    print(f"  M1 (in-memory, no spill): work_mem ≥ {m1_thresh} MB" if m1_thresh else "  M1 threshold not reached yet")
    for r in all_results:
        print(f"  {r['work_mem_mb']:>4} MB | {r['sort_method']:<30} | spill={r['spill_kb']:>8} kB | avg={r['avg_exec_ms']} ms")
    out_path = f"{RESULTS_DIR}/exp2_workmem_threshold.json"
    with open(out_path, "w") as f:
        json.dump({"m1_threshold_mb": m1_thresh, "rows": all_results}, f, indent=2)
    print(f"  Saved: {out_path}")
    return m1_thresh, all_results

# ─────────────────────────────────────────────────────────────────────────────
# EXPERIMENT 3: TP+AP mixed workload (Default vs Tuned)
# ─────────────────────────────────────────────────────────────────────────────
def exp3_mixed(tp_baseline_tps, m1_threshold_mb):
    print("\n" + "="*60)
    print("EXPERIMENT 3: TP+AP Mixed Workload (Default vs Tuned)")
    print("="*60)
    tuned_wm = m1_threshold_mb if m1_threshold_mb else 128
    configs = [
        {"name": "Default", "work_mem_mb": 64,      "label": f"Default (WM=64MB)"},
        {"name": "Tuned",   "work_mem_mb": tuned_wm, "label": f"Tuned (WM={tuned_wm}MB, no AP spill)"},
    ]
    ap_concurrencies = [1, 2, 4]
    all_results = []
    for cfg in configs:
        wm = cfg["work_mem_mb"]
        print(f"\n  ── Config: {cfg['label']} ──")
        set_work_mem(wm)
        for ap_conc in ap_concurrencies:
            print(f"\n    AP concurrency = {ap_conc}, warmup 20s + measure 120s ...")
            # Start AP load in background (ap_conc parallel AP SQL processes)
            ap_procs = []
            ap_sql_cmd = (
                f"LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu "
                f"/opt/openGauss/app/bin/gsql -d sbtest "
                f"-c \"SET work_mem='{wm}MB'; {AP_SQL.strip().replace(chr(10), ' ')}\" "
                f">/dev/null 2>&1"
            )
            # Warmup TP only first
            run_sysbench(threads=16, duration_s=20)
            stats_before = get_bgwriter_stats()
            mem_before = get_mem_usage_mb()
            # Launch AP processes
            for _ in range(ap_conc):
                p = subprocess.Popen(
                    ["su", "-", "omm", "-c",
                     f"while true; do {ap_sql_cmd}; done"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                p.stdin.write((OMM_PASS + "\n").encode())
                p.stdin.flush()
                ap_procs.append(p)
            time.sleep(2)  # let AP start
            # Measure TP while AP is running
            tp_out = run_sysbench(threads=16, duration_s=120)
            # Stop AP
            for p in ap_procs:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()
            stats_after = get_bgwriter_stats()
            mem_after = get_mem_usage_mb()
            tp_metrics = parse_sysbench(tp_out)
            tps = tp_metrics.get("tps", 0)
            p95 = tp_metrics.get("p95_ms")
            recovery = round(100.0 * tps / tp_baseline_tps, 1) if tp_baseline_tps and tps else None
            row = {
                "config": cfg["name"],
                "work_mem_mb": wm,
                "ap_concurrency": ap_conc,
                "tps": tps,
                "tps_recovery_pct": recovery,
                "p95_ms": p95,
                "cache_hit_pct": stats_after.get("cache_hit_pct"),
                "gaussdb_rss_mb": mem_after,
            }
            all_results.append(row)
            print(f"      TPS={tps}, recovery={recovery}%, p95={p95}ms, cache_hit={row['cache_hit_pct']}%")
    # Print comparison table
    print("\n  ─── Exp3 Summary Table ───")
    print(f"  {'Config':<12} {'AP':>4} {'TPS':>8} {'Recovery%':>10} {'p95(ms)':>9} {'Cache%':>8}")
    print(f"  {'-'*55}")
    for r in all_results:
        print(f"  {r['config']:<12} {r['ap_concurrency']:>4} {r['tps']:>8.1f} "
              f"{str(r.get('tps_recovery_pct','?')):>10} {str(r.get('p95_ms','?')):>9} "
              f"{str(r.get('cache_hit_pct','?')):>8}")
    out_path = f"{RESULTS_DIR}/exp3_mixed_workload.json"
    with open(out_path, "w") as f:
        json.dump({"tp_baseline_tps": tp_baseline_tps, "tuned_work_mem_mb": tuned_wm,
                   "rows": all_results}, f, indent=2)
    print(f"  Saved: {out_path}")
    # Reset work_mem
    set_work_mem(64)
    return all_results

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"GaussTune Experiment Runner — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Results dir: {RESULTS_DIR}")

    baseline_tps, baseline_p95 = exp1_tp_baseline()
    m1_thresh, exp2_rows = exp2_workmem_threshold()
    exp3_rows = exp3_mixed(baseline_tps, m1_thresh)

    print("\n" + "="*60)
    print("ALL EXPERIMENTS COMPLETE")
    print(f"  Exp1 Baseline TPS : {baseline_tps}")
    print(f"  Exp2 M1 Threshold : {m1_thresh} MB")
    print(f"  Results           : {RESULTS_DIR}/")
    print("="*60)
