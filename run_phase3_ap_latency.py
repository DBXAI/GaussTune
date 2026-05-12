#!/usr/bin/env python3
"""
GaussTune Phase 3 — AP latency under mixed load.

Same 12 configs as run_phase3_only.py, but AP workers log per-query
wall-clock time to /tmp/ap_lat_<sql>_<wid>.log so we can compute
mean / p50 / p95 / p99 AP latency under concurrent TP load.
"""
import subprocess, time, os, json, re, glob, statistics
from datetime import datetime

RESULTS_DIR = "/home/node/GaussTune/refine-logs/results"
OMM_PASS    = "1997"
GSQL        = "/opt/openGauss/app/bin/gsql"
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
TP_WARMUP    = 60
TP_MEASURE   = 120
AP_CONC      = 2
BASELINE_2GB = 1465.6
BASELINE_4GB = 1203.6

# ── Helpers ──────────────────────────────────────────────────────────────────
def omm_run(cmd, timeout=60):
    r = subprocess.run(
        ["su", "-", "omm", "-c", cmd],
        input=OMM_PASS + "\n", capture_output=True, text=True, timeout=timeout
    )
    return r.stdout, r.stderr

def gsql_sql(sql, db="sbtest"):
    tmp = "/tmp/gt_lat.sql"
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
    """Launch AP workers that log per-query wall-clock time (ms) to a file."""
    for pf in glob.glob(f"/tmp/ap_lat_{sql_name}_*.pid"):
        try: os.unlink(pf)
        except: pass
    for lf in glob.glob(f"/tmp/ap_lat_{sql_name}_*.log"):
        try: os.unlink(lf)
        except: pass

    sql_oneline = re.sub(r"\s+", " ", AP_SQLS[sql_name].strip())
    full_cmd = f"SET work_mem='{wm_mb}MB'; {sql_oneline}"

    # Worker script: record PID, then loop timing each gsql execution in ms
    script = (
        f"#!/bin/bash\n"
        f"LOGFILE=/tmp/ap_lat_{sql_name}_$$.log\n"
        f"echo $$ > /tmp/ap_lat_{sql_name}_$$.pid\n"
        f"while true; do\n"
        f"  T0=$(date +%s%3N)\n"
        f"  {GSQL} -d sbtest -c \"{full_cmd}\" >/dev/null 2>&1\n"
        f"  T1=$(date +%s%3N)\n"
        f"  echo $((T1 - T0)) >> $LOGFILE\n"
        f"done\n"
    )
    path = f"/tmp/ap_lat_{sql_name}.sh"
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
    time.sleep(8)
    return procs

def kill_workers(procs, sql_name):
    """Kill workers and return list of per-query latency samples (ms)."""
    # 1. Cancel active AP queries in DB
    cancel_sql = ("SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                  "WHERE state='active' AND query LIKE '%sbtest%' AND pid != pg_backend_pid();")
    try:
        gsql_sql(cancel_sql, db="postgres")
    except Exception:
        pass

    # 2. Kill bash loops via PID files
    pid_files = glob.glob(f"/tmp/ap_lat_{sql_name}_*.pid")
    pids = []
    for pf in pid_files:
        try:
            pid = open(pf).read().strip()
            if pid.isdigit(): pids.append(pid)
            os.unlink(pf)
        except: pass
    if pids:
        omm_run(f"kill -9 {' '.join(pids)} 2>/dev/null", timeout=10)

    # 3. Terminate su wrappers
    for p in procs:
        p.terminate()
        try: p.wait(timeout=5)
        except: p.kill()

    time.sleep(10)

    # 4. Collect latency samples from log files (only complete executions)
    samples = []
    for lf in glob.glob(f"/tmp/ap_lat_{sql_name}_*.log"):
        try:
            with open(lf) as f:
                for line in f:
                    line = line.strip()
                    if line.isdigit():
                        samples.append(int(line))
            os.unlink(lf)
        except: pass
    return samples

def ap_stats(samples):
    """Compute mean/p50/p95/p99 from latency samples (ms)."""
    if not samples:
        return {"count": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    s = sorted(samples)
    n = len(s)
    def pct(p): return s[int(n * p / 100)]
    return {
        "count":   n,
        "mean_ms": round(statistics.mean(s), 0),
        "p50_ms":  pct(50),
        "p95_ms":  pct(95),
        "p99_ms":  pct(99),
    }

def measure_mixed(sb, wm, sql_name, ref_tps, label):
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

    run_sysbench(TP_THREADS, TP_WARMUP)

    workers = launch_ap_workers(sql_name, wm_mb, AP_CONC)

    sb_before = get_db_stats()
    tp_out    = run_sysbench(TP_THREADS, TP_MEASURE)
    sb_after  = get_db_stats()

    samples = kill_workers(workers, sql_name)

    m        = parse_sysbench(tp_out)
    tps      = m.get("tps", 0)
    tp_p95   = m.get("p95_ms")
    hit      = cache_hit_pct(sb_before, sb_after)
    recovery = round(100.0 * tps / ref_tps, 1) if ref_tps and tps else None
    ap       = ap_stats(samples)

    print(
        f"TP: TPS={tps:.1f} ({recovery}%), p95={tp_p95}ms | "
        f"AP: n={ap['count']}, mean={ap['mean_ms']}ms, "
        f"p50={ap['p50_ms']}ms, p95={ap['p95_ms']}ms, p99={ap['p99_ms']}ms"
    )
    return {
        "label": label, "sb": sb, "wm": wm, "sql": sql_name,
        "ap_concurrency": AP_CONC,
        "tp_tps": tps, "tp_p95_ms": tp_p95,
        "cache_hit_pct": hit, "ref_tps": ref_tps, "recovery_pct": recovery,
        "ap_query_count": ap["count"],
        "ap_mean_ms": ap["mean_ms"],
        "ap_p50_ms":  ap["p50_ms"],
        "ap_p95_ms":  ap["p95_ms"],
        "ap_p99_ms":  ap["p99_ms"],
    }


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    t0 = datetime.now()
    print(f"\nGaussTune Phase 3 AP-Latency — {t0.strftime('%Y-%m-%d %H:%M:%S')}")

    wm_scan_path = f"{RESULTS_DIR}/v3_wm_scan.json"
    with open(wm_scan_path) as f:
        wm_scan = json.load(f)
    print("M1 thresholds:", {k: v["m1_mb"] for k, v in wm_scan.items()})
    print(f"AP concurrency: {AP_CONC} | TP threads: {TP_THREADS} | Warmup: {TP_WARMUP}s | Measure: {TP_MEASURE}s")

    print(f"\n{'='*80}")
    print("PHASE 3: AP latency under mixed TP+AP load")
    print(f"{'='*80}")

    all_results = []
    total = len(AP_SQLS) * 4
    idx   = 0

    for sql_name in AP_SQLS:
        m1_mb  = wm_scan[sql_name]["m1_mb"]
        m1_str = f"{m1_mb}MB"
        print(f"\n  ── AP form: {sql_name}  (M1={m1_mb}MB) ──")

        configs = [
            {"label": "Default",  "sb": "2GB", "wm": "64MB",  "ref": BASELINE_2GB},
            {"label": "WM-Tuned", "sb": "2GB", "wm": m1_str,  "ref": BASELINE_2GB},
            {"label": "SB-Tuned", "sb": "4GB", "wm": "64MB",  "ref": BASELINE_4GB},
            {"label": "Joint",    "sb": "4GB", "wm": m1_str,  "ref": BASELINE_4GB},
        ]
        for cfg in configs:
            idx += 1
            print(f"  [{idx}/{total}]", end=" ")
            result = measure_mixed(cfg["sb"], cfg["wm"], sql_name, cfg["ref"], cfg["label"])
            if result:
                all_results.append(result)

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print("SUMMARY — TP TPS Recovery  +  AP Query Latency under mixed load")
    print(f"{'='*100}")
    hdr = (f"  {'AP SQL':<14} {'Config':<10} {'SB':>4} {'WM':>6} "
           f"{'TP-TPS':>8} {'Recov%':>7} {'TP-p95':>7} "
           f"{'AP-n':>5} {'AP-mean':>8} {'AP-p50':>8} {'AP-p95':>8} {'AP-p99':>8}")
    print(hdr)
    print(f"  {'-'*96}")
    for r in all_results:
        print(
            f"  {r['sql']:<14} {r['label']:<10} {r['sb']:>4} {r['wm']:>6} "
            f"{r['tp_tps']:>8.1f} {str(r['recovery_pct']):>7} {str(r['tp_p95_ms']):>7} "
            f"{r['ap_query_count']:>5} {str(r['ap_mean_ms']):>8} "
            f"{str(r['ap_p50_ms']):>8} {str(r['ap_p95_ms']):>8} {str(r['ap_p99_ms']):>8}"
        )

    # ── Restore and save ──────────────────────────────────────────────────────
    set_guc("shared_buffers", "4GB")
    set_guc("work_mem", "64MB")
    restart_db()

    out_path = f"{RESULTS_DIR}/v3_phase3_ap_latency.json"
    with open(out_path, "w") as f:
        json.dump({
            "baseline_2gb": BASELINE_2GB,
            "baseline_4gb": BASELINE_4GB,
            "wm_scan": wm_scan,
            "results": all_results,
            "ap_concurrency": AP_CONC,
            "tp_threads": TP_THREADS,
            "warmup_s": TP_WARMUP,
            "measure_s": TP_MEASURE,
        }, f, indent=2)
    print(f"\n  Saved: {out_path}")

    elapsed = (datetime.now() - t0).total_seconds() / 60
    print(f"\nAll done — {elapsed:.1f} min")
