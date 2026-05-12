#!/usr/bin/env python3
"""
Window_rank verification experiment.
For each config: measure TP-only baseline first, then mixed TP+AP load.
Also samples CPU usage during measurement.
AP_CONC=8, same as Experiment 3.
"""
import subprocess, time, os, json, re, glob, statistics, threading
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
AP_SQL = (
    "SELECT id, k, "
    "RANK() OVER (ORDER BY c DESC, pad ASC, k DESC) AS rk "
    "FROM sbtest1 "
    "ORDER BY rk "
    "LIMIT 100000"
)

TP_THREADS  = 16
TP_WARMUP   = 60
TP_MEASURE  = 120
AP_CONC     = 8

CONFIGS = [
    {"label": "Default",  "sb": "4GB", "wm": "64MB",  "spill_kb": 395368},
    {"label": "WM-Tuned", "sb": "4GB", "wm": "768MB", "spill_kb": 0},
    {"label": "SB-Tuned", "sb": "6GB", "wm": "64MB",  "spill_kb": 395368},
    {"label": "Joint",    "sb": "6GB", "wm": "768MB",  "spill_kb": 0},
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def omm_run(cmd, timeout=60):
    r = subprocess.run(
        ["su", "-", "omm", "-c", cmd],
        input=OMM_PASS + "\n", capture_output=True, text=True, timeout=timeout
    )
    return r.stdout, r.stderr

def gsql_sql(sql, db="sbtest"):
    tmp = "/tmp/gt_wr.sql"
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

def sample_cpu(duration_s, interval=2):
    """Sample total CPU usage (%) every interval seconds, return list."""
    samples = []
    end = time.time() + duration_s
    while time.time() < end:
        try:
            r = subprocess.run(
                ["top", "-bn1"],
                capture_output=True, text=True, timeout=5
            )
            # top output: "%Cpu(s):  X.X us, ..."
            m = re.search(r"%Cpu.*?(\d+\.\d+)\s+id", r.stdout)
            if m:
                idle = float(m.group(1))
                samples.append(round(100.0 - idle, 1))
        except:
            pass
        time.sleep(interval)
    return samples

def cpu_stats(samples):
    if not samples:
        return {"mean": None, "max": None}
    return {"mean": round(statistics.mean(samples), 1), "max": max(samples)}

def launch_ap_workers(wm_mb):
    for f in glob.glob("/tmp/ap_wr_*.pid") + glob.glob("/tmp/ap_wr_*.log"):
        try: os.unlink(f)
        except: pass

    sql_oneline = re.sub(r"\s+", " ", AP_SQL.strip())
    full_cmd = f"SET work_mem='{wm_mb}MB'; {sql_oneline}"
    script = (
        f"#!/bin/bash\n"
        f"LOGFILE=/tmp/ap_wr_$$.log\n"
        f"echo $$ > /tmp/ap_wr_$$.pid\n"
        f"while true; do\n"
        f"  T0=$(date +%s%3N)\n"
        f"  {GSQL} -d sbtest -c \"{full_cmd}\" >/dev/null 2>&1\n"
        f"  T1=$(date +%s%3N)\n"
        f"  echo $((T1 - T0)) >> $LOGFILE\n"
        f"done\n"
    )
    path = "/tmp/ap_wr.sh"
    with open(path, "w") as f: f.write(script)
    os.chmod(path, 0o755)

    procs = []
    for _ in range(AP_CONC):
        p = subprocess.Popen(
            ["su", "-", "omm", "-c", path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        p.stdin.write((OMM_PASS + "\n").encode())
        p.stdin.flush()
        procs.append(p)
    time.sleep(10)
    return procs

def kill_workers(procs):
    cancel_sql = ("SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                  "WHERE state='active' AND query LIKE '%sbtest%' AND pid != pg_backend_pid();")
    try: gsql_sql(cancel_sql, db="postgres")
    except: pass

    pids = []
    for pf in glob.glob("/tmp/ap_wr_*.pid"):
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
    for lf in glob.glob("/tmp/ap_wr_*.log"):
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
        return {"count": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None}
    s = sorted(samples)
    n = len(s)
    def pct(p): return s[min(int(n * p / 100), n - 1)]
    return {"count": n, "mean_ms": round(statistics.mean(s)),
            "p50_ms": pct(50), "p95_ms": pct(95)}


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    t0 = datetime.now()
    print(f"\nWindow_rank Verification — {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"AP_CONC={AP_CONC} | TP_THREADS={TP_THREADS} | Warmup={TP_WARMUP}s | Measure={TP_MEASURE}s")
    print(f"{'='*80}")

    all_results = []

    for idx, cfg in enumerate(CONFIGS, 1):
        sb, wm = cfg["sb"], cfg["wm"]
        wm_mb = wm_to_mb(wm)
        label = cfg["label"]

        print(f"\n[{idx}/4] {label}  (SB={sb}, WM={wm}, spill={cfg['spill_kb']}kB)")
        print(f"  Setting GUCs and restarting DB ...")
        set_guc("shared_buffers", sb)
        set_guc("work_mem", wm)
        ok = restart_db()
        if not ok:
            print("  RESTART FAILED — retrying ...")
            time.sleep(30)
            ok = restart_db()
            if not ok:
                print("  FAILED TWICE, skipping.")
                continue

        # ── Step 1: TP-only baseline for this config ───────────────────────
        print(f"  Step 1: TP-only warmup {TP_WARMUP}s + measure {TP_MEASURE}s ...", end=" ", flush=True)
        run_sysbench(TP_THREADS, TP_WARMUP)
        tp_only_out = run_sysbench(TP_THREADS, TP_MEASURE)
        tp_only = parse_sysbench(tp_only_out)
        tp_only_tps = tp_only.get("tps", 0)
        tp_only_p95 = tp_only.get("p95_ms")
        print(f"TPS={tp_only_tps:.1f}, p95={tp_only_p95}ms")

        # ── Step 2: Mixed TP+AP ────────────────────────────────────────────
        print(f"  Step 2: Mixed warmup {TP_WARMUP}s + measure {TP_MEASURE}s ...", end=" ", flush=True)
        run_sysbench(TP_THREADS, TP_WARMUP)

        workers = launch_ap_workers(wm_mb)
        sb_before = get_db_stats()

        # CPU sampling thread
        cpu_samples = []
        def _cpu_sample():
            cpu_samples.extend(sample_cpu(TP_MEASURE, interval=3))
        cpu_thread = threading.Thread(target=_cpu_sample, daemon=True)
        cpu_thread.start()

        tp_out    = run_sysbench(TP_THREADS, TP_MEASURE)
        sb_after  = get_db_stats()
        ap_samps  = kill_workers(workers)
        cpu_thread.join(timeout=10)

        m        = parse_sysbench(tp_out)
        tps      = m.get("tps", 0)
        tp_p95   = m.get("p95_ms")
        hit      = cache_hit_pct(sb_before, sb_after)
        miss     = round(100.0 - hit, 1)
        recovery = round(100.0 * tps / tp_only_tps, 1) if tp_only_tps and tps else None
        ap       = ap_stats(ap_samps)
        cpu      = cpu_stats(cpu_samples)

        print(
            f"TPS={tps:.1f} (vs TP-only {tp_only_tps:.1f}, recovery={recovery}%), "
            f"p95={tp_p95}ms\n"
            f"          cache_miss={miss}% | "
            f"CPU mean={cpu['mean']}% max={cpu['max']}% | "
            f"AP n={ap['count']} mean={ap['mean_ms']}ms p50={ap['p50_ms']}ms p95={ap['p95_ms']}ms"
        )

        all_results.append({
            "label": label, "sb": sb, "wm": wm,
            "spill_kb": cfg["spill_kb"],
            "tp_only_tps": tp_only_tps, "tp_only_p95_ms": tp_only_p95,
            "tp_mixed_tps": tps, "tp_mixed_p95_ms": tp_p95,
            "recovery_pct": recovery,
            "cache_hit_pct": hit, "cache_miss_pct": miss,
            "cpu_mean_pct": cpu["mean"], "cpu_max_pct": cpu["max"],
            "ap_count": ap["count"], "ap_mean_ms": ap["mean_ms"],
            "ap_p50_ms": ap["p50_ms"], "ap_p95_ms": ap["p95_ms"],
        })

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print("WINDOW_RANK VERIFICATION SUMMARY")
    print(f"{'='*100}")
    print(f"  {'Config':<10} {'SB':>4} {'WM':>6} {'Spill':>8} "
          f"{'TP-only':>8} {'TP-mixed':>9} {'Recov%':>7} {'Miss%':>6} "
          f"{'CPU-mean':>9} {'CPU-max':>8} "
          f"{'AP-n':>5} {'AP-mean':>8} {'AP-p50':>7} {'AP-p95':>7}")
    print(f"  {'-'*98}")
    for r in all_results:
        spill_s = f"{r['spill_kb']}kB"
        print(
            f"  {r['label']:<10} {r['sb']:>4} {r['wm']:>6} {spill_s:>8} "
            f"{r['tp_only_tps']:>8.1f} {r['tp_mixed_tps']:>9.1f} {str(r['recovery_pct']):>7} "
            f"{r['cache_miss_pct']:>6.1f} "
            f"{str(r['cpu_mean_pct']):>9} {str(r['cpu_max_pct']):>8} "
            f"{r['ap_count']:>5} {str(r['ap_mean_ms']):>8} "
            f"{str(r['ap_p50_ms']):>7} {str(r['ap_p95_ms']):>7}"
        )

    # ── Restore and save ──────────────────────────────────────────────────────
    set_guc("shared_buffers", "4GB")
    set_guc("work_mem", "64MB")
    restart_db()

    out_path = f"{RESULTS_DIR}/windowrank_verify.json"
    with open(out_path, "w") as f:
        json.dump({
            "ap_concurrency": AP_CONC,
            "tp_threads": TP_THREADS,
            "warmup_s": TP_WARMUP,
            "measure_s": TP_MEASURE,
            "results": all_results,
        }, f, indent=2)
    print(f"\n  Saved: {out_path}")
    elapsed = (datetime.now() - t0).total_seconds() / 60
    print(f"\nAll done — {elapsed:.1f} min")
