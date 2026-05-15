#!/usr/bin/env python3
"""
SB Penalty Calibration — HUGE-PAGES variant
============================================
Identical protocol to sb_calib.py but each SB level is backed 100% by HugeTLB
(2 MB pages) instead of the kernel's default 4 KB pages. Used to answer
"is TLB pressure the cause of the SB > 4 GB TPS cliff observed in sb_calib?"

Per-level changes vs sb_calib.py:
  - reserve_huge_pages(sb_mb): set nr_hugepages = sb_mb/2 + 5-10% headroom
                                + enable THP `always` on enabled/defrag/shmem
  - set_guc("enable_huge_pages", "on") before DB restart
  - DB teardown at end: nr_hugepages=0, THP back to madvise, enable_huge_pages=off

PREREQUISITES (one-time host setup, will fail without them):

  1. vm.hugetlb_shm_group must include omm's GID
     ---------------------------------------------
     gaussdb runs as `omm` (gid 1001 = dbgroup), and SHM_HUGETLB shmget() is
     gated by /proc/sys/vm/hugetlb_shm_group. Default value 0 means only root
     can use SHM_HUGETLB; if you leave it at 0, gaussdb startup with
     enable_huge_pages=on will FAIL with:
        FATAL: could not create shared memory segment: Operation not permitted
        Failed system call was shmget(..., 0o3600).
                                       ^---- 0o3600 = IPC_CREAT|SHM_HUGETLB|0600

     Fix (persisted in /etc/sysctl.d/99-gausstune-hugetlb.conf on this host):
        echo 1001 | sudo tee /proc/sys/vm/hugetlb_shm_group        # runtime
        echo "vm.hugetlb_shm_group = 1001" | sudo tee \\
             /etc/sysctl.d/99-gausstune-hugetlb.conf               # persistent
        sudo sysctl --system   # to apply

     Replace 1001 with `id -g omm` if omm has a different gid on your host.

  2. NO need to raise omm's memlock (RLIMIT_MEMLOCK)
     ---------------------------------------------
     SHM_HUGETLB does not consume the memlock budget (unlike mmap(MAP_LOCKED)).
     ulimit -l can stay at the default 65536 kB.

  3. enable_huge_pages requires DB restart (not reload)
     ---------------------------------------------
     This script uses ALTER SYSTEM SET + restart_db(), which is the correct
     sequence: ALTER SYSTEM writes postgresql.auto.conf, then restart picks it
     up at startup. Do NOT expect pg_reload_conf() alone to apply it (the LOG
     line "cannot be changed without restarting the server" is informational,
     not fatal — the subsequent restart in measure_at_sb does the actual work).

  4. Memory accounting: HugeTLB pool is NOT a double allocation
     ---------------------------------------------
     nr_hugepages=N reserves 2N MB carved out of free memory; shared_buffers
     then occupies this same pool (not a separate 4 KB allocation). So the
     "OS cache budget" for sysbench/sbtest data is identical to the no-HP
     case: TotalMem - shared_buffers - process overhead. Only the PTE
     mechanism differs (2 MB vs 4 KB pages → lower TLB pressure with HP).

  5. Fragmentation can prevent full pool allocation
     ---------------------------------------------
     When MemFree is fragmented (typical after warm OS cache), the kernel
     may not be able to compact enough contiguous 2 MB regions to satisfy
     `echo N > /proc/sys/vm/nr_hugepages`. The script logs a WARNING if the
     resulting pool is smaller than SB. If shmget fails because of this,
     gaussdb will refuse to start and the level is recorded as error.

Output:
  run-logs/sb_calib_ro_hp.{log,json}
"""

from __future__ import annotations
import subprocess, time, re, os, json, math, threading
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
GSQL         = "/opt/openGauss/app/bin/gsql"
OMM_PASS     = "1997"
LOG_PATH     = "/home/node/GaussTune/run-logs/sb_calib_ro_hp.log"
JSON_OUT     = "/home/node/GaussTune/run-logs/sb_calib_ro_hp.json"

BASE_SB_MB   = 1024
SB_LEVELS    = [1024, 2048, 4096, 6144, 8192, 12288, 16384]

WARMUP_S     = 180
MEASURE_S    = 60

PERF_EVENTS  = ("dTLB-load-misses,longest_lat_cache.miss,longest_lat_cache.reference,"
                "cycles,stalled-cycles-backend,stalled-cycles-frontend,page-faults")
SUDO_PASS    = "1997"

SB_CMD = (
    "LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu "
    "sysbench oltp_read_only "
    "--db-driver=pgsql --pgsql-host=/tmp --pgsql-port=5432 "
    "--pgsql-user=omm --pgsql-password= --pgsql-db=sbtest "
    "--tables=10 --table-size=10000000 "
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
    tmp = "/tmp/sbcalib_stats.sql"
    with open(tmp, "w") as f:
        f.write("SELECT blks_hit, blks_read FROM pg_stat_database WHERE datname='sbtest';")
    os.chmod(tmp, 0o666)
    out, _ = omm_run(f"{GSQL} -d postgres -f {tmp}", timeout=15)
    row = parse_row(out)
    if len(row) >= 2:
        return int(row[0]), int(row[1])
    return 0, 0

def get_bgwriter_stats() -> dict:
    """Query pg_stat_bgwriter for write-origin attribution counters."""
    sql = ("SELECT buffers_checkpoint, buffers_clean, buffers_backend, "
           "buffers_backend_fsync, checkpoints_timed, checkpoints_req, "
           "maxwritten_clean "
           "FROM pg_stat_bgwriter;")
    tmp = "/tmp/sbcalib_bgw.sql"
    with open(tmp, "w") as f:
        f.write(sql)
    os.chmod(tmp, 0o666)
    out, _ = omm_run(f"{GSQL} -d postgres -f {tmp}", timeout=15)
    row = parse_row(out)
    keys = ["buffers_checkpoint", "buffers_clean", "buffers_backend",
            "buffers_backend_fsync", "checkpoints_timed", "checkpoints_req",
            "maxwritten_clean"]
    result = {}
    for i, k in enumerate(keys):
        try:
            result[k] = int(row[i]) if i < len(row) else 0
        except (ValueError, IndexError):
            result[k] = 0
    return result

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
    for line in reversed(output.split("\n")):
        m = re.search(r"transactions:\s+\d+\s+\((\d+\.\d+)\s+per sec", line)
        if m:
            return float(m.group(1))
    tps_samples = []
    for line in output.split("\n"):
        m = re.search(r"\[\s*\d+s\s*\].*tps:\s*([\d.]+)", line)
        if m:
            tps_samples.append(float(m.group(1)))
    return sum(tps_samples) / len(tps_samples) if tps_samples else 0.0

# ── perf ──────────────────────────────────────────────────────────────────────
def run_perf(duration_s: int, out_file: str, pid: int):
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
    counts = {}
    try:
        with open(out_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) < 3:
                    continue
                val_str = parts[0].strip()
                event   = parts[2].strip()
                try:
                    counts[event] = int(val_str.replace(",", ""))
                except ValueError:
                    pass
    except Exception:
        pass
    return counts

# ── vmstat ────────────────────────────────────────────────────────────────────
def run_vmstat(duration_s: int, out_file: str):
    try:
        r = subprocess.run(f"vmstat 1 {duration_s}", shell=True,
                           capture_output=True, text=True, timeout=duration_s + 10)
        with open(out_file, "w") as f:
            f.write(r.stdout)
    except Exception as e:
        log(f"  [warn] vmstat: {e}")

def parse_vmstat(out_file: str) -> dict:
    si_list, so_list, b_list, wa_list = [], [], [], []
    try:
        with open(out_file) as f:
            lines = f.readlines()
        for line in lines[2:]:
            parts = line.split()
            if len(parts) < 16:
                continue
            try:
                b_list.append(int(parts[1]))
                si_list.append(int(parts[6]))
                so_list.append(int(parts[7]))
                wa_list.append(int(parts[15]))
            except ValueError:
                continue
    except Exception:
        pass
    def avg(lst): return round(sum(lst) / len(lst), 2) if lst else 0
    return {
        "vmstat_b_avg":  avg(b_list),
        "vmstat_si_avg": avg(si_list),
        "vmstat_so_avg": avg(so_list),
        "vmstat_wa_avg": avg(wa_list),
    }

# ── iostat ────────────────────────────────────────────────────────────────────
def _data_device() -> str:
    """Detect the block device backing the OpenGauss data directory."""
    try:
        out = subprocess.check_output(["df", "/opt/openGauss/data"], text=True)
        raw = out.split("\n")[1].split()[0]       # e.g. /dev/sda1 or /dev/nvme0n1p1
        dev = raw.rsplit("/", 1)[-1]
        m = re.match(r"(nvme\d+n\d+|[a-z]+)", dev)
        return m.group(1) if m else "sda"
    except Exception:
        return "sda"

def run_iostat(duration_s: int, out_file: str):
    dev = _data_device()
    try:
        r = subprocess.run(f"iostat -xk {dev} 1 {duration_s}",
                           shell=True, capture_output=True, text=True,
                           timeout=duration_s + 10)
        with open(out_file, "w") as f:
            f.write(r.stdout)
    except Exception as e:
        log(f"  [warn] iostat: {e}")

def parse_iostat(out_file: str) -> dict:
    """Parse iostat -xk output; return per-interval averages of wkB/s, w_await, %util."""
    wkb_list, w_await_list, util_list = [], [], []
    try:
        with open(out_file) as f:
            lines = f.readlines()
        col_map: dict = {}
        for line in lines:
            line = line.rstrip()
            if re.match(r"Device", line):
                cols = line.split()
                col_map = {c: i for i, c in enumerate(cols)}
                continue
            if not col_map or not line.strip():
                continue
            if re.match(r"Linux|avg-cpu", line):
                continue
            parts = line.split()
            try:
                wkb_idx  = col_map.get("wkB/s",   col_map.get("kB_wrtn/s", None))
                w_aw_idx = col_map.get("w_await",  col_map.get("await",     None))
                util_idx = col_map.get("%util",    None)
                if wkb_idx  is not None and wkb_idx  < len(parts):
                    wkb_list.append(float(parts[wkb_idx]))
                if w_aw_idx is not None and w_aw_idx < len(parts):
                    w_await_list.append(float(parts[w_aw_idx]))
                if util_idx is not None and util_idx < len(parts):
                    util_list.append(float(parts[util_idx]))
            except (ValueError, IndexError):
                pass
    except Exception:
        pass
    def avg(lst): return round(sum(lst) / len(lst), 1) if lst else 0.0
    return {"wkb_s": avg(wkb_list), "w_await": avg(w_await_list), "util_pct": avg(util_list)}

# ── wait events ───────────────────────────────────────────────────────────────
def get_wait_events() -> list:
    sql = ("SELECT COALESCE(wait_event_type,'CPU') as type, "
           "COALESCE(wait_event,'running') as event, count(*) "
           "FROM pg_stat_activity WHERE state='active' "
           "GROUP BY 1,2 ORDER BY 3 DESC;")
    tmp = "/tmp/sbcalib_wait.sql"
    with open(tmp, "w") as f:
        f.write(sql)
    os.chmod(tmp, 0o666)
    out, _ = omm_run(f"{GSQL} -d postgres -f {tmp}", timeout=10)
    events = []
    in_data = False
    for line in out.split("\n"):
        if re.match(r"\s*-+", line):
            in_data = True
            continue
        if in_data and line.strip() and not line.strip().startswith("("):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                try:
                    events.append({"type": parts[0], "event": parts[1],
                                   "count": int(parts[2])})
                except ValueError:
                    pass
    return events

# ── Measurement ───────────────────────────────────────────────────────────────
def reserve_huge_pages(sb_mb: int):
    """Reserve nr_hugepages = sb_mb/2 + 10% headroom (2MB each).

    Sized to exactly cover shared_buffers so SB is 100% backed by 2MB pages.
    Note: this does NOT double-count memory — HugeTLB pool is carved out of
    MemFree, and shared_buffers occupies that pool (not separate). OS cache
    available = TotalMem - SB - process overhead, identical to the no-HP case.

    Compaction may briefly evict OS cache to free 2MB-contiguous regions,
    but OS cache refills via the warmup sysbench run before measurement.
    """
    n_pages   = (sb_mb // 2) + max(64, (sb_mb // 20))   # +5-10% headroom
    try:
        # Compact memory first so kernel can find 2MB-contiguous regions
        subprocess.run(["sudo", "tee", "/proc/sys/vm/compact_memory"],
                       input="1\n", capture_output=True, text=True, timeout=10)
        time.sleep(2)
        # Set nr_hugepages (kernel may not honor full request if fragmented)
        r = subprocess.run(["sudo", "tee", "/proc/sys/vm/nr_hugepages"],
                           input=f"{n_pages}\n", capture_output=True, text=True, timeout=15)
        # Verify
        with open("/proc/sys/vm/nr_hugepages") as f:
            actual = int(f.read().strip())
        with open("/proc/meminfo") as f:
            free = sum(int(l.split()[1]) for l in f if l.startswith("HugePages_Free"))
        actual_mb = actual * 2
        required_mb = sb_mb
        log(f"  [HP] target={sb_mb}MB, requested={n_pages} pages, "
            f"got {actual} pages ({actual_mb}MB), free={free}")
        if actual_mb < required_mb:
            log(f"  [HP] WARNING: pool {actual_mb}MB < SB {required_mb}MB — "
                f"shmget may fail (kernel couldn't compact enough)")
        # Also nudge THP enabled=always (THP for anon outside shared_buffers)
        for knob, val in [("enabled", "always"), ("defrag", "always"),
                          ("shmem_enabled", "always")]:
            subprocess.run(["sudo", "tee", f"/sys/kernel/mm/transparent_hugepage/{knob}"],
                           input=f"{val}\n", capture_output=True, text=True, timeout=5)
    except Exception as e:
        log(f"  [HP] reservation error: {e}")


def measure_at_sb(sb_mb: int) -> dict:
    log(f"\n{'='*60}")
    log(f"  SB = {sb_mb} MB  (HUGE-PAGES round)")
    log(f"{'='*60}")

    # iter5-hp: reserve huge-page pool BEFORE applying shared_buffers + restart.
    # nr_hugepages = min(sb_mb, 8192)/2 + headroom, so SB up to 8GB is fully
    # backed by 2MB pages; SB > 8GB falls back partly to 4KB (intentional).
    reserve_huge_pages(sb_mb)

    compact_memory()
    set_guc("shared_buffers", f"{sb_mb}MB")
    # Enable HugeTLB in gaussdb (huge_pages=on means SHM_HUGETLB on shmget)
    set_guc("enable_huge_pages", "on")
    ok = restart_db()
    if not ok:
        log(f"  ERROR: DB restart failed at SB={sb_mb}MB — skipping")
        return {"sb_mb": sb_mb, "tps": None, "error": "restart_failed"}

    # Verify gaussdb actually grabbed huge pages
    try:
        with open("/proc/meminfo") as f:
            mi = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    k = parts[0].strip()
                    v = parts[1].strip().split()
                    if v and v[0].isdigit():
                        mi[k] = int(v[0])
        hp_used = mi.get("HugePages_Total", 0) - mi.get("HugePages_Free", 0)
        log(f"  [HP] after DB restart: HugePages_Total={mi.get('HugePages_Total',0)}, "
            f"Free={mi.get('HugePages_Free',0)}, used={hp_used} ({hp_used*2}MB), "
            f"AnonHP={mi.get('AnonHugePages',0)}kB, ShmemHP={mi.get('ShmemHugePages',0)}kB")
    except Exception:
        pass

    mem_before = read_meminfo()
    log(f"  MemAvailable after restart: {mem_before}MB")

    log(f"  Warmup {WARMUP_S}s ...")
    gsql_q("SELECT pg_stat_reset();")
    omm_run(SB_CMD.format(duration=WARMUP_S), timeout=WARMUP_S + 30)

    log(f"  Measuring {MEASURE_S}s (+ perf + bgwriter + iostat) ...")
    perf_out   = f"/tmp/sbcalib_perf_{sb_mb}.txt"
    vmstat_out = f"/tmp/sbcalib_vmstat_{sb_mb}.txt"
    iostat_out = f"/tmp/sbcalib_iostat_{sb_mb}.txt"
    pids = subprocess.check_output(["pgrep", "-x", "gaussdb"]).split()
    gaussdb_pid = int(pids[0]) if pids else 0

    # Snapshot counters at start of measurement window
    hit0, read0 = get_db_stats()
    bw0 = get_bgwriter_stats()

    perf_thread   = threading.Thread(
        target=run_perf,   args=(MEASURE_S, perf_out,   gaussdb_pid), daemon=True)
    vmstat_thread = threading.Thread(
        target=run_vmstat, args=(MEASURE_S, vmstat_out),              daemon=True)
    iostat_thread = threading.Thread(
        target=run_iostat, args=(MEASURE_S, iostat_out),              daemon=True)
    perf_thread.start()
    vmstat_thread.start()
    iostat_thread.start()

    wait_results = []
    def _sample_wait():
        time.sleep(20)
        return get_wait_events()
    wait_thread = threading.Thread(
        target=lambda: wait_results.append(_sample_wait()), daemon=True)
    wait_thread.start()

    out, _ = omm_run(SB_CMD.format(duration=MEASURE_S), timeout=MEASURE_S + 30)
    perf_thread.join(timeout=MEASURE_S + 15)
    vmstat_thread.join(timeout=MEASURE_S + 15)
    iostat_thread.join(timeout=MEASURE_S + 15)
    wait_thread.join(timeout=5)
    wait_events = wait_results[0] if wait_results else []

    # Snapshot counters at end of measurement window
    hit1, read1 = get_db_stats()
    bw1 = get_bgwriter_stats()
    bw_delta = {k: bw1.get(k, 0) - bw0.get(k, 0) for k in bw0}

    tps    = parse_sysbench_tps(out)
    dh     = hit1  - hit0
    dr     = read1 - read0
    total  = dh + dr
    hit_rate = dh / total if total > 0 else 1.0
    mem_after = read_meminfo()

    perf = parse_perf(perf_out)
    vmst = parse_vmstat(vmstat_out)
    iost = parse_iostat(iostat_out)

    dtlb     = perf.get("dTLB-load-misses", 0)
    l3m      = perf.get("longest_lat_cache.miss", 0)
    l3r      = perf.get("longest_lat_cache.reference", 0)
    cyc      = perf.get("cycles", 0)
    stall_be = perf.get("stalled-cycles-backend", 0)
    pgflt    = perf.get("page-faults", 0)
    txns     = max(1, tps * MEASURE_S)
    l3_pct   = round(l3m / l3r * 100, 2) if l3r > 0 else 0.0
    stall_pct = round(stall_be / cyc * 100, 2) if cyc > 0 else 0.0

    # Convert buffer counts (pages) to MB  (1 page = 8 KB)
    chkpt_mb   = round(bw_delta["buffers_checkpoint"] * 8 / 1024, 1)
    clean_mb   = round(bw_delta["buffers_clean"]       * 8 / 1024, 1)
    backend_mb = round(bw_delta["buffers_backend"]     * 8 / 1024, 1)
    n_chkpts   = bw_delta["checkpoints_timed"] + bw_delta["checkpoints_req"]

    log(f"  TPS               = {tps:.1f}")
    log(f"  blks_hit          = {dh:,}  blks_read = {dr:,}  hit_rate = {hit_rate*100:.2f}%")
    log(f"  MemAvail          = {mem_after}MB")
    log(f"  L3-miss/txn       = {l3m/txns:.0f}  L3-miss% = {l3_pct}%")
    log(f"  stall-backend/txn = {stall_be/txns:.0f}  stall% = {stall_pct}%")
    log(f"  page-faults/txn   = {pgflt/txns:.2f}")
    log(f"  cycles/txn        = {cyc/txns:.0f}")
    log(f"  vmstat b={vmst['vmstat_b_avg']}  si={vmst['vmstat_si_avg']}  "
        f"so={vmst['vmstat_so_avg']}  wa={vmst['vmstat_wa_avg']}%")
    log(f"  bgwriter: chkpt={chkpt_mb}MB  clean={clean_mb}MB  "
        f"backend={backend_mb}MB  n_checkpoints={n_chkpts}")
    log(f"  iostat:   wkB/s={iost['wkb_s']}  w_await={iost['w_await']}ms  "
        f"util={iost['util_pct']}%")
    if wait_events:
        log(f"  wait_events       = " +
            "  ".join(f"{e['type']}/{e['event']}×{e['count']}" for e in wait_events[:3]))
    log(f"  Recorded: SB={sb_mb}MB → TPS={tps:.2f}")

    return {
        "sb_mb":               sb_mb,
        "tps":                 round(tps, 2),
        "blks_hit":            dh,
        "blks_read":           dr,
        "hit_rate":            round(hit_rate, 5),
        "mem_avail":           mem_after,
        "dtlb_miss_per_txn":   round(dtlb     / txns, 1),
        "l3_miss_per_txn":     round(l3m      / txns, 1),
        "l3_miss_pct":         l3_pct,
        "stall_be_per_txn":    round(stall_be / txns, 1),
        "stall_pct":           stall_pct,
        "page_faults_per_txn": round(pgflt    / txns, 3),
        "cycles_per_txn":      round(cyc      / txns, 0),
        "vmstat":              vmst,
        "bgwriter": {
            "chkpt_mb":       chkpt_mb,
            "clean_mb":       clean_mb,
            "backend_mb":     backend_mb,
            "n_checkpoints":  n_chkpts,
            "maxwritten_clean":      bw_delta["maxwritten_clean"],
            "buffers_backend_fsync": bw_delta["buffers_backend_fsync"],
        },
        "iostat":              iost,
        "wait_events":         wait_events[:5],
    }

# ── Model fitting ─────────────────────────────────────────────────────────────
def fit_penalty(results: list[dict]) -> dict:
    """
    Fit a piecewise-linear penalty model:
      tps(SB) = tps_base                           SB <= SB_safe
              = tps_base - slope * (SB - SB_safe)  SB >  SB_safe

    SB_safe = largest SB where tps > tps_base * 0.95 (5% tolerance).
    Also fits exponential decay for _mimo_simulate integration.
    """
    valid = [r for r in results if r.get("tps") is not None]
    if len(valid) < 2:
        return {}

    sb_vals  = [r["sb_mb"] for r in valid]
    tps_vals = [r["tps"]   for r in valid]
    tps_base = tps_vals[0]

    sb_safe = sb_vals[0]
    for r in valid:
        if r["tps"] >= tps_base * 0.95:
            sb_safe = r["sb_mb"]
        else:
            break

    safe_tps = next((r["tps"] for r in valid if r["sb_mb"] == sb_safe), tps_base)
    last     = valid[-1]
    if last["sb_mb"] > sb_safe and last["tps"] is not None:
        slope_tps_per_mb = (safe_tps - last["tps"]) / (last["sb_mb"] - sb_safe)
    else:
        slope_tps_per_mb = 0.0

    total_ram = 14700
    lam = 0.0
    if last["sb_mb"] > sb_safe and last["tps"] and last["tps"] > 0:
        ratio = last["tps"] / tps_base
        if 0 < ratio < 1.0:
            lam = -math.log(ratio) * total_ram / (last["sb_mb"] - sb_safe)

    log("\n── Penalty model ────────────────────────────────────────")
    log(f"  tps_base          = {tps_base:.1f} TPS  (at SB={sb_vals[0]}MB)")
    log(f"  SB_safe           = {sb_safe}MB  (≤5% TPS degradation)")
    log(f"  slope (linear)    = {slope_tps_per_mb*1024:.4f} TPS/GB")
    log(f"  lambda (exp)      = {lam:.6f}  "
        f"[tps(SB) = {tps_base:.0f} × exp(-{lam:.4f} × max(0, SB-{sb_safe}) / {total_ram})]")
    log(f"  → integrate into _mimo_simulate: penalise SB > {sb_safe}MB")
    log("─────────────────────────────────────────────────────────\n")

    return {
        "tps_base":         round(tps_base, 2),
        "sb_safe_mb":       sb_safe,
        "slope_tps_per_mb": round(slope_tps_per_mb, 6),
        "slope_tps_per_gb": round(slope_tps_per_mb * 1024, 4),
        "lambda_exp":       round(lam, 6),
        "total_ram_mb":     total_ram,
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

    log(f"\nRestoring SB to {BASE_SB_MB}MB ...")
    set_guc("shared_buffers", f"{BASE_SB_MB}MB")
    set_guc("enable_huge_pages", "off")
    restart_db()
    # Restore THP / hugepages to a low-impact state so subsequent experiments
    # are not skewed by lingering reservation.
    try:
        subprocess.run(["sudo", "tee", "/proc/sys/vm/nr_hugepages"],
                       input="0\n", capture_output=True, text=True, timeout=10)
        for knob, val in [("enabled", "madvise"), ("defrag", "madvise"),
                          ("shmem_enabled", "never")]:
            subprocess.run(["sudo", "tee", f"/sys/kernel/mm/transparent_hugepage/{knob}"],
                           input=f"{val}\n", capture_output=True, text=True, timeout=5)
        log("  [HP] Teardown: nr_hugepages=0, THP back to madvise/never")
    except Exception as e:
        log(f"  [HP] teardown error: {e}")
    log("Done.\n")

    model = fit_penalty(results)

    # ── Summary table ────────────────────────────────────────────────────────
    log("── Results ──────────────────────────────────────────────")
    hdr = (f"  {'SB(MB)':>8}  {'TPS':>6}  {'hit%':>5}  {'wa%':>5}  "
           f"{'chkpt_MB':>9}  {'clean_MB':>9}  {'backend_MB':>10}  "
           f"{'wkB/s':>7}  {'w_await':>7}  {'L3/txn':>8}")
    sep = (f"  {'─'*8}  {'─'*6}  {'─'*5}  {'─'*5}  "
           f"{'─'*9}  {'─'*9}  {'─'*10}  "
           f"{'─'*7}  {'─'*7}  {'─'*8}")
    log(hdr)
    log(sep)
    for r in results:
        if r.get("tps") is None:
            log(f"  {r['sb_mb']:>8}  {'ERROR':>6}")
            continue
        v  = r.get("vmstat",   {})
        bw = r.get("bgwriter", {})
        io = r.get("iostat",   {})
        log(f"  {r['sb_mb']:>8}  {r['tps']:>6.1f}  "
            f"{r['hit_rate']*100:>5.1f}  "
            f"{v.get('vmstat_wa_avg',0):>5.1f}  "
            f"{bw.get('chkpt_mb',   0):>9.1f}  "
            f"{bw.get('clean_mb',   0):>9.1f}  "
            f"{bw.get('backend_mb', 0):>10.1f}  "
            f"{io.get('wkb_s',      0):>7.1f}  "
            f"{io.get('w_await',    0):>7.1f}  "
            f"{r['l3_miss_per_txn']:>8.0f}")
    log("─────────────────────────────────────────────────────────")

    output = {
        "date":      datetime.now().isoformat(),
        "warmup_s":  WARMUP_S,
        "measure_s": MEASURE_S,
        "levels":    results,
        "model":     model,
    }
    with open(JSON_OUT, "w") as f:
        json.dump(output, f, indent=2)
    log(f"\nSaved: {JSON_OUT}")

if __name__ == "__main__":
    main()
