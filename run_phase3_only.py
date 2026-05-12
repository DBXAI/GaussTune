#!/usr/bin/env python3
"""
GaussTune: Phase 3 only — Default vs Tuned comparison per AP form.

Loads Phase 0 WM scan results from v3_wm_scan.json.
Runs 12 measurements: 4 configs × 3 AP forms.
Fixes: proper omm-owned process cleanup, longer warmup, longer restart wait.

Configs compared per AP form:
  Default  : SB=2GB, WM=64MB  (memory-limited, WM below M1 for most forms)
  WM-Tuned : SB=2GB, WM=M1   (WM raised to no-spill threshold)
  SB-Tuned : SB=4GB, WM=64MB  (SB covers dataset)
  Joint    : SB=4GB, WM=M1   (both SB and WM jointly optimized)
"""
import subprocess, time, os, json, re, glob
from datetime import datetime

RESULTS_DIR = "/home/node/GaussTune/refine-logs/results"
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

TP_THREADS   = 16
TP_WARMUP    = 60    # longer warmup for stable 4GB buffer
TP_MEASURE   = 120
AP_CONC      = 2
BASELINE_2GB = 1465.6   # TP-only at SB=2GB (from run_tuning.py phase1)
BASELINE_4GB = 1203.6   # TP-only at SB=4GB (from run_tuning_v3.py phase1)

# ── Helpers ──────────────────────────────────────────────────────────────────
def omm_run(cmd, timeout=60):
    r = subprocess.run(
        ["su", "-", "omm", "-c", cmd],
        input=OMM_PASS + "\n", capture_output=True, text=True, timeout=timeout
    )
    return r.stdout, r.stderr

def gsql_sql(sql, db="sbtest"):
    tmp = "/tmp/gt_p3.sql"
    with open(tmp, "w") as f: f.write(sql)
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
    for _ in range(16):
        out, _ = omm_run(f"{GSQL} -d postgres -c \"SELECT 1;\"", timeout=10)
        if "1" in out:
            return True
        time.sleep(4)
    return False

def wm_to_mb(wm_str):
    return int(wm_str[:-2]) * 1024 if wm_str.endswith("GB") else int(wm_str[:-2])

def get_db_stats():
    out, _ = gsql_sql(
        "SELECT blks_hit, blks_read FROM pg_stat_database WHERE datname='sbtest';",
        db="postgres"
    )
    nums = re.findall(r"\d+", out)
    return {"blks_hit": int(nums[-2]), "blks_read": int(nums[-1])} if len(nums) >= 2 else {"blks_hit": 0, "blks_read": 0}

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
        if mo: m[key] = float(mo.group(1))
    return m

def run_sysbench(threads, duration_s):
    cmd = f"{SB_BASE} --threads={threads} --time={duration_s} run"
    out, _ = omm_run(cmd, timeout=duration_s + 90)
    return out

def launch_ap_workers(sql_name, wm_mb, count):
    """Launch AP worker loop scripts as omm user. Records PID in /tmp for clean kill."""
    # Remove any stale PID files from this sql_name
    for pf in glob.glob(f"/tmp/ap_p3_{sql_name}_*.pid"):
        try: os.unlink(pf)
        except: pass

    sql_oneline = re.sub(r"\s+", " ", AP_SQLS[sql_name].strip())
    full_cmd = f"SET work_mem='{wm_mb}MB'; {sql_oneline}"
    script = (
        f"#!/bin/bash\n"
        f"echo $$ > /tmp/ap_p3_{sql_name}_$$.pid\n"
        f"while true; do {GSQL} -d sbtest -c \"{full_cmd}\" >/dev/null 2>&1; done\n"
    )
    path = f"/tmp/ap_p3_{sql_name}.sh"
    with open(path, "w") as f: f.write(script)
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
    time.sleep(8)   # give workers time to start and write PID files
    return procs

def kill_workers(procs, sql_name):
    """Robustly kill AP workers: cancel DB queries, kill bash PIDs, terminate wrappers."""
    # 1. Cancel active AP queries in DB
    cancel_sql = ("SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                  "WHERE state='active' AND query LIKE '%sbtest%' AND pid != pg_backend_pid();")
    try:
        gsql_sql(cancel_sql, db="postgres")
    except Exception:
        pass

    # 2. Kill bash loops via omm kill (they outlive su wrapper)
    pid_files = glob.glob(f"/tmp/ap_p3_{sql_name}_*.pid")
    if pid_files:
        pids = []
        for pf in pid_files:
            try:
                pid = open(pf).read().strip()
                if pid.isdigit(): pids.append(pid)
                os.unlink(pf)
            except: pass
        if pids:
            omm_run(f"kill -9 {' '.join(pids)} 2>/dev/null", timeout=10)

    # 3. Terminate the su wrapper processes
    for p in procs:
        p.terminate()
        try: p.wait(timeout=5)
        except: p.kill()

    # 4. Brief wait for any in-flight gsql to release connections
    time.sleep(10)

def measure_mixed(sb, wm, sql_name, ref_tps, label):
    """Configure DB, warmup TP, launch AP workers, measure TP for TP_MEASURE s."""
    wm_mb = wm_to_mb(wm)
    print(f"    {label} (SB={sb}, WM={wm}) ...", end=" ", flush=True)
    set_guc("shared_buffers", sb)
    set_guc("work_mem", wm)
    ok = restart_db()
    if not ok:
        print("DB RESTART FAILED — retrying once ...")
        time.sleep(30)
        ok = restart_db()
        if not ok:
            print("FAILED TWICE, skipping.")
            return None

    # Warmup TP (fills buffer cache)
    run_sysbench(TP_THREADS, TP_WARMUP)

    # Launch AP workers
    workers = launch_ap_workers(sql_name, wm_mb, AP_CONC)

    # Measure TP under AP load
    sb_before = get_db_stats()
    tp_out = run_sysbench(TP_THREADS, TP_MEASURE)
    sb_after = get_db_stats()

    # Kill AP workers
    kill_workers(workers, sql_name)

    m = parse_sysbench(tp_out)
    tps = m.get("tps", 0)
    p95 = m.get("p95_ms")
    hit = cache_hit_pct(sb_before, sb_after)
    recovery = round(100.0 * tps / ref_tps, 1) if ref_tps and tps else None
    print(f"TPS={tps:.1f}, recovery={recovery}%, p95={p95}ms, cache_hit={hit}%")
    return {
        "label": label, "sb": sb, "wm": wm, "sql": sql_name,
        "ap_concurrency": AP_CONC, "tps": tps, "p95_ms": p95,
        "cache_hit_pct": hit, "ref_tps": ref_tps, "recovery_pct": recovery,
    }


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    t0 = datetime.now()
    print(f"\nGaussTune Phase 3 — {t0.strftime('%Y-%m-%d %H:%M:%S')}")

    # Load WM scan results (M1 thresholds)
    wm_scan_path = f"{RESULTS_DIR}/v3_wm_scan.json"
    with open(wm_scan_path) as f:
        wm_scan = json.load(f)
    print("M1 thresholds from WM scan:")
    for sql_name, r in wm_scan.items():
        print(f"  {sql_name}: M1 = {r['m1_mb']}MB")

    print(f"\nTP-only baselines: SB=2GB → {BASELINE_2GB} TPS | SB=4GB → {BASELINE_4GB} TPS")
    print(f"AP concurrency: {AP_CONC} | TP threads: {TP_THREADS} | Warmup: {TP_WARMUP}s | Measure: {TP_MEASURE}s")

    print(f"\n{'='*70}")
    print("PHASE 3: Default vs Tuned comparison per AP form")
    print(f"{'='*70}")

    all_results = []
    total = len(AP_SQLS) * 4
    idx = 0

    for sql_name in AP_SQLS:
        m1_mb = wm_scan[sql_name]["m1_mb"]
        m1_str = f"{m1_mb}MB"
        print(f"\n  ── AP form: {sql_name}  (M1={m1_mb}MB) ──")

        configs = [
            {"label": "Default",  "sb": "2GB", "wm": "64MB", "ref": BASELINE_2GB},
            {"label": "WM-Tuned", "sb": "2GB", "wm": m1_str, "ref": BASELINE_2GB},
            {"label": "SB-Tuned", "sb": "4GB", "wm": "64MB", "ref": BASELINE_4GB},
            {"label": "Joint",    "sb": "4GB", "wm": m1_str, "ref": BASELINE_4GB},
        ]

        for cfg in configs:
            idx += 1
            print(f"  [{idx}/{total}]", end=" ")
            result = measure_mixed(cfg["sb"], cfg["wm"], sql_name, cfg["ref"], cfg["label"])
            if result:
                all_results.append(result)

    # ── Print summary table ───────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("PHASE 3 SUMMARY — TPS Recovery vs TP-only Baseline")
    print(f"{'='*70}")
    header = f"  {'AP SQL':<16} {'Config':<12} {'SB':>5} {'WM':>6} {'TPS':>8} {'Recov%':>8} {'p95ms':>7}"
    print(header)
    print(f"  {'-'*65}")
    for r in all_results:
        tps_s = f"{r['tps']:.1f}" if r.get("tps") else "N/A"
        rec_s = str(r.get("recovery_pct", "?"))
        p95_s = str(r.get("p95_ms", "?"))
        print(f"  {r['sql']:<16} {r['label']:<12} {r['sb']:>5} {r['wm']:>6} "
              f"{tps_s:>8} {rec_s:>8} {p95_s:>7}")

    # ── Restore and save ──────────────────────────────────────────────────────
    set_guc("shared_buffers", "4GB")
    set_guc("work_mem", "64MB")
    restart_db()

    out_path = f"{RESULTS_DIR}/v3_phase3_comparison.json"
    with open(out_path, "w") as f:
        json.dump({
            "baseline_2gb": BASELINE_2GB,
            "baseline_4gb": BASELINE_4GB,
            "wm_scan": wm_scan,
            "comparison": all_results,
            "ap_concurrency": AP_CONC,
            "tp_threads": TP_THREADS,
            "warmup_s": TP_WARMUP,
            "measure_s": TP_MEASURE,
        }, f, indent=2)
    print(f"\n  Saved: {out_path}")

    elapsed = (datetime.now() - t0).total_seconds() / 60
    print(f"\nAll done — {elapsed:.1f} min")
