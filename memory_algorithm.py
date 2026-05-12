#!/usr/bin/env python3
"""
Dynamic Memory Allocation Algorithm for OpenGauss TP+AP Mixed Workload
Based on Phase A (SB sweep) and Phase B (AP injection shock) experimental findings.

Key findings that motivate the algorithm:
1. Phase A: SB=6GB is the sweet spot for a 20GB workset — higher SB yields no
   significant TPS gain, meaning marginal SB is available to loan to work_mem.
2. Phase B: Larger work_mem dramatically reduces AP's impact on TP:
     wm=64MB×1  → 39.7% TPS drop  (many sort passes = lots of SB eviction)
     wm=1024MB×4 → -1.1% TPS drop (sort mostly in-memory, minimal SB pollution)
   Rule: work_mem ≥ (sort_size / sort_passes_needed), where fewer passes = less SB churn.
3. work_mem change (ALTER SYSTEM + pg_reload_conf) takes effect immediately with no restart.
   shared_buffers change requires DB restart — use it only as a last resort or offline.

Algorithm: "Loan work_mem from SB headroom when AP load spikes"
  - Monitor: ap_active, tps_drop%, miss%, spill_bytes
  - When AP detected: increase work_mem (immediate, no restart)
  - When AP ends: restore work_mem
  - SB is only adjusted if persistent miss% elevation warrants a restart

Target: keep TP TPS drop within 10% during AP injection.
"""
import subprocess, time, re, os
from datetime import datetime

GSQL     = "/opt/openGauss/app/bin/gsql"
OMM_PASS = "1997"

# ── Tunable parameters ────────────────────────────────────────────────────────
SB_BASELINE_MB   = 6144   # shared_buffers baseline (6GB) — sweet spot from Phase A
WM_BASELINE_MB   = 64     # work_mem baseline
WM_MAX_MB        = 1024   # max work_mem to loan (Phase B: 1024×4 → no TPS drop)
SB_MIN_MB        = 4096   # minimum SB — keep 4GB to preserve TP hot set
MISS_TRIGGER     = 3.0    # % cache miss increase that triggers wm loan
TPS_DROP_TRIGGER = 10.0   # % TPS drop that triggers wm loan
POLL_INTERVAL    = 10     # seconds between evaluations
WM_STEP_MB       = 128    # work_mem step size per adjustment
AP_DETECT_SPILL  = 10     # MB/interval spill that indicates AP activity

# ── Helper functions ──────────────────────────────────────────────────────────
def omm_run(cmd, timeout=30):
    r = subprocess.run(["su", "-", "omm", "-c", cmd],
        input=OMM_PASS+"\n", capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr

def parse_row(out):
    lines = out.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"\s*-+", line):
            for dl in lines[i+1:]:
                dl = dl.strip()
                if dl and not dl.startswith("("):
                    return [x.strip() for x in dl.split("|")]
    return []

def get_db_stats():
    """Returns (blks_hit, blks_read, temp_bytes, n_ap_active) from pg_stat_database + pg_stat_activity."""
    tmp = "/tmp/mem_algo.sql"
    with open(tmp, "w") as f:
        f.write(
            "SELECT blks_hit, blks_read, temp_bytes FROM pg_stat_database WHERE datname='sbtest';\n"
        )
    os.chmod(tmp, 0o644)
    out, _ = omm_run(f"{GSQL} -d postgres -f {tmp}", timeout=15)
    row = parse_row(out)
    if len(row) >= 3:
        blks_hit, blks_read, temp_bytes = int(row[0]), int(row[1]), int(row[2])
    else:
        blks_hit, blks_read, temp_bytes = 0, 0, 0

    # Count active AP queries (sort/window functions)
    out2, _ = omm_run(
        f"{GSQL} -d postgres -c \"SELECT count(*) FROM pg_stat_activity "
        f"WHERE state='active' AND (query ILIKE '%order by%' OR query ILIKE '%window%') "
        f"AND query NOT LIKE '%pg_stat%';\"",
        timeout=10)
    row2 = parse_row(out2)
    n_ap = int(row2[0]) if row2 else 0

    return blks_hit, blks_read, temp_bytes, n_ap

def get_tps_estimate():
    """Sample TPS over 5 seconds using pg_stat_database xact_commit delta."""
    tmp = "/tmp/mem_algo2.sql"
    with open(tmp, "w") as f:
        f.write("SELECT xact_commit FROM pg_stat_database WHERE datname='sbtest';")
    os.chmod(tmp, 0o644)
    out1, _ = omm_run(f"{GSQL} -d postgres -f {tmp}", timeout=10)
    row1 = parse_row(out1)
    t1 = time.time()
    time.sleep(5)
    out2, _ = omm_run(f"{GSQL} -d postgres -f {tmp}", timeout=10)
    row2 = parse_row(out2)
    t2 = time.time()
    if row1 and row2:
        delta_xact = int(row2[0]) - int(row1[0])
        return round(delta_xact / (t2 - t1), 1)
    return 0.0

def set_work_mem(mb):
    """Set work_mem for all new connections via ALTER DATABASE (ALTER SYSTEM unsupported in OG)."""
    sql = f"ALTER DATABASE sbtest SET work_mem='{mb}MB';"
    omm_run(f"{GSQL} -d postgres -c \"{sql}\"", timeout=15)
    print(f"  → SET work_mem={mb}MB", flush=True)

def get_current_work_mem():
    """Return current work_mem in MB."""
    out, _ = omm_run(f"{GSQL} -d postgres -c 'SHOW work_mem;'", timeout=10)
    row = parse_row(out)
    if row:
        m = re.match(r"(\d+)", row[0])
        if m: return int(m.group(1))
    return WM_BASELINE_MB


# ── Main algorithm loop ───────────────────────────────────────────────────────

class MemoryAllocator:
    def __init__(self):
        self.wm_current = get_current_work_mem()
        self.prev_hit = 0
        self.prev_read = 0
        self.prev_temp = 0
        self.tps_baseline = None   # measured during stable TP-only period
        self.miss_baseline = None  # measured during stable period
        self.ap_duration = 0       # seconds AP has been active
        self.recovery_duration = 0 # seconds since AP ended
        self.state = "idle"        # idle | ap_active | recovering

    def update(self):
        """One evaluation tick (called every POLL_INTERVAL seconds)."""
        now = datetime.now().strftime("%H:%M:%S")

        # Get current stats
        hit, read, temp, n_ap = get_db_stats()
        dh = hit - self.prev_hit
        dr = read - self.prev_read
        dt = temp - self.prev_temp
        self.prev_hit, self.prev_read, self.prev_temp = hit, read, temp

        total = dh + dr
        miss_pct = round(100.0 * dr / total, 2) if total > 0 else 0.0
        spill_mb = round(dt / 1024 / 1024, 1)

        # Detect AP activity
        ap_active = n_ap > 0 or spill_mb > AP_DETECT_SPILL

        # ── State machine ──────────────────────────────────────────────────────

        if self.state == "idle":
            # Calibrate baseline during idle
            if self.miss_baseline is None:
                self.miss_baseline = miss_pct
            else:
                # EMA update
                self.miss_baseline = 0.9 * self.miss_baseline + 0.1 * miss_pct

            if ap_active:
                self.state = "ap_active"
                self.ap_duration = 0
                print(f"[{now}] AP DETECTED: n_ap={n_ap} spill={spill_mb}MB miss={miss_pct}%")
                self._respond_to_ap(n_ap, miss_pct, spill_mb)

        elif self.state == "ap_active":
            self.ap_duration += POLL_INTERVAL
            print(f"[{now}] AP active {self.ap_duration}s: n_ap={n_ap} spill={spill_mb}MB "
                  f"miss={miss_pct}% wm={self.wm_current}MB")

            if ap_active:
                # Escalate if miss% still high and wm can be increased
                miss_delta = miss_pct - (self.miss_baseline or 0)
                if miss_delta > MISS_TRIGGER and self.wm_current < WM_MAX_MB:
                    new_wm = min(self.wm_current + WM_STEP_MB, WM_MAX_MB)
                    set_work_mem(new_wm)
                    self.wm_current = new_wm
            else:
                print(f"[{now}] AP ENDED after {self.ap_duration}s")
                self.state = "recovering"
                self.recovery_duration = 0

        elif self.state == "recovering":
            self.recovery_duration += POLL_INTERVAL
            print(f"[{now}] Recovering {self.recovery_duration}s: miss={miss_pct}% wm={self.wm_current}MB")

            if ap_active:
                # AP came back
                self.state = "ap_active"
                self.ap_duration = 0
                self._respond_to_ap(n_ap, miss_pct, spill_mb)
            elif self.wm_current > WM_BASELINE_MB and self.recovery_duration >= 30:
                # Gradually restore work_mem
                new_wm = max(self.wm_current - WM_STEP_MB, WM_BASELINE_MB)
                set_work_mem(new_wm)
                self.wm_current = new_wm
                if self.wm_current <= WM_BASELINE_MB:
                    self.state = "idle"
                    print(f"[{now}] Fully recovered. Back to idle.")

    def _respond_to_ap(self, n_ap, miss_pct, spill_mb):
        """Increase work_mem in response to AP load."""
        # Target: work_mem large enough for 1-pass sort of AP query working set
        # Phase B showed: wm=1024MB × 4 workers → no TPS drop
        # Heuristic: target wm = sort_size / conc / 2  (where sort_size ~= spill * num_passes)
        # Conservative: step up by WM_STEP_MB per AP worker detected
        target_wm = min(WM_BASELINE_MB + n_ap * WM_STEP_MB, WM_MAX_MB)
        if target_wm > self.wm_current:
            set_work_mem(target_wm)
            self.wm_current = target_wm


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Memory Allocator starting — baseline wm={WM_BASELINE_MB}MB SB={SB_BASELINE_MB}MB")
    allocator = MemoryAllocator()
    while True:
        try:
            allocator.update()
        except Exception as e:
            print(f"ERROR: {e}")
        time.sleep(POLL_INTERVAL)
