#!/usr/bin/env python3
"""
db_helpers.py — DB helper library for GaussTune experiment harness
===================================================================

Provides all OpenGauss / sysbench / OS helpers used by bench_methods.py:
  - gsql / omm_run wrappers
  - DB restart, GUC setting, shared_buffers change
  - sysbench launch, AP injection, stat collection
  - iowait%, w_await, meminfo, calibration helpers

This file is a pure library — no main(), no experiment orchestration.
All experiment logic lives in bench_methods.py.
"""

import subprocess, time, re, os, json, threading
from datetime import datetime
from stmm_controller import STMMController, BRBEController, ProactiveBRBEController, PAGE_SIZE_KB
from workloads import WORKLOADS

# ── Config ────────────────────────────────────────────────────────────────────
GSQL        = "/opt/openGauss/app/bin/gsql"
OMM_PASS    = "1997"
LOG_PATH    = "/home/node/GaussTune/run-logs/db_helpers.log"
RESULTS_DIR = "/home/node/GaussTune/run-logs"

SB_CMD = (
    "LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu "
    "sysbench oltp_read_write "
    "--db-driver=pgsql --pgsql-host=/tmp --pgsql-port=5432 "
    "--pgsql-user=omm --pgsql-password= --pgsql-db=sbtest "
    "--tables=10 --table-size=2000000 "
    "--db-ps-mode=disable --threads=16 --rand-type=uniform "
    "--report-interval=5 --time={duration} run"
)

AP_SQL = "SELECT k, c, pad FROM sbtest1 ORDER BY c DESC, pad ASC, k DESC"

AP_CONC   = 4
AP_DUR    = 360
PRE_AP_S  = 60
POST_AP_S = 180

WM_INIT       = 64
SB_MB         = 1024
RAM_MB        = 14700
OS_RESERVE_MB = 2048

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
    tmp = "/tmp/stmm_q.sql"
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


def set_guc(p, v, timeout=15):
    if p == "work_mem":
        gsql_q(f"ALTER DATABASE sbtest SET {p}='{v}';", timeout=timeout)
    else:
        gsql_q(f"ALTER SYSTEM SET {p}='{v}'; SELECT pg_reload_conf();", timeout=timeout)


def compact_memory(drop_caches: bool = False):
    """Trigger kernel memory compaction; optionally drop OS page cache first."""
    steps = []
    if drop_caches:
        steps.append(("/proc/sys/vm/drop_caches", "3"))
    steps.append(("/proc/sys/vm/compact_memory", "1"))
    for path, val in steps:
        try:
            r = subprocess.run(["sudo", "tee", path], input=val + "\n",
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                log(f"  [warn] compact_memory: tee {path} failed: {r.stderr.strip()}")
        except Exception as e:
            log(f"  [warn] compact_memory: {e}")
    time.sleep(2)


def restart_db():
    try:
        gsql_q("CHECKPOINT;", timeout=60)
        time.sleep(2)
    except Exception:
        pass
    omm_run("export GAUSSHOME=/opt/openGauss/app; export PATH=$GAUSSHOME/bin:$PATH; "
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


def ensure_db_ready(label=""):
    """Verify DB is fully ready; retry restart once if not."""
    out, _ = omm_run(f"{GSQL} -d postgres -c 'SELECT 1;'", timeout=10)
    if "1 row" in out or "(1 row)" in out:
        return
    log(f"  DB not responding{' (' + label + ')' if label else ''} — retrying restart...")
    ok = restart_db()
    if not ok:
        raise RuntimeError("DB failed to come up after retry — aborting")


def apply_sb_change(new_sb_mb, stmm_controller=None, warmup_s=60, current_wm_mb=None):
    """Apply shared_buffers change via ALTER SYSTEM + DB restart + re-warmup."""
    wm_was_lowered = False
    if current_wm_mb and current_wm_mb > WM_INIT:
        peak_mb = new_sb_mb + SB_MB + AP_CONC * current_wm_mb + OS_RESERVE_MB
        if peak_mb > RAM_MB:
            log(f"  → OOM guard: peak={peak_mb}MB > RAM={RAM_MB}MB — dropping WM to {WM_INIT}MB during restart")
            set_guc("work_mem", f"{WM_INIT}MB")
            wm_was_lowered = True

    log(f"  → SB change: {new_sb_mb}MB — compacting memory + restarting DB...")
    compact_memory(drop_caches=True)
    set_guc("shared_buffers", f"{new_sb_mb}MB")
    ok = restart_db()
    if not ok:
        log("  WARNING: restart_db() failed during SB change — continuing with old SB")
        if wm_was_lowered:
            set_guc("work_mem", f"{current_wm_mb}MB")
        return False
    if stmm_controller is not None:
        stmm_controller.sb_mb = float(new_sb_mb)
    log(f"  → Warming up {warmup_s}s after SB change...")
    omm_run(SB_CMD.format(duration=warmup_s), timeout=warmup_s + 30)
    if wm_was_lowered:
        log(f"  → OOM guard: restoring WM to {current_wm_mb}MB")
        set_guc("work_mem", f"{current_wm_mb}MB")
    try:
        gsql_q("CHECKPOINT;", timeout=60)
    except Exception:
        pass
    log(f"  → SB now {new_sb_mb}MB, warmup done.")
    return True


def get_db_stats():
    tmp = "/tmp/stmm_stats.sql"
    with open(tmp, "w") as f:
        f.write("SELECT blks_hit, blks_read, temp_bytes "
                "FROM pg_stat_database WHERE datname='sbtest';")
    os.chmod(tmp, 0o666)
    out, _ = omm_run(f"{GSQL} -d postgres -f {tmp}", timeout=15)
    row = parse_row(out)
    if len(row) >= 3:
        return int(row[0]), int(row[1]), int(row[2])
    return 0, 0, 0


def measure_tps(duration_s: int) -> float:
    """Run sysbench for duration_s seconds and return average TPS."""
    out, _ = omm_run(SB_CMD.format(duration=duration_s), timeout=duration_s + 15)
    vals = []
    for line in out.splitlines():
        m = re.search(r"thds:\s*\d+\s+tps:\s*([\d.]+)", line)
        if m:
            vals.append(float(m.group(1)))
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def get_gaussdb_pid() -> int | None:
    """Return PID of the running gaussdb process owned by omm, or None."""
    out, _ = omm_run("pgrep -u omm gaussdb 2>/dev/null | head -1", timeout=10)
    pid = out.strip().split()[0] if out.strip() else ""
    return int(pid) if pid.isdigit() else None


def _read_w_await() -> float:
    """Read current w_await (ms) from iostat for the data device. Returns 0 on failure."""
    try:
        dev = subprocess.check_output(["df", "/opt/openGauss/data"],
                                      text=True, timeout=5).split("\n")[1].split()[0]
        dev = dev.rsplit("/", 1)[-1]
        m = re.match(r"(nvme\d+n\d+|[a-z]+)", dev)
        dev = m.group(1) if m else "sda"
        r = subprocess.run(f"iostat -xk {dev} 1 1", shell=True,
                           capture_output=True, text=True, timeout=8)
        col_map: dict = {}
        for line in r.stdout.splitlines():
            if line.startswith("Device"):
                col_map = {c: i for i, c in enumerate(line.split())}
                continue
            if col_map and line.strip() and not line.startswith(("Linux", "avg")):
                parts = line.split()
                idx = col_map.get("w_await", col_map.get("await", None))
                if idx is not None and idx < len(parts):
                    return float(parts[idx])
    except Exception:
        pass
    return 0.0


def _read_iowait_pct() -> float:
    """Read instantaneous iowait% from /proc/stat over a 1-second window. Returns 0.0 on failure."""
    def _read():
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.split()
                    keys = ["user", "nice", "system", "idle", "iowait",
                            "irq", "softirq", "steal"]
                    return {k: int(parts[i + 1]) for i, k in enumerate(keys)
                            if i + 1 < len(parts)}
        return {}
    try:
        j0 = _read()
        time.sleep(1)
        j1 = _read()
        delta = {k: max(0, j1.get(k, 0) - j0.get(k, 0)) for k in j0}
        total = sum(delta.values())
        return 100.0 * delta.get("iowait", 0) / total if total > 0 else 0.0
    except Exception:
        return 0.0


def _parse_perf_output(path: str) -> dict:
    result = {}
    try:
        content = open(path).read()
        patterns = {
            "dtlb_miss": r"([\d,]+)\s+dTLB-load-misses",
            "l3_miss":   r"([\d,]+)\s+longest_lat_cache\.miss",
            "l3_ref":    r"([\d,]+)\s+longest_lat_cache\.reference",
            "cycles":    r"([\d,]+)\s+cycles",
        }
        for key, pat in patterns.items():
            m = re.search(pat, content)
            if m:
                result[key] = int(m.group(1).replace(",", ""))
        if result.get("l3_miss") and result.get("l3_ref") and result["l3_ref"] > 0:
            result["l3_miss_pct"] = round(100 * result["l3_miss"] / result["l3_ref"], 1)
    except Exception:
        pass
    return result


def _run_perf_phase(gdb_pid: int, out_path: str, duration_s: int, delay_s: int = 0):
    """Run perf stat against gdb_pid for duration_s seconds (after optional delay)."""
    PERF_EVENTS = "dTLB-load-misses,longest_lat_cache.miss,longest_lat_cache.reference,cycles"
    if delay_s > 0:
        time.sleep(delay_s)
    try:
        omm_run(f"perf stat -e {PERF_EVENTS} -p {gdb_pid} -o {out_path} -- sleep {duration_s}",
                timeout=duration_s + 30)
    except Exception as e:
        log(f"  [perf] collection error: {e}")


def calibrate_io_costs() -> tuple[float, float]:
    """Measure actual disk read and write throughput via dd O_DIRECT."""
    DEFAULT = (0.01, 0.01)
    READ_MB  = 256
    WRITE_MB = 256
    WRITE_FILE = "/opt/openGauss/data/stmm_write_cal.tmp"

    try:
        out, _ = omm_run(
            f"find /opt/openGauss/data/base -type f -size +{READ_MB}M | head -1", timeout=10)
        data_file = out.strip()
        if not data_file:
            raise ValueError("no large data file found")
        dd_cmd = (f"dd if={data_file} of=/dev/null bs=1M count={READ_MB} iflag=direct 2>&1")
        t_start = time.time()
        omm_run(dd_cmd, timeout=120)
        elapsed = time.time() - t_start
        if elapsed > 0.1:
            read_cost = elapsed / READ_MB
            log(f"  [Calibrate] read_cost={read_cost:.4f} s/MB  "
                f"({READ_MB}MB dd in {elapsed:.1f}s = {READ_MB/elapsed:.0f} MB/s)")
        else:
            log(f"  [Calibrate] dd read too fast ({elapsed:.2f}s) — using default")
            read_cost = DEFAULT[0]
    except Exception as e:
        log(f"  [Calibrate] read cost failed: {e} — using default")
        read_cost = DEFAULT[0]

    try:
        omm_run(f"rm -f {WRITE_FILE}", timeout=5)
        dd_cmd = (f"dd if=/dev/zero of={WRITE_FILE} bs=1M count={WRITE_MB} "
                  f"oflag=direct conv=fdatasync 2>&1")
        t_start = time.time()
        omm_run(dd_cmd, timeout=120)
        elapsed = time.time() - t_start
        omm_run(f"rm -f {WRITE_FILE}", timeout=5)
        if elapsed > 0.1:
            write_cost = elapsed / WRITE_MB
            log(f"  [Calibrate] write_cost={write_cost:.4f} s/MB  "
                f"({WRITE_MB}MB dd in {elapsed:.1f}s = {WRITE_MB/elapsed:.0f} MB/s)")
        else:
            log(f"  [Calibrate] dd too fast ({elapsed:.2f}s) — using default write_cost")
            write_cost = DEFAULT[1]
    except Exception as e:
        log(f"  [Calibrate] write cost failed: {e} — using default")
        omm_run(f"rm -f {WRITE_FILE}", timeout=5)
        write_cost = DEFAULT[1]

    return read_cost, write_cost


def _norm_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).lower()


def explain_ap_query():
    """Return (rows, tuple_width_bytes) for WM sizing.

    Priority:
      1. workloads.json override — used when planner statistics are known wrong.
      2. EXPLAIN scan — picks the WM-consuming node with max (rows×width).
      3. workloads.json fallback — only if SQL is registered and EXPLAIN fails.
    Raises RuntimeError if SQL is not in workloads.json and EXPLAIN yields nothing.
    """
    WM_NODES = re.compile(
        r"^\s*(?:->)?\s*"
        r"(Sort|Hash(?:\s+Join)?|HashAggregate|MergeJoin|WindowAgg|Unique)\b",
        re.IGNORECASE,
    )
    cur_wl = next((w for w in WORKLOADS if w["ap_sql"] == AP_SQL), None)

    if cur_wl is not None:
        override = cur_wl.get("explain_cardinality_override")
        if override is not None:
            return override

    rows, width = None, None
    try:
        out, _ = gsql_q(f"EXPLAIN {AP_SQL};", db="sbtest", timeout=15)
        best_footprint = 0
        for line in out.split("\n"):
            if not WM_NODES.match(line):
                continue
            m = re.search(r"rows=(\d+)\s+width=(\d+)", line)
            if not m:
                continue
            r, w = int(m.group(1)), int(m.group(2))
            if r * w > best_footprint:
                best_footprint = r * w
                rows, width = r, w
    except Exception:
        pass

    if rows is not None:
        return rows, width

    if cur_wl is not None:
        return cur_wl["explain_fallback_rows"], cur_wl["explain_fallback_width"]

    raise RuntimeError(
        "explain_ap_query: EXPLAIN returned nothing and AP_SQL is not in workloads.json."
    )


def check_cardinality_error():
    """Compare planner's row estimate against actual rows from dbe_perf.statement_history."""
    WM_NODES = re.compile(
        r"^\s*(?:->)?\s*"
        r"(Sort|Hash(?:\s+Join)?|HashAggregate|MergeJoin|WindowAgg|Unique)\b",
        re.IGNORECASE,
    )
    fingerprint = re.sub(r"\s+", " ", AP_SQL.strip())[-60:].replace("'", "''")
    sql = (
        "SELECT n_returned_rows FROM dbe_perf.statement_history "
        f"WHERE query LIKE '%{fingerprint}%' "
        "  AND n_returned_rows > 0 "
        "ORDER BY finish_time DESC LIMIT 20;"
    )
    try:
        out, err = gsql_q(sql, db="postgres", timeout=15)
        vals = [int(x.strip()) for x in out.split("\n")
                if x.strip().isdigit() and int(x.strip()) > 0]
        if not vals:
            log(f"  [CardCheck] no rows in statement_history matching AP_SQL (err={err.strip()[:80]})")
            return
        vals.sort()
        act_rows = vals[len(vals) // 2]
    except Exception as e:
        log(f"  [CardCheck] statement_history query failed: {e}")
        return

    est_rows, width = None, 184
    try:
        out, _ = gsql_q(f"EXPLAIN {AP_SQL};", db="sbtest", timeout=15)
        best = 0
        for line in out.split("\n"):
            if not WM_NODES.match(line):
                continue
            m = re.search(r"rows=(\d+)\s+width=(\d+)", line)
            if not m:
                continue
            r, w = int(m.group(1)), int(m.group(2))
            if r * w > best:
                best = r * w
                est_rows, width = r, w
    except Exception as e:
        log(f"  [CardCheck] EXPLAIN failed: {e}")

    if est_rows is None or act_rows == 0:
        return

    error = abs(est_rows - act_rows) / act_rows
    log(f"  [CardCheck] est={est_rows:,}  actual={act_rows:,}  error={error:.1%}  width={width}")
    if error > 0.15:
        log(f"  [CardCheck] error > 15% — updating workloads.json: rows={act_rows} width={width}")
        from workloads import update_cardinality, WORKLOADS as _WL
        if any(w["ap_sql"] == AP_SQL for w in _WL):
            update_cardinality(AP_SQL, act_rows, width)
        else:
            log(f"  [CardCheck] AP_SQL not in workloads.json — add manually to persist override")
    else:
        log(f"  [CardCheck] within threshold, no update")


def get_n_ap():
    out2, _ = omm_run(
        f"{GSQL} -d postgres -c \"SELECT count(*) FROM pg_stat_activity "
        f"WHERE state='active' AND query ILIKE '%order by c desc%' "
        f"AND query NOT LIKE '%pg_stat%';\"",
        timeout=10)
    row = parse_row(out2)
    return int(row[0]) if row else 0


_bp_snapshot = []


def snapshot_buffer_pool():
    """Capture which relation pages are in shared_buffers via pg_buffercache_pages()."""
    global _bp_snapshot
    sql = """
        SELECT c.relname, b.relforknumber, max(b.relblocknumber::bigint) AS max_block,
               count(*) AS pages
        FROM pg_buffercache_pages() b
        JOIN pg_class c ON c.relfilenode = b.relfilenode
        WHERE b.isvalid
          AND b.reldatabase = (SELECT oid FROM pg_database WHERE datname='sbtest')
        GROUP BY c.relname, b.relforknumber
        ORDER BY pages DESC;
    """
    out, err = gsql_q(sql, db="sbtest", timeout=30)
    snapshot = []
    for line in out.split("\n"):
        line = line.strip()
        if not line or line.startswith("-") or line.startswith("relname") or line.startswith("("):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            try:
                snapshot.append({
                    "relname":   parts[0],
                    "fork":      int(parts[1]),
                    "max_block": int(parts[2]),
                    "pages":     int(parts[3]) if len(parts) > 3 else 0,
                })
            except ValueError:
                pass
    _bp_snapshot = snapshot
    total_pages = sum(e["pages"] for e in snapshot)
    log(f"  [Snapshot] Captured {len(snapshot)} relations, "
        f"{total_pages} pages ({total_pages * PAGE_SIZE_KB / 1024:.0f} MB) in shared_buffers")


def prewarm_buffer_pool(target_sb_mb):
    """Replay the buffer pool snapshot captured by snapshot_buffer_pool()."""
    if not _bp_snapshot:
        log("  [Prewarm] No snapshot available — skipping prewarm")
        return
    rels = [e["relname"] for e in _bp_snapshot if e["fork"] == 0]
    if not rels:
        log("  [Prewarm] Snapshot has no main-fork pages — skipping")
        return
    log(f"  [Prewarm] Reloading {len(rels)} relations into SB={target_sb_mb}MB...")
    t0 = time.time()
    loaded = 0
    for rel in rels:
        try:
            out, err = gsql_q(f"SELECT count(*) FROM \"{rel}\";", db="sbtest", timeout=120)
            if "row" in out:
                loaded += 1
        except Exception as e:
            log(f"  [Prewarm] WARNING: scan of {rel} failed: {e}")
    elapsed = time.time() - t0
    log(f"  [Prewarm] Done: {loaded}/{len(rels)} relations in {elapsed:.1f}s")


def reset_between_runs(target_sb_mb=None):
    """Reset between configs: DB restart + scaled warmup. No drop_caches."""
    kill_ap()
    sb_target = target_sb_mb or SB_MB
    warmup_s = min(420, max(180, int(180 * sb_target / SB_MB)))
    set_guc("shared_buffers", f"{sb_target}MB")
    set_guc("work_mem", "64MB")
    log(f"  Resetting: restart DB (SB={sb_target}MB) + warmup {warmup_s}s...")
    compact_memory()
    ok = restart_db()
    if not ok:
        log("  WARNING: restart_db() failed — continuing anyway")
    gsql_q("SELECT pg_stat_reset();", db="sbtest", timeout=15)
    log(f"  Warming up {warmup_s}s...")
    omm_run(SB_CMD.format(duration=warmup_s), timeout=warmup_s + 30)
    log("  Reset complete.")


def check_mem_free_gb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)
    except Exception:
        pass
    return 99.0


def read_meminfo() -> dict:
    """Read key fields from /proc/meminfo. Returns values in MB."""
    result = {}
    try:
        fields = {"MemAvailable": "mem_avail_mb", "MemFree": "mem_free_mb",
                  "SwapTotal": "_swap_total_kb", "SwapFree": "_swap_free_kb"}
        with open("/proc/meminfo") as f:
            for line in f:
                key = line.split(":")[0]
                if key in fields:
                    kb = int(line.split()[1])
                    result[fields[key]] = round(kb / 1024, 0)
        swap_used = result.pop("_swap_total_kb", 0) - result.pop("_swap_free_kb", 0)
        result["swap_used_mb"] = max(0.0, swap_used)
    except Exception:
        pass
    return result


# ── AP injection ──────────────────────────────────────────────────────────────

_ap_procs = []

AP_LAT_LOG = "/tmp/stmm10_ap_lat.log"


def launch_ap(ap_dur_s):
    global _ap_procs
    omm_run("kill -9 $(pgrep -u omm -f 'gsql -d sbtest') 2>/dev/null; true", timeout=15)
    time.sleep(2)
    open(AP_LAT_LOG, "w").close()
    os.chmod(AP_LAT_LOG, 0o666)

    sql = re.sub(r"\s+", " ", AP_SQL.strip())
    script = (
        f"#!/bin/bash\n"
        f"LATLOG=\"{AP_LAT_LOG}\"\n"
        f"END=$((SECONDS + {ap_dur_s + 60}))\n"
        f"while [ $SECONDS -lt $END ]; do\n"
        f"  T0=$(date +%s%3N)\n"
        f"  {GSQL} -d sbtest -c \"{sql}\" >/dev/null 2>&1\n"
        f"  T1=$(date +%s%3N)\n"
        f"  echo \"$((T1 - T0))\" >> \"$LATLOG\"\n"
        f"  sleep 1\n"
        f"done\n"
    )
    with open("/tmp/stmm10_ap.sh", "w") as f:
        f.write(script)
    os.chmod("/tmp/stmm10_ap.sh", 0o755)
    _ap_procs = []
    for _ in range(AP_CONC):
        p = subprocess.Popen(
            ["su", "-", "omm", "-c", "/tmp/stmm10_ap.sh"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.stdin.write((OMM_PASS + "\n").encode())
        p.stdin.close()
        _ap_procs.append(p)
    time.sleep(3)


def read_ap_latency() -> dict:
    """Read AP_LAT_LOG and return latency stats (ms). Returns {} if no data."""
    try:
        vals = [int(x) for x in open(AP_LAT_LOG).read().split() if x.strip().isdigit()]
    except Exception:
        return {}
    if not vals:
        return {}
    vals.sort()
    n = len(vals)
    return {
        "ap_lat_n":      n,
        "ap_lat_avg_ms": round(sum(vals) / n, 0),
        "ap_lat_med_ms": vals[n // 2],
        "ap_lat_p95_ms": vals[int(n * 0.95)],
        "ap_lat_p99_ms": vals[int(n * 0.99)],
        "ap_lat_max_ms": vals[-1],
    }


def kill_ap():
    global _ap_procs
    for _ in range(3):
        try:
            gsql_q("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                   "WHERE query LIKE '%FROM sbtest%' AND query LIKE '%ORDER BY%' "
                   "AND pid!=pg_backend_pid();",
                   timeout=15)
        except Exception:
            pass
        time.sleep(1)
    omm_run("kill -9 $(pgrep -u omm -f 'gsql -d sbtest') 2>/dev/null; true", timeout=15)
    for p in _ap_procs:
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
    _ap_procs = []
    for _ in range(90):
        if check_mem_free_gb() >= 4.0:
            break
        time.sleep(1)
