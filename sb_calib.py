#!/usr/bin/env python3
"""
SB Penalty Calibration
======================
Measures TP TPS at each shared_buffers level to characterize the penalty
function tps_penalty(SB) = tps_0 - tps(SB).

Protocol per SB level:
  1. ALTER SYSTEM + DB restart (compact memory first for THP)
  2. Warmup: sysbench WARMUP_S seconds (fills buffer pool)
  3. Measure: sysbench MEASURE_S seconds (stable TPS)
  4. Record: (SB, tps, blks_hit_rate, mem_avail_mb)

After all levels, restores SB to BASE_SB_MB and fits a simple penalty model.
"""

import subprocess, time, re, os, json, math, threading
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
GSQL         = "/opt/openGauss/app/bin/gsql"
OMM_PASS     = "1997"
LOG_PATH     = "/home/node/GaussTune/run-logs/sb_calib4.log"
JSON_OUT     = "/home/node/GaussTune/run-logs/sb_calib4.json"

BASE_SB_MB   = 1024   # restore to this at end
SB_LEVELS    = [1024, 2048, 3072, 4096, 5120, 6144, 7168]

WARMUP_S     = 180    # sysbench warmup after each SB change
MEASURE_S    = 60     # TPS measurement window

PERF_EVENTS  = "dTLB-load-misses,longest_lat_cache.miss,longest_lat_cache.reference,cycles"
OS_RESERVE   = 2048

SB_CMD = (
    "LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu "
    "sysbench oltp_read_write "
    "--db-driver=pgsql --pgsql-host=/tmp --pgsql-port=5432 "
    "--pgsql-user=omm --pgsql-password= --pgsql-db=sbtest "
    "--tables=10 --table-size=2000000 "
    "--db-ps-mode=disable --threads=16 --rand-type=uniform "
    "--report-interval=5 --time={duration} run"
)

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
_log_file = open(LOG_PATH, "w", buffering=1)

def log(msg=""):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    _log_file.write(line + "\n")
    _log_file.flush()

# ── DB helpers ────────────────────────────────────────────────────────────────
def omm_run(cmd, timeout=60):
    r = subprocess.run(["su", "-", "omm", "-c", cmd],
        input=OMM_PASS + "\n", capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr

def gsql_q(sql, db="postgres", timeout=30):
    tmp = "/tmp/sbcalib_q.sql"
    with open(tmp, "w") as f:
        f.write(sql)
    os.chmod(tmp, 0o666)
    return omm_run(f"{GSQL} -d {db} -f {tmp}", timeout=timeout)

def parse_row(out):
    for i, line in enumerate(out.split("\n")):
        if re.match(r"\s*-+", line):
            for dl in out.split("\n")[i + 1:]:
                dl = dl.strip()
                if dl and not dl.startswith("("):
                    return [x.strip() for x in dl.split("|")]
    return []

def set_guc(p, v):
    gsql_q(f"ALTER SYSTEM SET {p}='{v}'; SELECT pg_reload_conf();")

def compact_memory():
    try:
        subprocess.run(["sudo", "tee", "/proc/sys/vm/compact_memory"],
                       input="1\n", capture_output=True, text=True, timeout=10)
    except Exception as e:
        log(f"  [warn] compact: {e}")
    time.sleep(2)

def restart_db():
    try:
        gsql_q("CHECKPOINT;", timeout=60)
        time.sleep(2)
    except Exception:
        pass
    omm_run(
        "export GAUSSHOME=/opt/openGauss/app; export PATH=$GAUSSHOME/bin:$PATH; "
        "export LD_LIBRARY_PATH=$GAUSSHOME/lib; "
        "gs_ctl restart -D /opt/openGauss/data", timeout=300)
    for _ in range(90):
        if os.path.exists("/tmp/.s.PGSQL.5432"):
            break
        time.sleep(2)
    else:
        return False
    time.sleep(20)
    successes = 0
    for _ in range(60):
        out, _ = omm_run(f"{GSQL} -d postgres -c 'SELECT 1;'", timeout=10)
        if "1 row" in out or "(1 row)" in out:
            successes += 1
            if successes >= 3:
                return True
        else:
            successes = 0
        time.sleep(3)
    return False

def get_db_stats():
    """Returns (blks_hit, blks_read) cumulative counters."""
    tmp = "/tmp/sbcalib_stats.sql"
    with open(tmp, "w") as f:
        f.write("SELECT blks_hit, blks_read FROM pg_stat_database WHERE datname='sbtest';")
    os.chmod(tmp, 0o666)
    out, _ = omm_run(f"{GSQL} -d postgres -f {tmp}", timeout=15)
    row = parse_row(out)
    if len(row) >= 2:
        return int(row[0]), int(row[1])
    return 0, 0

def read_meminfo():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0

def parse_sysbench_tps(output: str) -> float:
    """Extract average TPS from sysbench output (transactions/sec from summary line)."""
    for line in reversed(output.split("\n")):
        m = re.search(r"transactions:\s+\d+\s+\((\d+\.\d+)\s+per sec", line)
        if m:
            return float(m.group(1))
    # fallback: average of report-interval lines
    tps_samples = []
    for line in output.split("\n"):
        m = re.search(r"\[\s*\d+s\s*\].*tps:\s*([\d.]+)", line)
        if m:
            tps_samples.append(float(m.group(1)))
    return sum(tps_samples) / len(tps_samples) if tps_samples else 0.0

SUDO_PASS    = "1997"

def run_perf(duration_s: int, out_file: str, pid: int):
    """Run perf stat attached to gaussdb pid for duration_s seconds."""
    cmd = (f"echo '{SUDO_PASS}' | sudo -S perf stat "
           f"-e {PERF_EVENTS} -x, -p {pid} sleep {duration_s}")
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=duration_s + 15)
        with open(out_file, "w") as f:
            f.write(r.stdout + r.stderr)
    except Exception as e:
        log(f"  [warn] perf: {e}")


def parse_perf(out_file: str) -> dict:
    """Parse perf stat CSV output (-x,). Returns dict of event→count."""
    counts = {}
    try:
        with open(out_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                val_str = parts[0].strip()
                event   = parts[2].strip() if len(parts) > 2 else parts[1].strip()
                try:
                    counts[event] = int(val_str.replace(",", ""))
                except ValueError:
                    pass
    except Exception:
        pass
    return counts
def measure_at_sb(sb_mb: int) -> dict:
    log(f"\n{'='*60}")
    log(f"  SB = {sb_mb} MB")
    log(f"{'='*60}")

    compact_memory()          # compact only, no drop_caches
    set_guc("shared_buffers", f"{sb_mb}MB")
    ok = restart_db()
    if not ok:
        log(f"  ERROR: DB restart failed at SB={sb_mb}MB — skipping")
        return {"sb_mb": sb_mb, "tps": None, "error": "restart_failed"}

    mem_before = read_meminfo()
    log(f"  MemAvailable after restart: {mem_before}MB")

    # Warmup: fill buffer pool
    log(f"  Warmup {WARMUP_S}s ...")
    gsql_q("SELECT pg_stat_reset();")
    omm_run(SB_CMD.format(duration=WARMUP_S), timeout=WARMUP_S + 30)

    # Measurement: sysbench + perf in parallel
    log(f"  Measuring {MEASURE_S}s (+ perf stat) ...")
    perf_out = f"/tmp/sbcalib_perf_{sb_mb}.txt"
    gaussdb_pid = next(iter(
        int(p) for p in subprocess.check_output(["pgrep", "-x", "gaussdb"]).split()
    ), 0)
    hit0, read0 = get_db_stats()
    perf_thread = threading.Thread(
        target=run_perf, args=(MEASURE_S, perf_out, gaussdb_pid), daemon=True)
    perf_thread.start()
    out, _ = omm_run(SB_CMD.format(duration=MEASURE_S), timeout=MEASURE_S + 30)
    perf_thread.join(timeout=MEASURE_S + 15)
    hit1, read1 = get_db_stats()

    tps = parse_sysbench_tps(out)
    dh  = hit1  - hit0
    dr  = read1 - read0
    total = dh + dr
    hit_rate = dh / total if total > 0 else 1.0
    mem_after = read_meminfo()

    perf = parse_perf(perf_out)
    dtlb  = perf.get("dTLB-load-misses", 0)
    l3m   = perf.get("longest_lat_cache.miss", 0)
    l3r   = perf.get("longest_lat_cache.reference", 0)
    cyc   = perf.get("cycles", 0)
    txns  = max(1, tps * MEASURE_S)
    l3_pct = round(l3m / l3r * 100, 2) if l3r > 0 else 0.0

    log(f"  TPS             = {tps:.1f}")
    log(f"  blks_hit        = {dh:,}  blks_read = {dr:,}  hit_rate = {hit_rate*100:.2f}%")
    log(f"  MemAvail        = {mem_after}MB")
    log(f"  dTLB-miss/txn   = {dtlb/txns:.0f}  ({dtlb:,} total)")
    log(f"  L3-miss/txn     = {l3m/txns:.0f}  L3-miss% = {l3_pct}%")
    log(f"  cycles/txn      = {cyc/txns:.0f}")

    return {
        "sb_mb":           sb_mb,
        "tps":             round(tps, 2),
        "blks_hit":        dh,
        "blks_read":       dr,
        "hit_rate":        round(hit_rate, 5),
        "mem_avail":       mem_after,
        "dtlb_miss":       dtlb,
        "dtlb_miss_per_txn": round(dtlb / txns, 1),
        "l3_miss":         l3m,
        "l3_miss_per_txn": round(l3m / txns, 1),
        "l3_miss_pct":     l3_pct,
        "cycles":          cyc,
        "cycles_per_txn":  round(cyc / txns, 0),
    }

# ── Model fitting ─────────────────────────────────────────────────────────────
def fit_penalty(results: list[dict]) -> dict:
    """
    Fit a piecewise-linear penalty model:
      tps(SB) = tps_base                           SB <= SB_safe
              = tps_base - slope * (SB - SB_safe)  SB >  SB_safe

    SB_safe = largest SB where tps > tps_base * 0.95 (5% tolerance).
    slope   = (tps_safe - tps_large) / (SB_large - SB_safe)  [TPS per MB]

    Also fits an exponential decay for smoother integration into _mimo_simulate:
      tps(SB) = tps_base * exp(-lambda * max(0, SB - SB_safe) / total_ram)
    """
    valid = [r for r in results if r.get("tps") is not None]
    if len(valid) < 2:
        return {}

    sb_vals  = [r["sb_mb"]  for r in valid]
    tps_vals = [r["tps"]    for r in valid]

    tps_base = tps_vals[0]  # TPS at baseline SB (smallest tested)

    # Find SB_safe: last level where tps > tps_base * 0.95
    sb_safe = sb_vals[0]
    for r in valid:
        if r["tps"] >= tps_base * 0.95:
            sb_safe = r["sb_mb"]
        else:
            break  # first level that drops below threshold

    # Linear slope from sb_safe to max tested
    safe_tps  = next((r["tps"] for r in valid if r["sb_mb"] == sb_safe), tps_base)
    last      = valid[-1]
    if last["sb_mb"] > sb_safe and last["tps"] is not None:
        slope_tps_per_mb = (safe_tps - last["tps"]) / (last["sb_mb"] - sb_safe)
    else:
        slope_tps_per_mb = 0.0

    # Exponential lambda
    # tps(SB_max) = tps_base * exp(-λ * (SB_max - SB_safe) / total_ram)
    # → λ = -ln(tps_max/tps_base) * total_ram / (SB_max - SB_safe)
    total_ram = 14700  # MB
    if last["sb_mb"] > sb_safe and last["tps"] is not None and last["tps"] > 0:
        ratio = last["tps"] / tps_base
        if ratio < 1.0 and ratio > 0:
            lam = -math.log(ratio) * total_ram / (last["sb_mb"] - sb_safe)
        else:
            lam = 0.0
    else:
        lam = 0.0

    log("\n── Penalty model ────────────────────────────────────────")
    log(f"  tps_base          = {tps_base:.1f} TPS  (at SB={sb_vals[0]}MB)")
    log(f"  SB_safe           = {sb_safe}MB  (≤5% TPS degradation)")
    log(f"  slope (linear)    = {slope_tps_per_mb*1024:.4f} TPS/GB")
    log(f"  lambda (exp)      = {lam:.6f}  "
        f"[tps(SB) = {tps_base:.0f} × exp(-{lam:.4f} × max(0, SB-{sb_safe}) / {total_ram})]")
    log(f"  → integrate into _mimo_simulate: penalise SB > {sb_safe}MB")
    log("─────────────────────────────────────────────────────────\n")

    return {
        "tps_base":          round(tps_base, 2),
        "sb_safe_mb":        sb_safe,
        "slope_tps_per_mb":  round(slope_tps_per_mb, 6),
        "slope_tps_per_gb":  round(slope_tps_per_mb * 1024, 4),
        "lambda_exp":        round(lam, 6),
        "total_ram_mb":      total_ram,
    }

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log("SB Penalty Calibration")
    log(f"  SB levels : {SB_LEVELS} MB")
    log(f"  Warmup    : {WARMUP_S}s per level")
    log(f"  Measure   : {MEASURE_S}s per level")
    log(f"  Total est : {len(SB_LEVELS) * (WARMUP_S + MEASURE_S + 90) // 60} min\n")

    results = []
    for sb in SB_LEVELS:
        r = measure_at_sb(sb)
        results.append(r)
        log(f"  Recorded: SB={sb}MB → TPS={r.get('tps', 'N/A')}")

    # Restore baseline SB
    log(f"\nRestoring SB to {BASE_SB_MB}MB ...")
    set_guc("shared_buffers", f"{BASE_SB_MB}MB")
    restart_db()
    log("Done.\n")

    model = fit_penalty(results)

    # Print summary table
    log("── Results ──────────────────────────────────────────────")
    log(f"  {'SB(MB)':>8}  {'TPS':>6}  {'hit%':>6}  {'blks_read':>10}  "
        f"{'dTLB/txn':>10}  {'L3miss/txn':>11}  {'cyc/txn':>10}  {'MemAvail':>9}")
    log(f"  {'-'*8}  {'-'*6}  {'-'*6}  {'-'*10}  {'-'*10}  {'-'*11}  {'-'*10}  {'-'*9}")
    for r in results:
        if r.get("tps") is not None:
            log(f"  {r['sb_mb']:>8}  {r['tps']:>6.1f}  {r['hit_rate']*100:>6.1f}  "
                f"{r['blks_read']:>10,}  {r['dtlb_miss_per_txn']:>10.0f}  "
                f"{r['l3_miss_per_txn']:>11.0f}  {r['cycles_per_txn']:>10.0f}  "
                f"{r['mem_avail']:>9}")
        else:
            log(f"  {r['sb_mb']:>8}  {'ERROR':>6}")
    log("─────────────────────────────────────────────────────────")

    output = {
        "date":    datetime.now().isoformat(),
        "warmup_s":  WARMUP_S,
        "measure_s": MEASURE_S,
        "levels":  results,
        "model":   model,
    }
    with open(JSON_OUT, "w") as f:
        json.dump(output, f, indent=2)
    log(f"\nSaved: {JSON_OUT}")

if __name__ == "__main__":
    main()
