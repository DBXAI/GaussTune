#!/usr/bin/env python3
"""
STMM Live Test — OpenGauss TP+AP Mixed Workload
Demonstrates DB2 STMM (VLDB 2006) adapted for OpenGauss, with WM+SB co-tuning.

Experiment design:
  PRE  [0–60s]:   TP-only, STMM calibrates; may recommend SB increase
  TRANSITION:     If STMM suggests SB↑, apply it (restart DB) before AP injection
  AP   [60–420s]: AP injected (4 concurrent sort queries), STMM adapts WM
  POST [420–540s]: AP stops, STMM recovers WM (and eventually SB)

Configs:
  Static-Default:      WM=64MB,   SB=6GB  (baseline)
  STMM (WM+SB auto):  WM→auto,   SB→auto (main experiment)
  Static-Expert-WM:   WM=1024MB, SB=6GB  (WM-only oracle)
  Static-Expert-Full: WM=1024MB, SB=10GB (full oracle — target for STMM)

Output: JSON + log in refine-logs/results/stmm_test_results.json
"""

import subprocess, time, re, os, json, threading
from datetime import datetime
from stmm_controller import STMMController, BRBEController, ProactiveBRBEController, PAGE_SIZE_KB
from workloads import WORKLOADS

USE_BRBE      = True   # set False to use original STMMController
USE_PROACTIVE = True   # set False to fall back to reactive BRBEController

# ── Config ────────────────────────────────────────────────────────────────────
GSQL     = "/opt/openGauss/app/bin/gsql"
OMM_PASS = "1997"
LOG_PATH = "/home/node/GaussTune/run-logs/stmm_run11.log"
JSON_OUT = "/home/node/GaussTune/run-logs/stmm_run11_results.json"
RESULTS_DIR = "/home/node/GaussTune/run-logs"
PERF_EVENTS  = "dTLB-load-misses,longest_lat_cache.miss,longest_lat_cache.reference,cycles"
PERF_PRE_OUT = "/tmp/stmm10_perf_pre.txt"
PERF_AP_OUT  = "/tmp/stmm10_perf_ap.txt"

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

SB_CMD = (
    "LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu "
    "sysbench oltp_read_write "
    "--db-driver=pgsql --pgsql-host=/tmp --pgsql-port=5432 "
    "--pgsql-user=omm --pgsql-password= --pgsql-db=sbtest "
    "--tables=10 --table-size=2000000 "
    "--db-ps-mode=disable --threads=16 --rand-type=uniform "
    "--report-interval=5 --time={duration} run"
)

# Workload 1: sort-heavy seq scan (uses ring buffer → doesn't pressure SB)
AP_SQL = "SELECT k, c, pad FROM sbtest1 ORDER BY c DESC, pad ASC, k DESC"

AP_CONC    = 4
AP_DUR     = 360
PRE_AP_S   = 60
POST_AP_S  = 180
TOTAL_S    = PRE_AP_S + AP_DUR + POST_AP_S   # 540s

STMM_POLL  = 15
WM_INIT    = 64
WM_EXPERT  = 512    # sort threshold: 2M rows×212B=424MB → 512MB avoids spill
SB_MB      = 1024   # baseline SB: small → IO bottleneck (4.6GB data >> 1024MB SB)
SB_EXPERT  = 4096   # expert SB: covers most of TP working set, no TLB issue (THP=on)
RAM_MB        = 14700  # physical RAM (14.7GB); used for OOM guard in apply_sb_change
OS_RESERVE_MB = 2048   # headroom for OS, sshd, sysbench, etc.
PRE2_AP_S  = 30     # post-proactive TP-only measurement for fair drop baseline

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
    """Trigger kernel memory compaction to defragment physical pages before large SB alloc.
    Improves THP 2MB-page allocation success rate. Requires sudoers entry (no password).

    drop_caches: set True only before LARGE SB allocations where THP must form 2MB pages.
    Do NOT set True before baseline resets — it clears the OS page cache and causes
    cold-start warmup (pre_tps drops to <30 TPS instead of ~170 TPS).
    """
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
    # Wait for socket to appear (DB process started)
    for _ in range(90):
        if os.path.exists("/tmp/.s.PGSQL.5432"):
            break
        time.sleep(2)
    else:
        return False
    time.sleep(20)
    # Wait for DB to be ready: 3 consecutive SELECT 1 successes.
    # 60 attempts at 3s = up to 3 min (covers WAL crash-recovery after unclean kill).
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
    """Verify DB is fully ready; retry restart once if not. Raises on persistent failure."""
    out, _ = omm_run(f"{GSQL} -d postgres -c 'SELECT 1;'", timeout=10)
    if "1 row" in out or "(1 row)" in out:
        return
    log(f"  DB not responding{' (' + label + ')' if label else ''} — retrying restart...")
    ok = restart_db()
    if not ok:
        raise RuntimeError("DB failed to come up after retry — aborting")


def apply_sb_change(new_sb_mb, stmm_controller=None, warmup_s=60, current_wm_mb=None):
    """Apply a shared_buffers change via ALTER SYSTEM + DB restart + re-warmup.

    OOM guard: if new_sb_mb + AP_CONC * current_wm + OS_RESERVE > RAM_MB, temporarily
    drop work_mem to 64MB before restarting (two gaussdb processes briefly coexist during
    restart, so peak usage = new_sb + old_sb + AP workers). Restores WM after warmup.
    """
    wm_was_lowered = False
    if current_wm_mb and current_wm_mb > WM_INIT:
        peak_mb = new_sb_mb + SB_MB + AP_CONC * current_wm_mb + OS_RESERVE_MB
        if peak_mb > RAM_MB:
            log(f"  → OOM guard: peak={peak_mb}MB > RAM={RAM_MB}MB — dropping WM to {WM_INIT}MB during restart")
            set_guc("work_mem", f"{WM_INIT}MB")
            wm_was_lowered = True

    log(f"  → STMM SB change: {new_sb_mb}MB — compacting memory + restarting DB...")
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
    if delay_s > 0:
        time.sleep(delay_s)
    try:
        omm_run(f"perf stat -e {PERF_EVENTS} -p {gdb_pid} -o {out_path} -- sleep {duration_s}",
                timeout=duration_s + 30)
    except Exception as e:
        log(f"  [perf] collection error: {e}")


def calibrate_io_costs() -> tuple[float, float]:
    """
    Measure actual disk read and write throughput on this machine.

    Both use dd with O_DIRECT against the OpenGauss data directory
    (same filesystem as buffer pool data files and sort temp files),
    bypassing the OS page cache to get raw disk speed.

    read_cost_s_per_mb  = elapsed / READ_MB   (sequential read)
    write_cost_s_per_mb = elapsed / WRITE_MB  (sequential write, fdatasync)

    Returns (read_cost_s_per_mb, write_cost_s_per_mb).
    Falls back to defaults (0.01, 0.01) on any error.
    """
    DEFAULT = (0.01, 0.01)
    READ_MB  = 256
    WRITE_MB = 256
    WRITE_FILE = "/opt/openGauss/data/stmm_write_cal.tmp"

    # ── Read cost: dd sequential read from data directory (O_DIRECT) ───────────
    try:
        # Find a data file large enough to read READ_MB from
        out, _ = omm_run(
            f"find /opt/openGauss/data/base -type f -size +{READ_MB}M "
            f"| head -1", timeout=10)
        data_file = out.strip()
        if not data_file:
            raise ValueError("no large data file found")
        dd_cmd = (f"dd if={data_file} of=/dev/null bs=1M count={READ_MB} "
                  f"iflag=direct 2>&1")
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

    # ── Write cost: dd sequential write to data directory (O_DIRECT) ───────────
    WRITE_MB   = 256
    WRITE_FILE = "/opt/openGauss/data/stmm_write_cal.tmp"
    try:
        # Remove stale file if exists
        omm_run(f"rm -f {WRITE_FILE}", timeout=5)
        dd_cmd = (f"dd if=/dev/zero of={WRITE_FILE} bs=1M count={WRITE_MB} "
                  f"oflag=direct conv=fdatasync 2>&1")
        t_start = time.time()
        out, _ = omm_run(dd_cmd, timeout=120)
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
    """Return (rows, tuple_width_bytes) for WM sizing, or raise RuntimeError if unavailable.

    Priority:
      1. workloads.json override — used when planner statistics are known wrong.
      2. EXPLAIN scan — picks the WM-consuming node with max (rows×width).
      3. workloads.json fallback — only if SQL is registered and EXPLAIN fails/matches nothing.
    Raises RuntimeError if SQL is not in workloads.json and EXPLAIN yields nothing.
    """
    WM_NODES = re.compile(
        r"^\s*(?:->)?\s*"
        r"(Sort|Hash(?:\s+Join)?|HashAggregate|MergeJoin|WindowAgg|Unique)\b",
        re.IGNORECASE,
    )
    cur_wl = next((w for w in WORKLOADS if w["ap_sql"] == AP_SQL), None)

    # 1. Ground-truth override
    if cur_wl is not None:
        override = cur_wl.get("explain_cardinality_override")
        if override is not None:
            return override

    # 2. EXPLAIN scan
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

    # 3. workloads.json fallback — only valid if SQL is registered
    if cur_wl is not None:
        return cur_wl["explain_fallback_rows"], cur_wl["explain_fallback_width"]

    raise RuntimeError(
        "explain_ap_query: EXPLAIN returned nothing and AP_SQL is not in workloads.json. "
        "Add the query with known cardinality to workloads.json before running."
    )


def check_cardinality_error():
    """Compare planner's row estimate against actual rows from dbe_perf.statement_history.

    OpenGauss records n_returned_rows for every completed statement — no extra execution
    needed.  We query statement_history for entries matching AP_SQL that finished during
    the AP phase, take the median n_returned_rows, then compare against EXPLAIN's estimate.
    Updates workloads.json if error > 15%.
    """
    WM_NODES = re.compile(
        r"^\s*(?:->)?\s*"
        r"(Sort|Hash(?:\s+Join)?|HashAggregate|MergeJoin|WindowAgg|Unique)\b",
        re.IGNORECASE,
    )
    # Build a short fingerprint from the tail of AP_SQL (ORDER BY clause is distinctive)
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
        act_rows = vals[len(vals) // 2]  # median
    except Exception as e:
        log(f"  [CardCheck] statement_history query failed: {e}")
        return

    # Estimated rows + width via fast EXPLAIN (no execution)
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


# Global buffer pool snapshot — captured once after the initial warmup,
# replayed before every config run so all configs start with identical SB contents.
_bp_snapshot = []   # list of (relname, fork, max_block)


def snapshot_buffer_pool():
    """
    Capture which relation pages are currently in shared_buffers via
    pg_buffercache_pages(). Stores (relname, fork, max_blockno) per relation
    so we can replay the same pages via full-relation scans after restart.
    Only captures pages belonging to the sbtest database.
    """
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
    """
    Replay the buffer pool snapshot captured by snapshot_buffer_pool().
    After a DB restart, issue a sequential scan on each snapshotted relation
    to pull its pages back into the new shared_buffers.

    Uses SELECT count(*) scans (no data returned to client) so it's fast.
    Fork 0 = main heap, fork 1 = FSM, fork 2 = VM, fork 3 = init.
    We only scan fork 0 (heap) and any index relations (no fork suffix needed).
    """
    if not _bp_snapshot:
        log("  [Prewarm] No snapshot available — skipping prewarm")
        return

    # Collect unique relation names from snapshot (fork=0 only to avoid FSM/VM noise)
    rels = [e["relname"] for e in _bp_snapshot if e["fork"] == 0]
    if not rels:
        log("  [Prewarm] Snapshot has no main-fork pages — skipping")
        return

    log(f"  [Prewarm] Reloading {len(rels)} relations into SB={target_sb_mb}MB...")
    t0 = time.time()
    loaded = 0
    for rel in rels:
        try:
            # Sequential scan — loads all heap pages into shared_buffers
            out, err = gsql_q(f"SELECT count(*) FROM \"{rel}\";",
                              db="sbtest", timeout=120)
            if "row" in out:
                loaded += 1
        except Exception as e:
            log(f"  [Prewarm] WARNING: scan of {rel} failed: {e}")
    elapsed = time.time() - t0
    log(f"  [Prewarm] Done: {loaded}/{len(rels)} relations in {elapsed:.1f}s")


def reset_between_runs(target_sb_mb=None):
    """Reset between configs: DB restart (clears shared_buffers) + scaled warmup.
    Warmup scales with SB size so buffer pool is equally warm fraction regardless of SB.
    No drop_caches — OS page cache keeps TP pages warm for stable pre_tps."""
    kill_ap()
    sb_target = target_sb_mb or SB_MB
    # Scale warmup proportionally to SB so hit ratio is consistent across configs.
    # Base: SB_MB → 120s warmup. SB=6144MB → 360s, capped at 420s.
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


# ── Sysbench subprocess helper ────────────────────────────────────────────────

def run_sysbench_phase(duration_s, tps_timeline, stmm_timeline,
                       phase_offset_s, ap_inject_at, ap_kill_at,
                       stmm_obj, stmm_stop, sysbench_ready,
                       current_wm_ref, current_sb_ref, pending_sb_ref,
                       pending_sb_lock, label, prev_stats_ref=None):
    """
    Run sysbench for duration_s seconds, streaming TPS output.
    When a mid-AP SB change is applied (requires DB restart), sysbench is
    terminated cleanly before the restart, then restarted for the remaining
    duration so TPS collection continues uninterrupted.
    phase_offset_s + accumulated_offset + local_offset = global timeline t.
    Returns (final_tps, final_p95, ap_injected, ap_killed).
    """
    t_start          = time.time()
    accumulated_offset = 0   # sysbench seconds consumed by completed subprocesses
    ap_inject_time   = None
    ap_kill_wall     = None
    ap_kill_offset   = None  # global offset when AP was actually killed (for phase labeling)
    sb_lines         = []
    final_tps, final_p95 = 0.0, None
    ap_injected = ap_inject_at is None
    ap_killed   = ap_kill_at  is None

    while True:  # restart loop: re-enter after each mid-AP SB change
        remaining_s = duration_s - accumulated_offset
        if remaining_s < 10:
            break

        sb_proc = subprocess.Popen(
            ["su", "-", "omm", "-c", SB_CMD.format(duration=int(remaining_s))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        sb_proc.stdin.write(OMM_PASS + "\n")
        sb_proc.stdin.flush()

        sb_start_wall  = time.time()
        restart_needed = False

        for line in sb_proc.stdout:
            line = line.rstrip()
            sb_lines.append(line)
            elapsed = time.time() - t_start
            now     = time.time()

            # AP injection
            if not ap_injected and ap_inject_at is not None and elapsed >= ap_inject_at:
                log(f"  → t={phase_offset_s+elapsed:.0f}s AP INJECT "
                    f"({AP_CONC} workers, wm={current_wm_ref[0]}MB, sb={current_sb_ref[0]}MB)")
                launch_ap(AP_DUR)
                ap_inject_time = now
                ap_kill_wall   = now + AP_DUR
                ap_injected    = True

            # AP kill — wall-clock deadline so restarts don't shorten AP window
            if ap_injected and not ap_killed and ap_kill_wall is not None and now >= ap_kill_wall:
                elapsed_at_kill = time.time() - t_start
                ap_kill_offset = phase_offset_s + accumulated_offset + int(elapsed_at_kill)
                log(f"  → t={ap_kill_offset}s AP KILL")
                kill_ap()
                ap_killed = True

            # Post-AP SB change: apply pending SB recommendation after AP ends
            # (not mid-AP, to avoid disrupting AP TPS measurements)
            if ap_killed and pending_sb_ref is not None:
                with pending_sb_lock:
                    sb_to_apply = pending_sb_ref[0]
                    if sb_to_apply and sb_to_apply != current_sb_ref[0]:
                        pending_sb_ref[0] = None
                    else:
                        sb_to_apply = None

                if sb_to_apply:
                    log(f"  → t={phase_offset_s+elapsed:.0f}s POST-AP SB CHANGE: "
                        f"{current_sb_ref[0]}→{sb_to_apply}MB")
                    kill_ap()  # ensure AP is gone
                    # Terminate sysbench, apply SB change, restart sysbench for POST
                    sb_proc.terminate()
                    try:
                        sb_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        sb_proc.kill()
                        sb_proc.wait()
                    accumulated_offset += int(time.time() - sb_start_wall)
                    ok = apply_sb_change(sb_to_apply, stmm_controller=stmm_obj, warmup_s=30,
                                         current_wm_mb=current_wm_ref[0])
                    if ok:
                        current_sb_ref[0] = sb_to_apply
                        if prev_stats_ref is not None:
                            stats = list(get_db_stats())
                            prev_stats_ref[0], prev_stats_ref[1], prev_stats_ref[2] = stats
                    restart_needed = True
                    break

            # Parse TPS
            m = re.search(r"\[\s*(\d+)s\s*\].*tps:\s*([\d.]+)", line)
            if m:
                if not sysbench_ready.is_set():
                    sysbench_ready.set()
                local_offset = int(m.group(1))
                offset = phase_offset_s + accumulated_offset + local_offset
                tps    = float(m.group(2))
                # Use actual kill offset for phase labeling when AP was extended by restart
                ap_end = ap_kill_offset if ap_kill_offset is not None else (phase_offset_s + AP_DUR)
                ap_start = phase_offset_s  # AP begins at phase start (inject_at=0 for phase2)
                phase  = ("pre" if offset <= PRE_AP_S else
                          "ap"  if offset <= ap_end else "post")
                tps_timeline.append({"offset": offset, "t": offset,
                                      "tps": tps, "phase": phase,
                                      "wm_mb": current_wm_ref[0],
                                      "sb_mb": current_sb_ref[0]})
                if ap_killed and ap_kill_offset is not None and offset >= ap_kill_offset + POST_AP_S:
                    log(f"  → Post phase complete at t={offset}s — terminating sysbench")
                    sb_proc.terminate()
                    break
                elif ap_killed and ap_kill_offset is None and offset >= PRE_AP_S + AP_DUR + POST_AP_S:
                    log(f"  → Post phase complete at t={offset}s — terminating sysbench")
                    sb_proc.terminate()
                    break

            mf = re.search(r"transactions:\s+\d+\s+\(([\d.]+) per sec\.\)", line)
            if mf:
                final_tps = float(mf.group(1))
            mp = re.search(r"95th percentile:\s+([\d.]+)", line)
            if mp:
                final_p95 = float(mp.group(1))

        sb_proc.wait(timeout=30)
        if not restart_needed:
            break  # normal exit; do not restart

    if not tps_timeline and not sb_lines:
        log(f"  WARN: no TPS data in phase (duration={duration_s}s)")
        for l in sb_lines[:20]:
            log(f"    | {l}")

    return final_tps, final_p95, ap_injected, ap_killed


# ── Single experiment run ─────────────────────────────────────────────────────

def run_config(label: str, wm_fixed: int | None, use_stmm: bool, sb_fixed: int | None = None,
               ref_pre_tps: float | None = None,
               init_wm_override: int | None = None, init_sb_override: int | None = None):
    """
    Run one TP+AP experiment.
    wm_fixed: static work_mem (MB) for non-STMM runs.
    use_stmm: if True, STMM tunes WM (and optionally SB) autonomously.
    sb_fixed: if set, use this SB value (for Expert-Full); otherwise use SB_MB.
    init_wm_override: STMM starting WM (MB) — overrides WM_INIT to test tuning from high state.
    init_sb_override: STMM starting SB (MB) — overrides SB_MB to test tuning from high state.
    """
    log(f"\n{'='*70}")
    log(f"CONFIG: {label}  wm_fixed={wm_fixed}  sb_fixed={sb_fixed or SB_MB}  stmm={use_stmm}")
    avail = check_mem_free_gb()
    log(f"  Free RAM: {avail}GB")
    if avail < 4.0:
        log("  SKIP — insufficient RAM")
        return None

    gdb_pid  = get_gaussdb_pid()
    perf_pre: dict = {}
    perf_ap:  dict = {}

    if use_stmm and init_wm_override:
        init_wm = init_wm_override
    else:
        init_wm = WM_INIT if use_stmm else wm_fixed
    init_sb = init_sb_override if (use_stmm and init_sb_override) else (sb_fixed or SB_MB)
    set_guc("work_mem", f"{init_wm}MB")
    time.sleep(2)

    if use_stmm:
        if USE_PROACTIVE and USE_BRBE:
            stmm = ProactiveBRBEController(wm_init_mb=init_wm, sb_init_mb=init_sb,
                                            poll_s=STMM_POLL, n_ap_workers=AP_CONC)
        elif USE_BRBE:
            stmm = BRBEController(wm_init_mb=init_wm, sb_init_mb=init_sb, poll_s=STMM_POLL)
        else:
            stmm = STMMController(wm_init_mb=init_wm, sb_init_mb=init_sb, poll_s=STMM_POLL)
    else:
        stmm = None

    tps_timeline  = []
    stmm_timeline = []
    mem_timeline  = []
    current_wm    = [init_wm]          # mutable ref for cross-thread access
    current_sb    = [init_sb]
    pending_sb    = [None]             # STMM thread writes here
    pre2_tps      = None               # TP TPS after proactive SB change (fair drop baseline)
    pending_sb_lock = threading.Lock()
    sysbench_ready  = threading.Event()
    stmm_stop       = threading.Event()
    mem_stop        = threading.Event()
    prev_stats = list(get_db_stats())   # [hit, read, temp] — mutable for thread updates
    pre_start_stats = list(prev_stats)  # snapshot at run start for PRE-phase delta
    run_start_wall  = time.time()

    def mem_sample_thread():
        while not mem_stop.is_set():
            m = read_meminfo()
            m["t"] = round(time.time() - run_start_wall, 0)
            m["sb_mb"] = current_sb[0]
            mem_timeline.append(m)
            mem_stop.wait(15)

    t_mem = threading.Thread(target=mem_sample_thread, daemon=True)
    t_mem.start()

    def stmm_thread_v2():
        sysbench_ready.wait(timeout=30)
        while not stmm_stop.is_set():
            stmm_stop.wait(STMM_POLL)
            if stmm_stop.is_set():
                break
            try:
                hit, read, temp = get_db_stats()
                n_ap = get_n_ap()
                dh = hit  - prev_stats[0]
                dr = read - prev_stats[1]
                dt = temp - prev_stats[2]
                prev_stats[0], prev_stats[1], prev_stats[2] = hit, read, temp

                new_wm, suggest_sb = stmm.tick(dh, dr, dt, n_ap)
                if new_wm != current_wm[0]:
                    set_guc("work_mem", f"{new_wm}MB")
                    current_wm[0] = new_wm

                # Only queue SB suggestions while AP is running; ignore POST-phase noise
                if suggest_sb is not None and suggest_sb != current_sb[0] and n_ap > 0:
                    with pending_sb_lock:
                        if pending_sb[0] is None:
                            pending_sb[0] = suggest_sb
                    log(f"  STMM: SB suggestion {current_sb[0]}→{suggest_sb}MB (pending)")

                stmm_timeline.append({
                    "ts":    time.time(),
                    "wm_mb": new_wm,
                    "sb_mb": current_sb[0],
                    "log":   stmm.summary(),
                })
                log(f"  STMM: {stmm.summary()}")
            except Exception as e:
                log(f"  STMM thread error: {e}")

    final_tps, final_p95 = 0.0, None

    if use_stmm:
        # ── Two-phase run: PRE → (optional SB change) → AP+POST ──────────────

        # Phase 1: PRE only (STMM calibrates, may recommend SB increase)
        log(f"  [Phase 1] PRE {PRE_AP_S}s — STMM calibrating...")
        perf_pre_ev = threading.Event()
        if gdb_pid:
            def _pre_perf():
                _run_perf_phase(gdb_pid, PERF_PRE_OUT, PRE_AP_S)
                perf_pre_ev.set()
            threading.Thread(target=_pre_perf, daemon=True).start()
        else:
            perf_pre_ev.set()
        t_stmm = threading.Thread(target=stmm_thread_v2, daemon=True)
        t_stmm.start()

        ft, fp, _, _ = run_sysbench_phase(
            duration_s      = PRE_AP_S,
            tps_timeline    = tps_timeline,
            stmm_timeline   = stmm_timeline,
            phase_offset_s  = 0,
            ap_inject_at    = None,   # no AP in phase 1
            ap_kill_at      = None,
            stmm_obj        = stmm,
            stmm_stop       = stmm_stop,
            sysbench_ready  = sysbench_ready,
            current_wm_ref  = current_wm,
            current_sb_ref  = current_sb,
            pending_sb_ref  = pending_sb,
            pending_sb_lock = pending_sb_lock,
            label           = label,
        )

        # Stop STMM thread while we potentially restart DB
        stmm_stop.set()
        t_stmm.join(timeout=10)
        stmm_stop.clear()
        perf_pre_ev.wait(timeout=PRE_AP_S + 30)
        perf_pre = _parse_perf_output(PERF_PRE_OUT) if gdb_pid else {}

        # Apply SB change if recommended
        with pending_sb_lock:
            sb_to_apply = pending_sb[0]
            pending_sb[0] = None

        if sb_to_apply and sb_to_apply != current_sb[0]:
            ok = apply_sb_change(sb_to_apply, stmm_controller=stmm, warmup_s=60,
                                 current_wm_mb=current_wm[0])
            if ok:
                current_sb[0] = sb_to_apply
                # Reset pg_stat counters so phase-2 deltas are clean
                gsql_q("SELECT pg_stat_reset();", db="sbtest", timeout=15)

        # ── Proactive BRBE: predict WM and SB from AP query shape + TP state ────
        if isinstance(stmm, ProactiveBRBEController):
            log("  [Proactive] EXPLAIN AP_SQL → predicting WM/SB needs...")
            ap_rows, ap_width = explain_ap_query()
            blks_hit_delta  = prev_stats[0] - pre_start_stats[0]
            blks_read_delta = prev_stats[1] - pre_start_stats[1]
            mem = read_meminfo()
            mem_avail_mb = mem.get("mem_avail_mb", 4096)
            total_budget_mb = int((mem_avail_mb + current_sb[0]) * 0.60)
            log(f"  [Proactive] MemAvailable={mem_avail_mb:.0f}MB  SB={current_sb[0]}MB  "
                f"→ total_budget={total_budget_mb}MB (60% of MemAvail+SB)")
            wm_rec, sb_rec = stmm.predict_pre_ap(
                ap_rows, ap_width, blks_hit_delta, blks_read_delta, AP_CONC,
                total_budget_mb=total_budget_mb, pre_s=PRE_AP_S)
            entry = stmm.log[-1]
            log(f"  [Proactive] rows={ap_rows} width={ap_width}B "
                f"→ WM_rec={wm_rec}MB  SB_rec={sb_rec}MB  "
                f"(budget={total_budget_mb}MB  input={entry['input_mb']}MB  "
                f"tp_ws={entry['tp_ws_mb']}MB  B_total={entry['B_total']:.4f}  iters={entry['iters_used']})")

            if wm_rec != current_wm[0]:
                set_guc("work_mem", f"{wm_rec}MB")
                current_wm[0] = wm_rec
                stmm.wm_mb = float(wm_rec)
                log(f"  [Proactive] WM → {wm_rec}MB (applied before AP injection)")

            if sb_rec > current_sb[0]:
                # Scale warmup same as reset_between_runs for fair comparison
                proactive_warmup_s = min(420, max(120, int(120 * sb_rec / SB_MB)))
                log(f"  [Proactive] SB {current_sb[0]}→{sb_rec}MB — restarting DB (warmup={proactive_warmup_s}s)...")
                ok = apply_sb_change(sb_rec, stmm_controller=stmm, warmup_s=proactive_warmup_s,
                                     current_wm_mb=current_wm[0])
                if ok:
                    current_sb[0] = sb_rec
                    gsql_q("SELECT pg_stat_reset();", db="sbtest", timeout=15)
            else:
                log(f"  [Proactive] SB sufficient ({current_sb[0]}MB ≥ predicted {sb_rec}MB) — no restart")

        # After any proactive SB change, measure TP-only at the new SB as fair drop baseline.
        # This ensures drop = (pre2_tps - ap_tps)/pre2_tps compares same-SB conditions.
        if current_sb[0] > SB_MB:
            log(f"  [Proactive] pre2 measurement ({PRE2_AP_S}s TP-only at SB={current_sb[0]}MB)...")
            pre2_tps = measure_tps(PRE2_AP_S)
            log(f"  [Proactive] pre2_tps={pre2_tps:.1f} TPS — used as drop baseline")
            gsql_q("SELECT pg_stat_reset();", db="sbtest", timeout=15)

        # Phase 2: AP + POST + buffer for post-AP SB change restart.
        # SB changes now apply after AP ends (not mid-AP), so POST phase may include a restart.
        SB_RESTART_BUFFER = 180
        log(f"  [Phase 2] AP {AP_DUR}s + POST {POST_AP_S}s (+{SB_RESTART_BUFFER}s restart buffer) — starting...")
        # Notify controller: AP phase is starting — HOLD WM at proactive floor
        if isinstance(stmm, ProactiveBRBEController):
            stmm.start_ap_phase()
        # Re-fetch PID: DB may have been restarted during proactive SB change
        _ap_gdb_pid = get_gaussdb_pid()
        perf_ap_ev = threading.Event()
        if _ap_gdb_pid:
            def _ap_perf():
                _run_perf_phase(_ap_gdb_pid, PERF_AP_OUT, AP_DUR)
                perf_ap_ev.set()
            threading.Thread(target=_ap_perf, daemon=True).start()
        else:
            perf_ap_ev.set()
        sysbench_ready.clear()
        t_stmm2 = threading.Thread(target=stmm_thread_v2, daemon=True)
        t_stmm2.start()

        ft2, fp2, _, _ = run_sysbench_phase(
            duration_s      = AP_DUR + POST_AP_S + SB_RESTART_BUFFER,
            tps_timeline    = tps_timeline,
            stmm_timeline   = stmm_timeline,
            phase_offset_s  = PRE_AP_S,
            ap_inject_at    = 0,
            ap_kill_at      = AP_DUR,
            stmm_obj        = stmm,
            stmm_stop       = stmm_stop,
            sysbench_ready  = sysbench_ready,
            current_wm_ref  = current_wm,
            current_sb_ref  = current_sb,
            pending_sb_ref  = pending_sb,
            pending_sb_lock = pending_sb_lock,
            label           = label,
            prev_stats_ref  = prev_stats,
        )

        stmm_stop.set()
        t_stmm2.join(timeout=10)
        # AP phase ended — allow WM recovery during POST (if run again)
        if isinstance(stmm, ProactiveBRBEController):
            stmm.end_ap_phase()
        perf_ap_ev.wait(timeout=AP_DUR + 60)
        perf_ap = _parse_perf_output(PERF_AP_OUT) if _ap_gdb_pid else {}

        final_tps = ft2 or ft
        final_p95 = fp2 or fp

    else:
        # ── Single-phase run for static configs ───────────────────────────────
        perf_pre_ev = threading.Event()
        perf_ap_ev  = threading.Event()
        if gdb_pid:
            def _pre_perf():
                _run_perf_phase(gdb_pid, PERF_PRE_OUT, PRE_AP_S)
                perf_pre_ev.set()
            def _ap_perf():
                _run_perf_phase(gdb_pid, PERF_AP_OUT, AP_DUR, delay_s=PRE_AP_S)
                perf_ap_ev.set()
            threading.Thread(target=_pre_perf, daemon=True).start()
            threading.Thread(target=_ap_perf, daemon=True).start()
        else:
            perf_pre_ev.set()
            perf_ap_ev.set()
        final_tps, final_p95, _, _ = run_sysbench_phase(
            duration_s      = TOTAL_S,
            tps_timeline    = tps_timeline,
            stmm_timeline   = stmm_timeline,
            phase_offset_s  = 0,
            ap_inject_at    = PRE_AP_S,
            ap_kill_at      = PRE_AP_S + AP_DUR,
            stmm_obj        = None,
            stmm_stop       = stmm_stop,
            sysbench_ready  = sysbench_ready,
            current_wm_ref  = current_wm,
            current_sb_ref  = current_sb,
            pending_sb_ref  = pending_sb,
            pending_sb_lock = pending_sb_lock,
            label           = label,
        )
        perf_pre_ev.wait(timeout=PRE_AP_S + 30)
        perf_ap_ev.wait(timeout=PRE_AP_S + AP_DUR + 60)
        perf_pre = _parse_perf_output(PERF_PRE_OUT) if gdb_pid else {}
        perf_ap  = _parse_perf_output(PERF_AP_OUT)  if gdb_pid else {}

    if not tps_timeline:
        log(f"  WARN: no TPS data for {label}")
        return None

    def pavg(col, ph):
        rows = [x[col] for x in tps_timeline if x["phase"] == ph]
        return round(sum(rows) / len(rows), 2) if rows else None

    def pavg_last(col, ph, last_s=30):
        """Average over the last last_s seconds of a phase — skips cold ramp."""
        rows = [x for x in tps_timeline if x["phase"] == ph]
        if not rows:
            return None
        max_offset = max(x["offset"] for x in rows)
        tail = [x[col] for x in rows if x["offset"] >= max_offset - last_s]
        return round(sum(tail) / len(tail), 2) if tail else pavg(col, ph)

    pre_tps  = pavg_last("tps", "pre", last_s=30)  # last 30s only — skips cold ramp
    ap_tps   = pavg("tps", "ap")
    post_tps = pavg("tps", "post")
    # Drop baseline priority: pre2_tps (same-SB fair) > ref_pre_tps (shared) > pre_tps
    base     = pre2_tps if pre2_tps else (ref_pre_tps if ref_pre_tps else pre_tps)
    drop     = round(100 * (1 - ap_tps / base), 1)  if base and ap_tps   else None
    recovery = round(100 * post_tps / base, 1)       if base and post_tps else None

    ap_lat = read_ap_latency()
    ap_qps = round(ap_lat.get("ap_lat_n", 0) / AP_DUR, 2) if AP_DUR > 0 else 0.0
    pre2_str = f"  pre2={pre2_tps}" if pre2_tps else ""
    log(f"  TPS: pre={pre_tps}{pre2_str}  ap={ap_tps}  post={post_tps}  "
        f"drop={drop}%  recovery={recovery}%")
    if ap_lat:
        log(f"  AP latency (n={ap_lat['ap_lat_n']}): "
            f"avg={ap_lat['ap_lat_avg_ms']:.0f}ms  "
            f"med={ap_lat['ap_lat_med_ms']}ms  "
            f"p95={ap_lat['ap_lat_p95_ms']}ms  "
            f"p99={ap_lat['ap_lat_p99_ms']}ms  "
            f"max={ap_lat['ap_lat_max_ms']}ms")
    if ap_qps > 0:
        log(f"  AP QPS: {ap_qps} q/s ({ap_lat.get('ap_lat_n', 0)} queries / {AP_DUR}s)")
    if perf_pre:
        log(f"  [Perf PRE] dTLB-miss={perf_pre.get('dtlb_miss', 0):,}  "
            f"L3-miss={perf_pre.get('l3_miss_pct', 'N/A')}%  "
            f"cycles={perf_pre.get('cycles', 0):,}")
    if perf_ap:
        log(f"  [Perf AP]  dTLB-miss={perf_ap.get('dtlb_miss', 0):,}  "
            f"L3-miss={perf_ap.get('l3_miss_pct', 'N/A')}%  "
            f"cycles={perf_ap.get('cycles', 0):,}")

    log("  TPS timeline:")
    for t_entry in tps_timeline:
        bar = "█" * int((t_entry["tps"] or 0) / 10)
        log(f"    t={t_entry['t']:>4}s [{t_entry['phase']:>4}] "
            f"wm={t_entry['wm_mb']:>5}MB  sb={t_entry['sb_mb']:>5}MB  "
            f"TPS={t_entry['tps']:>7.1f}  {bar}")

    if use_stmm and stmm_timeline:
        log("  STMM tuning history:")
        for e in stmm_timeline:
            log(f"    {e['log']}")

    log("  [CardCheck] running EXPLAIN ANALYZE to verify planner cardinality...")
    check_cardinality_error()

    mem_stop.set()
    t_mem.join(timeout=5)

    # Log mem timeline summary: min/max MemAvailable during AP phase
    ap_mem = [m for m in mem_timeline if PRE_AP_S <= m.get("t", 0) <= PRE_AP_S + AP_DUR]
    if ap_mem:
        avail_vals = [m["mem_avail_mb"] for m in ap_mem if "mem_avail_mb" in m]
        swap_vals  = [m["swap_used_mb"] for m in ap_mem if "swap_used_mb" in m]
        if avail_vals:
            log(f"  [Mem AP]  MemAvail min={min(avail_vals):.0f}MB  max={max(avail_vals):.0f}MB  "
                f"swap_max={max(swap_vals):.0f}MB")

    return {
        "label":        label,
        "use_stmm":     use_stmm,
        "wm_fixed":     wm_fixed,
        "sb_fixed":     sb_fixed or SB_MB,
        "init_wm":      init_wm,
        "pre_tps":      pre_tps,
        "pre2_tps":     pre2_tps,
        "ap_tps":       ap_tps,
        "post_tps":     post_tps,
        "tps_drop_pct": drop,
        "recovery_pct": recovery,
        "full_tps":     final_tps,
        "full_p95_ms":  final_p95,
        "tps_timeline": tps_timeline,
        "stmm_timeline": stmm_timeline,
        "mem_timeline": mem_timeline,
        "final_wm_mb":  current_wm[0],
        "final_sb_mb":  current_sb[0],
        "ap_qps":       ap_qps,
        "perf_pre":     perf_pre,
        "perf_ap":      perf_ap,
        **ap_lat,
    }


# WORKLOADS loaded from workloads.py / workloads.json


def peak_mem_mb(sb_mb, wm_mb, conc=AP_CONC):
    """Estimate peak memory: SB + conc×WM + OS reserve."""
    return sb_mb + conc * wm_mb + OS_RESERVE_MB


def safe_sb_max(wm_mb=WM_EXPERT, conc=AP_CONC):
    """Maximum SB that keeps peak memory under RAM_MB."""
    return RAM_MB - conc * wm_mb - OS_RESERVE_MB - 512  # 512MB gaussdb overhead


def run_workload(workload: dict, wl_index: int, t_global):
    """Run all configs for one workload, save JSON, return list of result dicts."""
    global AP_SQL
    import stmm_controller as _sc

    name   = workload["name"]
    AP_SQL = workload["ap_sql"]

    # Apply per-workload WM_MAX override if needed (e.g. join3 needs 4096MB)
    orig_wm_max = _sc.WM_MAX_MB
    if "wm_max_override" in workload:
        _sc.WM_MAX_MB = workload["wm_max_override"]
        log(f"  [Workload] WM_MAX_MB overridden to {_sc.WM_MAX_MB}MB for {name}")

    # Per-workload safe SB ceiling
    sb_expert_safe = min(SB_EXPERT, safe_sb_max())
    log(f"  [Workload] safe SB ceiling = {sb_expert_safe}MB "
        f"(RAM={RAM_MB} - 4×{WM_EXPERT} - {OS_RESERVE_MB} OS - 512 overhead)")

    log(f"\n{'#'*80}")
    log(f"WORKLOAD {wl_index+1}: {name.upper()} — {workload['desc']}")
    log(f"AP_SQL: {AP_SQL[:80]}...")
    log(f"{'#'*80}")

    results = []

    # Run 1: Static-Default
    # Retry r1 up to 2 times if DB wasn't ready (no TPS data)
    for attempt in range(3):
        r1 = run_config("Static-Default (WM=64MB)", wm_fixed=64, use_stmm=False)
        if r1 and r1.get("pre_tps"):
            break
        log(f"  r1 attempt {attempt+1} failed (no TPS) — ensuring DB ready and retrying...")
        ensure_db_ready("r1 retry")
        reset_between_runs(target_sb_mb=SB_MB)
    if r1:
        results.append(r1)
    shared_pre_tps = r1["pre_tps"] if r1 else None
    log(f"  Shared pre_tps baseline: {shared_pre_tps} TPS")
    reset_between_runs(target_sb_mb=SB_MB)

    # Run 2: STMM+ProactiveBRBE
    if USE_PROACTIVE and USE_BRBE:
        stmm_label = "STMM+ProactiveBRBE (WM+SB→predict)"
    elif USE_BRBE:
        stmm_label = "STMM+BRBE (WM+SB→auto)"
    else:
        stmm_label = "STMM (WM+SB→auto)"
    r2 = run_config(stmm_label, wm_fixed=None, use_stmm=True, ref_pre_tps=shared_pre_tps)
    if r2:
        results.append(r2)
    reset_between_runs(target_sb_mb=SB_MB)

    # Restore baseline
    _sc.WM_MAX_MB = orig_wm_max
    set_guc("shared_buffers", f"{SB_MB}MB")
    set_guc("work_mem", "64MB")

    # Print summary
    log(f"\n{'='*80}")
    log(f"SUMMARY — {name.upper()}")
    log(f"{'='*80}")
    log(f"  {'Label':<52} {'WM_i':>6} {'WM_f':>6} {'SB_f':>6} "
        f"{'pre':>6} {'ap':>6} {'drop%':>6} {'rec%':>6}")
    log(f"  {'-'*96}")
    for r in results:
        log(f"  {r['label']:<52} {r['init_wm']:>6} {r['final_wm_mb']:>6} "
            f"{r['final_sb_mb']:>6} "
            f"{str(r['pre_tps']):>6} {str(r['ap_tps']):>6} "
            f"{str(r['tps_drop_pct']):>6} {str(r['recovery_pct']):>6}")

    r2_res = next((r for r in results if r["use_stmm"]), None)
    r1_res = next((r for r in results if "Default" in r["label"]), None)
    if r2_res and r1_res:
        imp = (r1_res["tps_drop_pct"] or 0) - (r2_res["tps_drop_pct"] or 0)
        log(f"\nSTMM improvement vs Default: {imp:.1f}pp drop reduction")

    # Save per-workload JSON
    json_path = os.path.join(RESULTS_DIR, f"stmm_results_{name}.json")
    out_data = {
        "date":      datetime.now().isoformat(),
        "workload":  name,
        "ap_sql":    AP_SQL,
        "sb_mb":     SB_MB,
        "sb_expert": sb_expert_safe,
        "ap_conc":   AP_CONC,
        "pre_s":     PRE_AP_S,
        "ap_s":      AP_DUR,
        "post_s":    POST_AP_S,
        "results":   [{k: v for k, v in r.items() if k != "stmm_timeline"} for r in results],
        "stmm_tuning_log": r2_res["stmm_timeline"] if r2_res else [],
    }
    with open(json_path, "w") as f:
        json.dump(out_data, f, indent=2)
    # Also update the combined JSON_OUT for backwards compat
    with open(JSON_OUT, "w") as f:
        json.dump(out_data, f, indent=2)
    log(f"Saved: {json_path}")

    return results


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    t0 = datetime.now()
    log(f"STMM Test — {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"SB_INIT={SB_MB}MB  SB_EXPERT={SB_EXPERT}MB  AP_CONC={AP_CONC}")
    log(f"PRE={PRE_AP_S}s  AP={AP_DUR}s  POST={POST_AP_S}s  STMM_POLL={STMM_POLL}s")
    log(f"Workloads: {[w['name'] for w in WORKLOADS]}")

    # Initial DB setup
    kill_ap()
    set_guc("shared_buffers", f"{SB_MB}MB")
    set_guc("work_mem", "64MB")
    log("\nRestarting DB (SB=6GB baseline)...")
    ok = restart_db()
    if not ok:
        time.sleep(60)
        ok = restart_db()
    ensure_db_ready("initial startup")
    log("  DB ready.")
    gsql_q("SELECT pg_stat_reset();", db="sbtest", timeout=15)
    import stmm_controller as _sc
    # Sync controller SB floor with test baseline so STMM never shrinks below SB_MB
    _sc.SB_INIT_MB = SB_MB
    _sc.SB_MIN_MB  = SB_MB
    read_cost, write_cost = calibrate_io_costs()
    _sc.DISK_READ_COST_S_PER_MB  = read_cost
    _sc.DISK_WRITE_COST_S_PER_MB = write_cost
    log(f"  I/O costs: read={read_cost:.4f} s/MB  write={write_cost:.4f} s/MB")

    log("Warmup 120s...")
    omm_run(SB_CMD.format(duration=120), timeout=150)
    log("Pre-run-1 reset (restart DB + warmup 120s)...")
    reset_between_runs(target_sb_mb=SB_MB)

    all_results = {}
    for i, wl in enumerate(WORKLOADS):
        try:
            results = run_workload(wl, i, t0)
            all_results[wl["name"]] = results
        except Exception as e:
            log(f"ERROR in workload {wl['name']}: {e}")
            import traceback
            log(traceback.format_exc())
        # Reset between workloads
        if i < len(WORKLOADS) - 1:
            log(f"\nResetting between workloads...")
            reset_between_runs(target_sb_mb=SB_MB)

    log(f"\nAll workloads complete. Total: {(datetime.now()-t0).total_seconds()/60:.1f} min")
    _log_file.close()


if __name__ == "__main__":
    import sys
    # Write PID file so external monitors can track this process
    pid_file = "/home/node/GaussTune/run-logs/stmm_test.pid"
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))
    try:
        main()
    finally:
        try:
            os.remove(pid_file)
        except Exception:
            pass
