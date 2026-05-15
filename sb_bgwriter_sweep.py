#!/usr/bin/env python3
"""
SB + Bgwriter Joint Sweep
=========================
Tests TPS vs SB at 3 checkpoint/bgwriter configs to determine
whether bgwriter_delay / checkpoint_completion_target cause the
SB penalty observed in sb_calib6.

Config design (one variable at a time):
  baseline    bgwriter_delay=2000  target=0.5  lru_maxpages=100   (current)
  bgw_fast    bgwriter_delay=200   target=0.5  lru_maxpages=100   (bgwriter alone)
  optimized   bgwriter_delay=200   target=0.9  lru_maxpages=200   (full tune)

SB levels: [1024, 2048, 3072, 4096] — covers the penalty onset region.

Total est: 3 configs × 4 SB × ~330s ≈ 66 min.
"""

import subprocess, time, re, os, json, threading
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
GSQL     = "/opt/openGauss/app/bin/gsql"
OMM_PASS = "1997"
LOG_PATH = "/home/node/GaussTune/run-logs/sb_bgwriter_sweep.log"
JSON_OUT = "/home/node/GaussTune/run-logs/sb_bgwriter_sweep.json"

WARMUP_S  = 180
MEASURE_S = 60

SB_LEVELS = [1024, 2048, 3072, 4096]

CONFIGS = [
    {
        "name":                         "baseline",
        "bgwriter_delay":               2000,
        "checkpoint_completion_target": "0.5",
        "bgwriter_lru_maxpages":        100,
    },
    {
        "name":                         "bgw_fast",
        "bgwriter_delay":               200,
        "checkpoint_completion_target": "0.5",
        "bgwriter_lru_maxpages":        100,
    },
    {
        "name":                         "optimized",
        "bgwriter_delay":               200,
        "checkpoint_completion_target": "0.9",
        "bgwriter_lru_maxpages":        200,
    },
]

# Restore to these values after sweep
RESTORE = {
    "bgwriter_delay":               2000,
    "checkpoint_completion_target": "0.5",
    "bgwriter_lru_maxpages":        100,
    "shared_buffers":               "1024MB",
}

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

# ── Helpers (same as sb_calib.py) ────────────────────────────────────────────
def omm_run(cmd, timeout=60):
    r = subprocess.run(["su", "-", "omm", "-c", cmd],
        input=OMM_PASS + "\n", capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr

def gsql_q(sql, db="postgres", timeout=30):
    tmp = "/tmp/sbsweep_q.sql"
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
    except Exception:
        pass
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
    tmp = "/tmp/sbsweep_stats.sql"
    with open(tmp, "w") as f:
        f.write("SELECT blks_hit, blks_read FROM pg_stat_database WHERE datname='sbtest';")
    os.chmod(tmp, 0o666)
    out, _ = omm_run(f"{GSQL} -d postgres -f {tmp}", timeout=15)
    row = parse_row(out)
    if len(row) >= 2:
        return int(row[0]), int(row[1])
    return 0, 0

def get_bgwriter_stats() -> dict:
    sql = ("SELECT buffers_checkpoint, buffers_clean, buffers_backend, "
           "checkpoints_timed, checkpoints_req "
           "FROM pg_stat_bgwriter;")
    tmp = "/tmp/sbsweep_bgw.sql"
    with open(tmp, "w") as f:
        f.write(sql)
    os.chmod(tmp, 0o666)
    out, _ = omm_run(f"{GSQL} -d postgres -f {tmp}", timeout=15)
    row = parse_row(out)
    keys = ["buffers_checkpoint", "buffers_clean", "buffers_backend",
            "checkpoints_timed", "checkpoints_req"]
    result = {}
    for i, k in enumerate(keys):
        try:
            result[k] = int(row[i]) if i < len(row) else 0
        except (ValueError, IndexError):
            result[k] = 0
    return result

def run_vmstat(duration_s: int, out_file: str):
    try:
        r = subprocess.run(f"vmstat 1 {duration_s}", shell=True,
                           capture_output=True, text=True, timeout=duration_s + 10)
        with open(out_file, "w") as f:
            f.write(r.stdout)
    except Exception:
        pass

def parse_vmstat(out_file: str) -> dict:
    b_list, wa_list = [], []
    try:
        with open(out_file) as f:
            lines = f.readlines()
        for line in lines[2:]:
            parts = line.split()
            if len(parts) < 16:
                continue
            try:
                b_list.append(int(parts[1]))
                wa_list.append(int(parts[15]))
            except ValueError:
                continue
    except Exception:
        pass
    def avg(lst): return round(sum(lst) / len(lst), 1) if lst else 0
    return {"b": avg(b_list), "wa": avg(wa_list)}

def _data_device() -> str:
    try:
        out = subprocess.check_output(["df", "/opt/openGauss/data"], text=True)
        raw = out.split("\n")[1].split()[0]
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
    except Exception:
        pass

def parse_iostat(out_file: str) -> dict:
    wkb_list, w_await_list = [], []
    try:
        with open(out_file) as f:
            lines = f.readlines()
        col_map: dict = {}
        for line in lines:
            line = line.rstrip()
            if re.match(r"Device", line):
                col_map = {c: i for i, c in enumerate(line.split())}
                continue
            if not col_map or not line.strip():
                continue
            if re.match(r"Linux|avg-cpu", line):
                continue
            parts = line.split()
            try:
                wkb_idx  = col_map.get("wkB/s",  col_map.get("kB_wrtn/s", None))
                w_aw_idx = col_map.get("w_await", col_map.get("await",     None))
                if wkb_idx  is not None and wkb_idx  < len(parts):
                    wkb_list.append(float(parts[wkb_idx]))
                if w_aw_idx is not None and w_aw_idx < len(parts):
                    w_await_list.append(float(parts[w_aw_idx]))
            except (ValueError, IndexError):
                pass
    except Exception:
        pass
    def avg(lst): return round(sum(lst) / len(lst), 1) if lst else 0.0
    return {"wkb_s": avg(wkb_list), "w_await": avg(w_await_list)}

def parse_sysbench_tps(output: str) -> float:
    for line in reversed(output.split("\n")):
        m = re.search(r"transactions:\s+\d+\s+\((\d+\.\d+)\s+per sec", line)
        if m:
            return float(m.group(1))
    samples = []
    for line in output.split("\n"):
        m = re.search(r"\[\s*\d+s\s*\].*tps:\s*([\d.]+)", line)
        if m:
            samples.append(float(m.group(1)))
    return sum(samples) / len(samples) if samples else 0.0

# ── Single measurement ────────────────────────────────────────────────────────
def measure_one(cfg: dict, sb_mb: int) -> dict:
    log(f"\n  ── {cfg['name']} / SB={sb_mb}MB ──────────────────────────")

    compact_memory()

    # Apply all GUCs then restart (shared_buffers needs restart anyway)
    set_guc("shared_buffers",               f"{sb_mb}MB")
    set_guc("bgwriter_delay",               cfg["bgwriter_delay"])
    set_guc("checkpoint_completion_target", cfg["checkpoint_completion_target"])
    set_guc("bgwriter_lru_maxpages",        cfg["bgwriter_lru_maxpages"])

    ok = restart_db()
    if not ok:
        log(f"  ERROR: restart failed")
        return {"config": cfg["name"], "sb_mb": sb_mb, "tps": None}

    log(f"  Warmup {WARMUP_S}s ...")
    gsql_q("SELECT pg_stat_reset();")
    omm_run(SB_CMD.format(duration=WARMUP_S), timeout=WARMUP_S + 30)

    log(f"  Measuring {MEASURE_S}s ...")
    vmstat_out = f"/tmp/sbsweep_vmstat_{cfg['name']}_{sb_mb}.txt"
    iostat_out = f"/tmp/sbsweep_iostat_{cfg['name']}_{sb_mb}.txt"

    hit0, read0 = get_db_stats()
    bw0 = get_bgwriter_stats()

    vmstat_thread = threading.Thread(
        target=run_vmstat, args=(MEASURE_S, vmstat_out), daemon=True)
    iostat_thread = threading.Thread(
        target=run_iostat, args=(MEASURE_S, iostat_out), daemon=True)
    vmstat_thread.start()
    iostat_thread.start()

    out, _ = omm_run(SB_CMD.format(duration=MEASURE_S), timeout=MEASURE_S + 30)
    vmstat_thread.join(timeout=MEASURE_S + 15)
    iostat_thread.join(timeout=MEASURE_S + 15)

    hit1, read1 = get_db_stats()
    bw1 = get_bgwriter_stats()
    bw_delta = {k: bw1.get(k, 0) - bw0.get(k, 0) for k in bw0}

    tps  = parse_sysbench_tps(out)
    dh   = hit1 - hit0
    dr   = read1 - read0
    total = dh + dr
    hit_rate = dh / total if total > 0 else 1.0
    vmst = parse_vmstat(vmstat_out)
    iost = parse_iostat(iostat_out)

    chkpt_mb   = round(bw_delta["buffers_checkpoint"] * 8 / 1024, 1)
    clean_mb   = round(bw_delta["buffers_clean"]       * 8 / 1024, 1)
    backend_mb = round(bw_delta["buffers_backend"]     * 8 / 1024, 1)
    n_chkpts   = bw_delta["checkpoints_timed"] + bw_delta["checkpoints_req"]

    log(f"  TPS={tps:.1f}  hit%={hit_rate*100:.1f}  wa%={vmst['wa']}  "
        f"chkpt={chkpt_mb}MB  clean={clean_mb}MB  backend={backend_mb}MB  "
        f"n_ckpt={n_chkpts}  wkB/s={iost['wkb_s']}  w_await={iost['w_await']}ms")

    return {
        "config":      cfg["name"],
        "sb_mb":       sb_mb,
        "tps":         round(tps, 2),
        "hit_rate":    round(hit_rate, 4),
        "vmstat_wa":   vmst["wa"],
        "vmstat_b":    vmst["b"],
        "chkpt_mb":    chkpt_mb,
        "clean_mb":    clean_mb,
        "backend_mb":  backend_mb,
        "n_checkpoints": n_chkpts,
        "wkb_s":       iost["wkb_s"],
        "w_await_ms":  iost["w_await"],
    }

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    n_total = len(CONFIGS) * len(SB_LEVELS)
    est_min = n_total * (WARMUP_S + MEASURE_S + 90) // 60
    log("SB + Bgwriter Joint Sweep")
    log(f"  Configs   : {[c['name'] for c in CONFIGS]}")
    log(f"  SB levels : {SB_LEVELS} MB")
    log(f"  Warmup    : {WARMUP_S}s / Measure: {MEASURE_S}s")
    log(f"  Total est : {est_min} min  ({n_total} combinations)\n")

    all_results = []
    for cfg in CONFIGS:
        log(f"\n{'='*60}")
        log(f"  Config: {cfg['name']}")
        log(f"    bgwriter_delay={cfg['bgwriter_delay']}ms  "
            f"completion_target={cfg['checkpoint_completion_target']}  "
            f"lru_maxpages={cfg['bgwriter_lru_maxpages']}")
        log(f"{'='*60}")
        for sb in SB_LEVELS:
            r = measure_one(cfg, sb)
            all_results.append(r)

    # Restore original settings
    log(f"\nRestoring original settings ...")
    for k, v in RESTORE.items():
        set_guc(k, v)
    restart_db()
    log("Done.\n")

    # ── Summary table: TPS by (SB, config) ───────────────────────────────────
    log("── TPS Summary ──────────────────────────────────────────")
    cfg_names = [c["name"] for c in CONFIGS]
    hdr = f"  {'SB(MB)':>8}" + "".join(f"  {n:>12}" for n in cfg_names)
    log(hdr)
    log(f"  {'─'*8}" + "".join(f"  {'─'*12}" for _ in cfg_names))
    for sb in SB_LEVELS:
        row = f"  {sb:>8}"
        for cname in cfg_names:
            r = next((x for x in all_results
                      if x["config"] == cname and x["sb_mb"] == sb), None)
            tps_str = f"{r['tps']:>8.1f}" if r and r["tps"] else "   ERROR"
            row += f"  {tps_str:>12}"
        log(row)
    log("─────────────────────────────────────────────────────────")

    # ── Backend write summary ─────────────────────────────────────────────────
    log("\n── Backend-forced write (MB/60s) ────────────────────────")
    log(hdr)
    log(f"  {'─'*8}" + "".join(f"  {'─'*12}" for _ in cfg_names))
    for sb in SB_LEVELS:
        row = f"  {sb:>8}"
        for cname in cfg_names:
            r = next((x for x in all_results
                      if x["config"] == cname and x["sb_mb"] == sb), None)
            val = f"{r['backend_mb']:>8.1f}" if r and r["tps"] else "   ERROR"
            row += f"  {val:>12}"
        log(row)
    log("─────────────────────────────────────────────────────────")

    # ── wa% summary ───────────────────────────────────────────────────────────
    log("\n── iowait % ─────────────────────────────────────────────")
    log(hdr)
    log(f"  {'─'*8}" + "".join(f"  {'─'*12}" for _ in cfg_names))
    for sb in SB_LEVELS:
        row = f"  {sb:>8}"
        for cname in cfg_names:
            r = next((x for x in all_results
                      if x["config"] == cname and x["sb_mb"] == sb), None)
            val = f"{r['vmstat_wa']:>8.1f}" if r and r["tps"] else "   ERROR"
            row += f"  {val:>12}"
        log(row)
    log("─────────────────────────────────────────────────────────")

    output = {
        "date":      datetime.now().isoformat(),
        "configs":   CONFIGS,
        "sb_levels": SB_LEVELS,
        "results":   all_results,
    }
    with open(JSON_OUT, "w") as f:
        json.dump(output, f, indent=2)
    log(f"\nSaved: {JSON_OUT}")

if __name__ == "__main__":
    main()
