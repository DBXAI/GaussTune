#!/usr/bin/env python3
"""
GaussTune: complete the remaining 4 combos (3GB SB × WM 256/512/768/1GB)
then run Phase 3. Injects the 16 already-measured results from the previous run.
"""
import subprocess, time, os, json, re
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
AP_SQLS = {
    "sort_heavy": "SELECT k, c, pad FROM sbtest1 ORDER BY c DESC, pad ASC, k DESC",
    "hashjoin_agg": (
        "SELECT a.k, COUNT(*) AS cnt, AVG(a.k) AS avg_k, MAX(LENGTH(a.c)) AS max_clen "
        "FROM sbtest1 a JOIN sbtest2 b ON a.k = b.k "
        "WHERE a.k % 1000 < 100 "
        "GROUP BY a.k ORDER BY cnt DESC, avg_k DESC LIMIT 5000"
    ),
    "multilevel_agg": (
        "SELECT bucket, SUM(cnt) AS total_cnt, AVG(avg_k) AS global_avg_k FROM ("
        "SELECT k%1000 AS bucket, COUNT(*) AS cnt, AVG(k) AS avg_k FROM sbtest1 GROUP BY k%1000 "
        "UNION ALL "
        "SELECT k%1000 AS bucket, COUNT(*) AS cnt, AVG(k) AS avg_k FROM sbtest2 GROUP BY k%1000 "
        "UNION ALL "
        "SELECT k%1000 AS bucket, COUNT(*) AS cnt, AVG(k) AS avg_k FROM sbtest3 GROUP BY k%1000"
        ") sub GROUP BY bucket ORDER BY total_cnt DESC"
    ),
}
TP_THREADS = 16
TP_WARMUP  = 30
TP_MEASURE = 120
AP_CONC    = 2
BASELINE_TPS = 1465.6  # from phase1

# ── 16 already-measured results from previous run ─────────────────────────────
KNOWN_ROWS = [
    {"shared_buffers":"512MB","work_mem":"64MB",  "tps":418.4,"tps_recovery_pct":28.6,"p95_ms":121.08,"cache_hit_pct":100.0},
    {"shared_buffers":"512MB","work_mem":"256MB", "tps":358.8,"tps_recovery_pct":24.5,"p95_ms":127.81,"cache_hit_pct":0.0},
    {"shared_buffers":"512MB","work_mem":"512MB", "tps":362.4,"tps_recovery_pct":24.7,"p95_ms":132.49,"cache_hit_pct":0.0},
    {"shared_buffers":"512MB","work_mem":"768MB", "tps":374.2,"tps_recovery_pct":25.5,"p95_ms":118.92,"cache_hit_pct":0.0},
    {"shared_buffers":"512MB","work_mem":"1GB",   "tps":325.9,"tps_recovery_pct":22.2,"p95_ms":147.61,"cache_hit_pct":100.0},
    {"shared_buffers":"1GB",  "work_mem":"64MB",  "tps":565.5,"tps_recovery_pct":38.6,"p95_ms":92.42, "cache_hit_pct":0.0},
    {"shared_buffers":"1GB",  "work_mem":"256MB", "tps":463.6,"tps_recovery_pct":31.6,"p95_ms":114.72,"cache_hit_pct":100.0},
    {"shared_buffers":"1GB",  "work_mem":"512MB", "tps":451.0,"tps_recovery_pct":30.8,"p95_ms":116.8, "cache_hit_pct":100.0},
    {"shared_buffers":"1GB",  "work_mem":"768MB", "tps":462.2,"tps_recovery_pct":31.5,"p95_ms":97.55, "cache_hit_pct":0.0},
    {"shared_buffers":"1GB",  "work_mem":"1GB",   "tps":447.3,"tps_recovery_pct":30.5,"p95_ms":108.68,"cache_hit_pct":100.0},
    {"shared_buffers":"2GB",  "work_mem":"64MB",  "tps":713.5,"tps_recovery_pct":48.7,"p95_ms":48.34, "cache_hit_pct":100.0},
    {"shared_buffers":"2GB",  "work_mem":"256MB", "tps":682.9,"tps_recovery_pct":46.6,"p95_ms":49.21, "cache_hit_pct":100.0},
    {"shared_buffers":"2GB",  "work_mem":"512MB", "tps":689.0,"tps_recovery_pct":47.0,"p95_ms":41.1,  "cache_hit_pct":100.0},
    {"shared_buffers":"2GB",  "work_mem":"768MB", "tps":664.7,"tps_recovery_pct":45.4,"p95_ms":43.39, "cache_hit_pct":100.0},
    {"shared_buffers":"2GB",  "work_mem":"1GB",   "tps":615.0,"tps_recovery_pct":42.0,"p95_ms":51.02, "cache_hit_pct":100.0},
    {"shared_buffers":"3GB",  "work_mem":"64MB",  "tps":682.8,"tps_recovery_pct":46.6,"p95_ms":35.59, "cache_hit_pct":100.0},
]
MISSING = [
    ("3GB", "256MB"), ("3GB", "512MB"), ("3GB", "768MB"), ("3GB", "1GB"),
]

def omm_run(cmd, timeout=60):
    r = subprocess.run(["su","-","omm","-c",cmd],
        input=OMM_PASS+"\n", capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr

def gsql_sql(sql, db="sbtest"):
    tmp = "/tmp/gt_fin.sql"
    with open(tmp,"w") as f: f.write(sql)
    os.chmod(tmp, 0o644)
    return omm_run(f"{GSQL} -d {db} -f {tmp}", timeout=600)

def set_guc(param, value):
    gsql_sql(f"ALTER SYSTEM SET {param} = '{value}'; SELECT pg_reload_conf();", db="postgres")
    time.sleep(3)

def restart_db():
    omm_run(
        "export GAUSSHOME=/opt/openGauss/app; export PATH=$GAUSSHOME/bin:$PATH; "
        "export LD_LIBRARY_PATH=$GAUSSHOME/lib; gs_ctl restart -D /opt/openGauss/data",
        timeout=120
    )
    time.sleep(12)
    for _ in range(8):
        out, _ = omm_run(f"{GSQL} -d postgres -c \"SELECT 1;\"", timeout=10)
        if "1" in out:
            return True
        time.sleep(3)
    return False

def get_db_stats():
    out, _ = gsql_sql("SELECT blks_hit, blks_read FROM pg_stat_database WHERE datname='sbtest';", db="postgres")
    nums = re.findall(r"\d+", out)
    return {"blks_hit": int(nums[-2]), "blks_read": int(nums[-1])} if len(nums) >= 2 else {"blks_hit":0,"blks_read":0}

def cache_hit_pct(before, after):
    dh = after["blks_hit"] - before["blks_hit"]
    dr = after["blks_read"] - before["blks_read"]
    total = dh + dr
    return round(100.0 * dh / total, 2) if total > 0 else 100.0

def parse_sb(output):
    m = {}
    for pat, key in [(r"transactions:\s+\d+\s+\(([\d.]+) per sec\.\)","tps"),(r"95th percentile:\s+([\d.]+)","p95_ms")]:
        mo = re.search(pat, output)
        if mo: m[key] = float(mo.group(1))
    return m

def run_sb(threads, duration):
    out, _ = omm_run(f"{SB_BASE} --threads={threads} --time={duration} run", timeout=duration+90)
    return out

def wm_to_mb(wm):
    return int(wm[:-2]) * 1024 if wm.endswith("GB") else int(wm[:-2])

def launch_ap(sql_name, wm_mb, count):
    sql = AP_SQLS[sql_name]
    script = f"#!/bin/bash\nwhile true; do {GSQL} -d sbtest -c \"SET work_mem='{wm_mb}MB'; {sql}\" >/dev/null 2>&1; done\n"
    path = f"/tmp/ap_{sql_name}.sh"
    with open(path,"w") as f: f.write(script)
    os.chmod(path, 0o755)
    procs = []
    for _ in range(count):
        p = subprocess.Popen(["su","-","omm","-c",path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.stdin.write((OMM_PASS+"\n").encode()); p.stdin.flush()
        procs.append(p)
    time.sleep(4)
    return procs

def kill_ap(procs):
    for p in procs:
        p.terminate()
        try: p.wait(timeout=5)
        except: p.kill()

def measure_mixed(sb, wm, ap_sql="hashjoin_agg", ap_conc=AP_CONC):
    wm_mb = wm_to_mb(wm)
    set_guc("shared_buffers", sb); set_guc("work_mem", wm)
    ok = restart_db()
    if not ok:
        print("    !! restart failed"); return None
    run_sb(TP_THREADS, TP_WARMUP)
    workers = launch_ap(ap_sql, wm_mb, ap_conc)
    sb_before = get_db_stats()
    tp_out = run_sb(TP_THREADS, TP_MEASURE)
    sb_after = get_db_stats()
    kill_ap(workers)
    m = parse_sb(tp_out)
    tps = m.get("tps", 0)
    recovery = round(100.0 * tps / BASELINE_TPS, 1) if tps else None
    return {"shared_buffers": sb, "work_mem": wm, "tps": tps,
            "tps_recovery_pct": recovery, "p95_ms": m.get("p95_ms"),
            "cache_hit_pct": cache_hit_pct(sb_before, sb_after)}

# ── complete missing 4 combos ─────────────────────────────────────────────────
print(f"\nGaussTune: completing missing combos — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
all_rows = list(KNOWN_ROWS)

for i, (sb, wm) in enumerate(MISSING, 1):
    print(f"\n  [missing {i}/4] SB={sb}, WM={wm} ...")
    row = measure_mixed(sb, wm)
    if row:
        all_rows.append(row)
        print(f"    TPS={row['tps']:.1f}, recovery={row['tps_recovery_pct']}%, "
              f"p95={row['p95_ms']}ms, cache_hit={row['cache_hit_pct']}%")

# ── rank and print full grid ──────────────────────────────────────────────────
ranked = sorted(all_rows, key=lambda r: r.get("tps",0), reverse=True)
print(f"\n{'='*60}")
print("FULL GRID SEARCH RESULTS (ranked by TPS, AP=hashjoin_agg, conc=2)")
print(f"  Baseline TPS = {BASELINE_TPS}")
print(f"{'='*60}")
print(f"  {'SB':<8} {'WM':<8} {'TPS':>8} {'Recov%':>8} {'p95ms':>8} {'CacheHit%':>10}")
print(f"  {'-'*57}")
for r in ranked:
    print(f"  {r['shared_buffers']:<8} {r['work_mem']:<8} {r['tps']:>8.1f} "
          f"{str(r.get('tps_recovery_pct','?')):>8} {str(r.get('p95_ms','?')):>8} "
          f"{str(r.get('cache_hit_pct','?')):>10}")

with open(f"{RESULTS_DIR}/tuning_grid_search.json","w") as f:
    json.dump({"baseline_tps": BASELINE_TPS, "ap_sql": "hashjoin_agg",
               "ap_concurrency": AP_CONC, "rows": all_rows, "ranked": ranked}, f, indent=2)
print(f"\n  Saved: {RESULTS_DIR}/tuning_grid_search.json")

best = ranked[0]
print(f"\n  Best config: SB={best['shared_buffers']}, WM={best['work_mem']}, "
      f"TPS={best['tps']:.1f} ({best['tps_recovery_pct']}%)")

# ── Phase 3: Best vs Default across all AP SQL types & concurrencies ──────────
print(f"\n{'='*60}")
print("PHASE 3: Best vs Default — all AP SQL types × concurrencies")
print(f"{'='*60}")

configs = [
    {"name": "Default", "sb": "2GB",                    "wm": "256MB"},
    {"name": "Best",    "sb": best["shared_buffers"],    "wm": best["work_mem"]},
]
ap_concurrencies = [1, 2, 4]
phase3_rows = []

for cfg in configs:
    print(f"\n  ── {cfg['name']} (SB={cfg['sb']}, WM={cfg['wm']}) ──")
    set_guc("shared_buffers", cfg["sb"]); set_guc("work_mem", cfg["wm"])
    restart_db()
    wm_mb = wm_to_mb(cfg["wm"])
    for sql_name in AP_SQLS:
        for ap_conc in ap_concurrencies:
            print(f"    AP={sql_name}, conc={ap_conc} ...", end=" ", flush=True)
            run_sb(TP_THREADS, TP_WARMUP)
            workers = launch_ap(sql_name, wm_mb, ap_conc)
            sb_before = get_db_stats()
            tp_out = run_sb(TP_THREADS, TP_MEASURE)
            sb_after = get_db_stats()
            kill_ap(workers)
            m = parse_sb(tp_out)
            tps = m.get("tps", 0)
            recovery = round(100.0 * tps / BASELINE_TPS, 1) if tps else None
            hit = cache_hit_pct(sb_before, sb_after)
            row = {"config": cfg["name"], "shared_buffers": cfg["sb"], "work_mem": cfg["wm"],
                   "ap_sql": sql_name, "ap_concurrency": ap_conc,
                   "tps": tps, "tps_recovery_pct": recovery,
                   "p95_ms": m.get("p95_ms"), "cache_hit_pct": hit}
            phase3_rows.append(row)
            print(f"TPS={tps:.1f} ({recovery}%), cache_hit={hit}%")

print(f"\n  ─── Phase 3 Summary ───")
print(f"  {'Config':<9} {'AP SQL':<16} {'AP':>3} {'TPS':>8} {'Recov%':>8} {'p95ms':>8} {'Cache%':>8}")
print(f"  {'-'*65}")
for r in phase3_rows:
    print(f"  {r['config']:<9} {r['ap_sql']:<16} {r['ap_concurrency']:>3} "
          f"{r['tps']:>8.1f} {str(r.get('tps_recovery_pct','?')):>8} "
          f"{str(r.get('p95_ms','?')):>8} {str(r.get('cache_hit_pct','?')):>8}")

with open(f"{RESULTS_DIR}/tuning_best_vs_default.json","w") as f:
    json.dump({"baseline_tps": BASELINE_TPS, "best_config": best,
               "rows": phase3_rows}, f, indent=2)
print(f"\n  Saved: {RESULTS_DIR}/tuning_best_vs_default.json")

# restore
set_guc("shared_buffers","4GB"); set_guc("work_mem","64MB"); restart_db()
print(f"\n{'='*60}\nALL DONE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}")
