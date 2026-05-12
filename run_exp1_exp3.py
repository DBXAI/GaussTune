#!/usr/bin/env python3
"""Re-run Experiments 1 and 3 with fixes:
 - --db-ps-mode=disable for sysbench
 - pg_stat_database for cache hit ratio
 - Fixed AP process management via script file
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
    "--tables=10 --table-size=100000 "
    "--db-ps-mode=disable"
)

def omm(cmd):
    r = subprocess.run(
        ["su", "-", "omm", "-c", cmd],
        input=OMM_PASS + "\n", capture_output=True, text=True
    )
    return r.stdout, r.stderr

def gsql(sql, db="postgres"):
    out, _ = omm(f'/opt/openGauss/app/bin/gsql -d {db} -c "{sql}"')
    return out

def set_work_mem(mb):
    gsql(f"ALTER SYSTEM SET work_mem = '{mb}MB'; SELECT pg_reload_conf();")
    time.sleep(2)

def get_cache_hit_pct():
    out = gsql("SELECT blks_hit, blks_read FROM pg_stat_database WHERE datname='sbtest';")
    nums = re.findall(r"\d+", out)
    if len(nums) >= 2:
        hit, read = int(nums[-2]), int(nums[-1])
        total = hit + read
        return round(100.0 * hit / total, 2) if total > 0 else 100.0
    return None

def get_db_stats_delta(before, after):
    """Compute cache hit % from delta of pg_stat_database counters."""
    try:
        dh = after["blks_hit"] - before["blks_hit"]
        dr = after["blks_read"] - before["blks_read"]
        total = dh + dr
        return round(100.0 * dh / total, 2) if total > 0 else 100.0
    except Exception:
        return None

def get_db_stats_raw():
    out = gsql("SELECT blks_hit, blks_read FROM pg_stat_database WHERE datname='sbtest';")
    nums = re.findall(r"\d+", out)
    if len(nums) >= 2:
        return {"blks_hit": int(nums[-2]), "blks_read": int(nums[-1])}
    return {"blks_hit": 0, "blks_read": 0}

def get_gaussdb_rss_mb():
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
        timeout=duration_s + 90
    )
    return r.stdout

def launch_ap_workers(count, work_mem_mb):
    procs = []
    for i in range(count):
        p = subprocess.Popen(
            ["su", "-", "omm", "-c", f"/tmp/ap_loop.sh {work_mem_mb}"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        p.stdin.write((OMM_PASS + "\n").encode())
        p.stdin.flush()
        procs.append(p)
    time.sleep(3)  # let workers start
    return procs

def kill_ap_workers(procs):
    for p in procs:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()

# ─────────────────────────────────────────────────────────────────────────────
# EXPERIMENT 1: TP-only baseline
# ─────────────────────────────────────────────────────────────────────────────
def exp1_tp_baseline():
    print("\n" + "="*60)
    print("EXPERIMENT 1: TP-only Baseline (16 threads, 300s × 3)")
    print("="*60)
    set_work_mem(64)
    results = []
    for run_id in range(1, 4):
        print(f"\n  Run {run_id}/3 — warmup 30s + measure 300s ...")
        run_sysbench(threads=16, duration_s=30)     # warmup
        stats_before = get_db_stats_raw()
        mem_rss = get_gaussdb_rss_mb()
        out = run_sysbench(threads=16, duration_s=300)  # measure
        stats_after = get_db_stats_raw()
        cache_hit = get_db_stats_delta(stats_before, stats_after)
        metrics = parse_sysbench(out)
        metrics.update({
            "run": run_id,
            "cache_hit_pct": cache_hit,
            "gaussdb_rss_mb": mem_rss,
            "spill": 0,
        })
        results.append(metrics)
        print(f"    TPS={metrics.get('tps','?'):.1f}, "
              f"p95={metrics.get('p95_ms','?')}ms, "
              f"cache_hit={cache_hit}%, "
              f"RSS={mem_rss}MB")
    tps_vals = [r["tps"] for r in results if "tps" in r]
    p95_vals = [r["p95_ms"] for r in results if "p95_ms" in r]
    avg_tps = round(sum(tps_vals)/len(tps_vals), 1) if tps_vals else None
    avg_p95 = round(sum(p95_vals)/len(p95_vals), 1) if p95_vals else None
    print(f"\n  ─── Exp1 Summary ───")
    print(f"  Avg TPS: {avg_tps} | Avg p95: {avg_p95} ms")
    out_path = f"{RESULTS_DIR}/exp1_tp_baseline.json"
    with open(out_path, "w") as f:
        json.dump({"config": {"shared_buffers": "4GB", "work_mem": "64MB"},
                   "summary": {"avg_tps": avg_tps, "avg_p95_ms": avg_p95},
                   "runs": results}, f, indent=2)
    print(f"  Saved: {out_path}")
    return avg_tps, avg_p95

# ─────────────────────────────────────────────────────────────────────────────
# EXPERIMENT 3: TP+AP mixed (Default vs Tuned)
# ─────────────────────────────────────────────────────────────────────────────
def exp3_mixed(tp_baseline_tps, m1_threshold_mb=160):
    print("\n" + "="*60)
    print("EXPERIMENT 3: TP+AP Mixed Workload (Default vs Tuned)")
    print(f"  TP baseline TPS={tp_baseline_tps}, Tuned work_mem={m1_threshold_mb}MB")
    print("="*60)
    configs = [
        {"name": "Default", "work_mem_mb": 64},
        {"name": "Tuned",   "work_mem_mb": m1_threshold_mb},
    ]
    ap_concurrencies = [1, 2, 4]
    all_results = []
    for cfg in configs:
        wm = cfg["work_mem_mb"]
        print(f"\n  ── Config: {cfg['name']} (work_mem={wm}MB) ──")
        set_work_mem(wm)
        for ap_conc in ap_concurrencies:
            print(f"\n    AP concurrency={ap_conc}: warmup 20s → measure 120s ...")
            run_sysbench(threads=16, duration_s=20)   # warmup without AP
            workers = launch_ap_workers(ap_conc, wm)
            time.sleep(2)
            stats_before = get_db_stats_raw()
            tp_out = run_sysbench(threads=16, duration_s=120)
            stats_after = get_db_stats_raw()
            kill_ap_workers(workers)
            cache_hit = get_db_stats_delta(stats_before, stats_after)
            mem_rss = get_gaussdb_rss_mb()
            m = parse_sysbench(tp_out)
            tps = m.get("tps", 0)
            p95 = m.get("p95_ms")
            recovery = round(100.0 * tps / tp_baseline_tps, 1) if tp_baseline_tps and tps else None
            row = {
                "config": cfg["name"],
                "work_mem_mb": wm,
                "ap_concurrency": ap_conc,
                "tps": tps,
                "tps_recovery_pct": recovery,
                "p95_ms": p95,
                "cache_hit_pct": cache_hit,
                "gaussdb_rss_mb": mem_rss,
            }
            all_results.append(row)
            print(f"      TPS={tps:.1f}, recovery={recovery}%, "
                  f"p95={p95}ms, cache_hit={cache_hit}%")
    print(f"\n  ─── Exp3 Summary Table ───")
    print(f"  {'Config':<10} {'AP':>4} {'TPS':>8} {'Recov%':>7} {'p95ms':>7} {'CacheHit%':>10}")
    print(f"  {'-'*52}")
    for r in all_results:
        print(f"  {r['config']:<10} {r['ap_concurrency']:>4} "
              f"{r['tps']:>8.1f} {str(r.get('tps_recovery_pct','?')):>7} "
              f"{str(r.get('p95_ms','?')):>7} {str(r.get('cache_hit_pct','?')):>10}")
    out_path = f"{RESULTS_DIR}/exp3_mixed_workload.json"
    with open(out_path, "w") as f:
        json.dump({"tp_baseline_tps": tp_baseline_tps,
                   "tuned_work_mem_mb": m1_threshold_mb,
                   "rows": all_results}, f, indent=2)
    print(f"  Saved: {out_path}")
    set_work_mem(64)
    return all_results

if __name__ == "__main__":
    print(f"GaussTune Exp1+Exp3 Re-run — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    baseline_tps, baseline_p95 = exp1_tp_baseline()
    exp3_rows = exp3_mixed(baseline_tps, m1_threshold_mb=160)
    print("\n" + "="*60)
    print("DONE")
    print(f"  Exp1 Baseline TPS : {baseline_tps}")
    print("="*60)
