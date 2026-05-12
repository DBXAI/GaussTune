#!/usr/bin/env python3
"""
GaussTune Experiment 3 — AP_CONC=8, SB default=4GB.

Configs per AP form:
  Default  : SB=4GB, WM=64MB
  WM-Tuned : SB=4GB, WM=M1
  SB-Tuned : SB=6GB, WM=64MB
  Joint    : SB=6GB, WM=M1

Measures per run:
  - TP: TPS, p95 latency
  - AP: mean/p50/p95/p99 latency (wall-clock per query, timed in bash)
  - AP spill: bytes_spilled via pg_stat_statements reset + EXPLAIN ANALYZE
"""
import subprocess, time, os, json, re, glob, statistics
from datetime import datetime

RESULTS_DIR  = "/home/node/GaussTune/refine-logs/results"
OMM_PASS     = "1997"
GSQL         = "/opt/openGauss/app/bin/gsql"
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
AP_CONC      = 8
BASELINE_4GB = 1203.6   # TP-only at SB=4GB from prior phase1

# ── Helpers ───────────────────────────────────────────────────────────────────
def omm_run(cmd, timeout=60):
    r = subprocess.run(
        ["su", "-", "omm", "-c", cmd],
        input=OMM_PASS + "\n", capture_output=True, text=True, timeout=timeout
    )
    return r.stdout, r.stderr

def gsql_sql(sql, db="sbtest"):
    tmp = "/tmp/gt_exp3.sql"
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

def measure_spill_kb(sql_name, wm_mb):
    """Run one EXPLAIN ANALYZE of the AP query and parse spill (sort space used)."""
    sql_oneline = re.sub(r"\s+", " ", AP_SQLS[sql_name].strip())
    explain_sql = f"SET work_mem='{wm_mb}MB'; EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql_oneline};"
    out, _ = gsql_sql(explain_sql, db="sbtest")
    # Extract "Sort Method: external merge  Disk: NNNNN kB"
    m = re.search(r"Disk:\s+(\d+)\s*kB", out)
    spill_kb = int(m.group(1)) if m else 0
    # Also catch "(Batches: N  Memory Usage: N kB)" style for hash
    if spill_kb == 0:
        m2 = re.search(r"Batches:\s*(\d+)", out)
        if m2 and int(m2.group(1)) > 1:
            mb = re.search(r"Memory Usage:\s*(\d+)\s*kB", out)
            spill_kb = -1   # spilled but couldn't parse exact size
    return spill_kb

def launch_ap_workers(sql_name, wm_mb, count):
    """Workers log per-query wall-clock ms to /tmp/ap_e3_<sql>_<pid>.log."""
    for f in glob.glob(f"/tmp/ap_e3_{sql_name}_*.pid") + glob.glob(f"/tmp/ap_e3_{sql_name}_*.log"):
        try: os.unlink(f)
        except: pass

    sql_oneline = re.sub(r"\s+", " ", AP_SQLS[sql_name].strip())
    full_cmd    = f"SET work_mem='{wm_mb}MB'; {sql_oneline}"
    script = (
        f"#!/bin/bash\n"
        f"LOGFILE=/tmp/ap_e3_{sql_name}_$$.log\n"
        f"echo $$ > /tmp/ap_e3_{sql_name}_$$.pid\n"
        f"while true; do\n"
        f"  T0=$(date +%s%3N)\n"
        f"  {GSQL} -d sbtest -c \"{full_cmd}\" >/dev/null 2>&1\n"
        f"  T1=$(date +%s%3N)\n"
        f"  echo $((T1 - T0)) >> $LOGFILE\n"
        f"done\n"
    )
    path = f"/tmp/ap_e3_{sql_name}.sh"
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
    time.sleep(10)   # longer startup wait for 8 workers
    return procs

def kill_workers(procs, sql_name):
    """Kill all workers, return latency samples (ms)."""
    cancel_sql = ("SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                  "WHERE state='active' AND query LIKE '%sbtest%' AND pid != pg_backend_pid();")
    try: gsql_sql(cancel_sql, db="postgres")
    except: pass

    pid_files = glob.glob(f"/tmp/ap_e3_{sql_name}_*.pid")
    pids = []
    for pf in pid_files:
        try:
            pid = open(pf).read().strip()
            if pid.isdigit(): pids.append(pid)
            os.unlink(pf)
        except: pass
    if pids:
        omm_run(f"kill -9 {' '.join(pids)} 2>/dev/null", timeout=10)

    for p in procs:
        p.terminate()
        try: p.wait(timeout=5)
        except: p.kill()

    time.sleep(10)

    samples = []
    for lf in glob.glob(f"/tmp/ap_e3_{sql_name}_*.log"):
        try:
            with open(lf) as f:
                for line in f:
                    v = line.strip()
                    if v.isdigit(): samples.append(int(v))
            os.unlink(lf)
        except: pass
    return samples

def ap_stats(samples):
    if not samples:
        return {"count": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    s = sorted(samples)
    n = len(s)
    def pct(p): return s[min(int(n * p / 100), n - 1)]
    return {
        "count":   n,
        "mean_ms": round(statistics.mean(s)),
        "p50_ms":  pct(50),
        "p95_ms":  pct(95),
        "p99_ms":  pct(99),
    }

def measure_mixed(sb, wm, sql_name, ref_tps, label, spill_kb):
    wm_mb = wm_to_mb(wm)
    print(f"    {label} (SB={sb}, WM={wm}) ...", end=" ", flush=True)
    set_guc("shared_buffers", sb)
    set_guc("work_mem", wm)
    ok = restart_db()
    if not ok:
        print("RESTART FAILED — retrying ...")
        time.sleep(30)
        ok = restart_db()
        if not ok:
            print("FAILED TWICE, skipping.")
            return None

    run_sysbench(TP_THREADS, TP_WARMUP)

    workers   = launch_ap_workers(sql_name, wm_mb, AP_CONC)
    sb_before = get_db_stats()
    tp_out    = run_sysbench(TP_THREADS, TP_MEASURE)
    sb_after  = get_db_stats()
    samples   = kill_workers(workers, sql_name)

    m        = parse_sysbench(tp_out)
    tps      = m.get("tps", 0)
    tp_p95   = m.get("p95_ms")
    hit      = cache_hit_pct(sb_before, sb_after)
    recovery = round(100.0 * tps / ref_tps, 1) if ref_tps and tps else None
    ap       = ap_stats(samples)

    print(
        f"TP: TPS={tps:.1f} ({recovery}%), p95={tp_p95}ms, cache={hit}% | "
        f"AP(n={ap['count']}): mean={ap['mean_ms']}ms p50={ap['p50_ms']}ms "
        f"p95={ap['p95_ms']}ms p99={ap['p99_ms']}ms | spill={spill_kb}kB"
    )
    return {
        "label": label, "sb": sb, "wm": wm, "sql": sql_name,
        "ap_concurrency": AP_CONC,
        "spill_kb": spill_kb,
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
    print(f"\nGaussTune Experiment 3 — {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"AP_CONC={AP_CONC} | TP_THREADS={TP_THREADS} | Warmup={TP_WARMUP}s | Measure={TP_MEASURE}s")
    print(f"Default SB=4GB | Tuned SB=6GB | System RAM=14GB")

    wm_scan_path = f"{RESULTS_DIR}/v3_wm_scan.json"
    with open(wm_scan_path) as f:
        wm_scan = json.load(f)
    print("M1 thresholds:", {k: f"{v['m1_mb']}MB" for k, v in wm_scan.items()})
    print(f"TP-only baseline (SB=4GB): {BASELINE_4GB} TPS")

    # ── Pre-measure spill per (sql, wm) combo ─────────────────────────────────
    print(f"\n{'='*70}")
    print("Pre-measuring spill amounts (isolated, no TP load)")
    print(f"{'='*70}")

    # Set neutral state for spill measurement
    set_guc("shared_buffers", "4GB")
    set_guc("work_mem", "64MB")
    restart_db()

    spill_map = {}   # (sql_name, wm_str) -> spill_kb
    for sql_name in AP_SQLS:
        m1_mb  = wm_scan[sql_name]["m1_mb"]
        m1_str = f"{m1_mb}MB"
        for wm_str, wm_mb in [("64MB", 64), (m1_str, m1_mb)]:
            kb = measure_spill_kb(sql_name, wm_mb)
            spill_map[(sql_name, wm_str)] = kb
            spill_label = f"{kb} kB" if kb >= 0 else "spilled (size N/A)"
            no_spill    = "NO SPILL" if kb == 0 else spill_label
            print(f"  {sql_name:14} WM={wm_str:>6}  spill={no_spill}")

    print(f"\n{'='*70}")
    print("Experiment 3: TP+AP mixed — TPS, TP latency, AP latency, spill")
    print(f"{'='*70}")

    all_results = []
    total = len(AP_SQLS) * 4
    idx   = 0

    for sql_name in AP_SQLS:
        m1_mb  = wm_scan[sql_name]["m1_mb"]
        m1_str = f"{m1_mb}MB"
        print(f"\n  ── AP form: {sql_name}  (M1={m1_mb}MB) ──")

        configs = [
            {"label": "Default",  "sb": "4GB", "wm": "64MB",  "ref": BASELINE_4GB},
            {"label": "WM-Tuned", "sb": "4GB", "wm": m1_str,  "ref": BASELINE_4GB},
            {"label": "SB-Tuned", "sb": "6GB", "wm": "64MB",  "ref": BASELINE_4GB},
            {"label": "Joint",    "sb": "6GB", "wm": m1_str,  "ref": BASELINE_4GB},
        ]
        for cfg in configs:
            idx += 1
            spill_kb = spill_map.get((sql_name, cfg["wm"]), -1)
            print(f"  [{idx}/{total}]", end=" ")
            result = measure_mixed(cfg["sb"], cfg["wm"], sql_name, cfg["ref"], cfg["label"], spill_kb)
            if result:
                all_results.append(result)

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"\n{'='*110}")
    print("EXPERIMENT 3 SUMMARY")
    print(f"{'='*110}")
    hdr = (
        f"  {'AP SQL':<14} {'Config':<10} {'SB':>4} {'WM':>6} {'Spill':>8} "
        f"{'TP-TPS':>8} {'Recov%':>7} {'TP-p95':>7} "
        f"{'AP-n':>5} {'AP-mean':>8} {'AP-p50':>7} {'AP-p95':>7} {'AP-p99':>7}"
    )
    print(hdr)
    print(f"  {'-'*106}")
    for r in all_results:
        spill_s = f"{r['spill_kb']}kB" if r['spill_kb'] >= 0 else "?"
        print(
            f"  {r['sql']:<14} {r['label']:<10} {r['sb']:>4} {r['wm']:>6} {spill_s:>8} "
            f"{r['tp_tps']:>8.1f} {str(r['recovery_pct']):>7} {str(r['tp_p95_ms']):>7} "
            f"{r['ap_query_count']:>5} {str(r['ap_mean_ms']):>8} "
            f"{str(r['ap_p50_ms']):>7} {str(r['ap_p95_ms']):>7} {str(r['ap_p99_ms']):>7}"
        )

    # ── Restore and save ──────────────────────────────────────────────────────
    set_guc("shared_buffers", "4GB")
    set_guc("work_mem", "64MB")
    restart_db()

    out_path = f"{RESULTS_DIR}/exp3_results.json"
    with open(out_path, "w") as f:
        json.dump({
            "baseline_4gb": BASELINE_4GB,
            "wm_scan": wm_scan,
            "ap_concurrency": AP_CONC,
            "tp_threads": TP_THREADS,
            "warmup_s": TP_WARMUP,
            "measure_s": TP_MEASURE,
            "spill_map": {f"{k[0]}|{k[1]}": v for k, v in spill_map.items()},
            "results": all_results,
        }, f, indent=2)
    print(f"\n  Saved: {out_path}")

    elapsed = (datetime.now() - t0).total_seconds() / 60
    print(f"\nAll done — {elapsed:.1f} min")
