#!/usr/bin/env python3
"""
TLB Pressure Benchmark — GaussTune
Experiments:
  A. SB=2048MB, no huge pages, TP-only 60s + perf stat
  B. SB=6144MB, no huge pages, TP-only 60s + perf stat
  C. SB=2048MB, huge pages ON, repeat
  D. SB=6144MB, huge pages ON, repeat

Compare: TPS, blks_hit/read, hit_ratio, iowait%, dTLB-miss rate, cycles/txn.

Run:
  # Step 1 (once, as root):
  sudo sysctl -w kernel.perf_event_paranoid=1
  sudo bash -c "echo 3200 > /proc/sys/vm/nr_hugepages"    # for experiments C/D

  # Step 2:
  python3 tlb_bench.py
"""

import os, re, subprocess, sys, time, threading, tempfile, signal
from pathlib import Path

# ── constants ────────────────────────────────────────────────────────────────
GSQL        = ("LD_LIBRARY_PATH=/opt/openGauss/app/lib "
               "/opt/openGauss/app/bin/gsql -U omm -p 5432 -d postgres")
OMM_PASS    = "1997"
GS_CTL      = ("export GAUSSHOME=/opt/openGauss/app; "
               "export PATH=$GAUSSHOME/bin:$PATH; "
               "export LD_LIBRARY_PATH=$GAUSSHOME/lib; "
               "gs_ctl {cmd} -D /opt/openGauss/data")
SB_CMD      = ("LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu "
               "sysbench oltp_read_write "
               "--db-driver=pgsql --pgsql-host=/tmp --pgsql-port=5432 "
               "--pgsql-user=omm --pgsql-password= --pgsql-db=sbtest "
               "--tables=10 --table-size=10000000 "
               "--db-ps-mode=disable --threads=16 --rand-type=uniform "
               "--report-interval=5 --time={duration} run")
MEASURE_S   = 60     # measurement window per experiment
WARMUP_S    = 120    # warmup after each restart (scaled for 6144MB: 360s)
PG_CONF     = "/opt/openGauss/data/postgresql.conf"
HP_PATH     = "/proc/sys/vm/nr_hugepages"
PARANOID    = "/proc/sys/kernel/perf_event_paranoid"
THP_SHMEM   = "/sys/kernel/mm/transparent_hugepage/shmem_enabled"

# ── helpers ──────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def omm(cmd, timeout=300):
    r = subprocess.run(["su", "-", "omm", "-c", cmd],
                       input=OMM_PASS + "\n", capture_output=True, text=True,
                       timeout=timeout)
    return r.stdout, r.stderr

def gsql(sql, timeout=15):
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
        f.write(sql); name = f.name
    out, _ = omm(f"{GSQL} -f {name}", timeout=timeout)
    os.unlink(name)
    return out

def set_guc(param, value):
    gsql(f"ALTER SYSTEM SET {param}='{value}'; SELECT pg_reload_conf();")

def restart_db(timeout=120):
    omm(GS_CTL.format(cmd="stop"), timeout=30)
    time.sleep(2)
    omm(GS_CTL.format(cmd="start"), timeout=timeout)
    for _ in range(30):
        out, _ = omm(f"{GSQL} -c 'SELECT 1;'", timeout=5)
        if "1" in out:
            return
        time.sleep(1)
    raise RuntimeError("DB did not come up")

def read_db_stats():
    """Return (blks_hit, blks_read, xact_commit) from sbtest database."""
    out = gsql("SELECT blks_hit, blks_read, xact_commit "
               "FROM pg_stat_database WHERE datname='sbtest';")
    for line in out.splitlines():
        m = re.search(r"\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)", line)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return 0, 0, 0

def reset_stats():
    gsql("SELECT pg_stat_reset();")

def gaussdb_pid():
    out = subprocess.check_output(
        ["pgrep", "-f", "gaussdb.*-D /opt/openGauss/data"], text=True).strip()
    return int(out.split()[0]) if out else None

def iowait_pct(duration_s):
    """Measure iowait% over duration_s seconds from /proc/stat."""
    def read_stat():
        line = open("/proc/stat").readline()
        vals = list(map(int, line.split()[1:]))
        total = sum(vals)
        iowait = vals[4]
        return total, iowait
    t0, io0 = read_stat()
    time.sleep(duration_s)
    t1, io1 = read_stat()
    return 100.0 * (io1 - io0) / max(t1 - t0, 1)

# ── perf helpers ─────────────────────────────────────────────────────────────
PERF_AVAIL = None

def check_perf():
    global PERF_AVAIL
    try:
        paranoid = int(open(PARANOID).read())
    except:
        PERF_AVAIL = False
        return False
    if paranoid > 1:
        log(f"  ⚠  perf_event_paranoid={paranoid} — need ≤1 for cross-process profiling.")
        log(f"     Run: sudo sysctl -w kernel.perf_event_paranoid=1")
        PERF_AVAIL = False
        return False
    # Test via omm so it matches how actual gaussdb profiling will run
    try:
        _, stderr = omm("perf stat -e task-clock -- sleep 0.1", timeout=10)
        PERF_AVAIL = "task-clock" in stderr or "seconds time elapsed" in stderr
    except Exception as e:
        log(f"  ⚠  perf check failed: {e}")
        PERF_AVAIL = False
    if PERF_AVAIL:
        log(f"  perf available (paranoid={paranoid})")
    else:
        log(f"  ⚠  perf unavailable (install linux-tools-$(uname -r) or check omm PATH)")
    return PERF_AVAIL

def run_perf_stat(pid, duration_s):
    """
    Run perf stat on gaussdb PID (owned by omm) for duration_s seconds.
    Must run via omm() so the profiler and target share the same uid.
    Returns dict with counter values or {} if unavailable.
    """
    if not PERF_AVAIL:
        return {}
    events = ("dTLB-load-misses,dTLB-loads,"
              "iTLB-load-misses,iTLB-loads,"
              "cycles,instructions,"
              "cache-misses,cache-references")
    cmd_str = f"perf stat -e {events} -p {pid} -- sleep {duration_s}"
    _, stderr = omm(cmd_str, timeout=duration_s + 20)
    result = {}
    for line in stderr.splitlines():
        line = line.strip()
        # e.g.  123,456,789      dTLB-load-misses   or   <not supported>
        m = re.match(r"([\d,]+)\s+([\w\-]+)", line)
        if m:
            key = m.group(2)
            val = int(m.group(1).replace(",", ""))
            result[key] = val
    return result

# ── huge pages helpers ────────────────────────────────────────────────────────
def huge_pages_count():
    try:
        return int(open(HP_PATH).read())
    except:
        return 0

def enable_huge_pages(n=3200):
    """Enable huge pages — requires root. Returns True if successful."""
    try:
        open(HP_PATH, "w").write(str(n))
        actual = huge_pages_count()
        log(f"  Huge pages set to {actual} (requested {n})")
        return actual > 0
    except PermissionError:
        log(f"  ✗ Cannot write {HP_PATH} — need root.")
        log(f"    Run: sudo bash -c 'echo {n} > {HP_PATH}'")
        return False

def disable_huge_pages():
    """Disable both explicit huge pages and THP shmem. Returns True if THP was disabled."""
    try:
        open(HP_PATH, "w").write("0")
    except:
        pass
    # Also disable THP shmem so Phase 1 is a clean no-huge-pages baseline
    thp_off = set_thp_shmem("never")
    if not thp_off:
        log("  ⚠  THP shmem still active — Phase 1 baseline may use huge pages.")
        log("     Run: sudo bash -c 'echo never > /sys/kernel/mm/transparent_hugepage/shmem_enabled'")
    return thp_off

def set_pg_hugepages(mode="try"):
    """Set huge_pages in postgresql.conf (needs omm write access)."""
    try:
        conf = subprocess.check_output(
            ["sudo", "-u", "omm", "cat", PG_CONF], text=True)
        new_conf = re.sub(r"^#?huge_pages\s*=.*", f"huge_pages = {mode}",
                          conf, flags=re.MULTILINE)
        if "huge_pages" not in new_conf:
            new_conf += f"\nhuge_pages = {mode}\n"
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(new_conf); fname = f.name
        subprocess.run(["sudo", "-u", "omm", "tee", PG_CONF],
                       input=new_conf, text=True, capture_output=True)
        log(f"  Set huge_pages={mode} in postgresql.conf")
        return True
    except Exception as e:
        log(f"  ✗ Cannot set huge_pages in conf: {e}")
        return False

def set_thp_shmem(mode="always"):
    """Enable THP for shmem — alternative to huge_pages. Requires root."""
    try:
        open(THP_SHMEM, "w").write(mode)
        log(f"  THP shmem = {mode}")
        return True
    except PermissionError:
        log(f"  ✗ Cannot set THP shmem — need root.")
        log(f"    Run: sudo bash -c 'echo {mode} > {THP_SHMEM}'")
        return False

# ── core experiment ───────────────────────────────────────────────────────────
def run_experiment(label, sb_mb):
    """
    Run one experiment: set SB, restart, warmup, measure 60s.
    Returns metrics dict.
    """
    log(f"\n{'='*60}")
    log(f"EXPERIMENT: {label}  (SB={sb_mb}MB)")
    log(f"{'='*60}")

    # 1. Configure and restart
    log(f"  Setting shared_buffers={sb_mb}MB + restart...")
    set_guc("shared_buffers", f"{sb_mb}MB")
    set_guc("work_mem", "64MB")
    restart_db()

    # 2. Warmup
    warmup_s = min(420, max(120, int(120 * sb_mb / 2048)))
    log(f"  Warming up {warmup_s}s...")
    omm(SB_CMD.format(duration=warmup_s), timeout=warmup_s + 30)

    # 3. Reset stats and record baseline
    reset_stats()
    pid = gaussdb_pid()
    log(f"  gaussdb PID={pid}")

    # 4. Parallel: sysbench + perf stat + iowait
    log(f"  Measuring {MEASURE_S}s (sysbench + perf stat)...")
    tps_values = []
    perf_result = {}
    io_pct = [0.0]

    def run_sysbench():
        out, _ = omm(SB_CMD.format(duration=MEASURE_S), timeout=MEASURE_S + 15)
        for line in out.splitlines():
            m = re.search(r"thds:\s*\d+\s+tps:\s*([\d.]+)", line)
            if m:
                tps_values.append(float(m.group(1)))

    def run_perf():
        if pid and PERF_AVAIL:
            perf_result.update(run_perf_stat(pid, MEASURE_S))

    def run_iowait():
        # Read iowait over measurement window
        def snap():
            vals = list(map(int, open("/proc/stat").readline().split()[1:]))
            return sum(vals), vals[4]
        t0, io0 = snap()
        time.sleep(MEASURE_S)
        t1, io1 = snap()
        dt = max(1, t1 - t0)
        io_pct[0] = 100.0 * (io1 - io0) / dt

    threads = [
        threading.Thread(target=run_sysbench, daemon=True),
        threading.Thread(target=run_perf,     daemon=True),
        threading.Thread(target=run_iowait,   daemon=True),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=MEASURE_S + 30)

    # 5. Read pg_stat_database delta
    bh, br, xc = read_db_stats()

    # 6. Compute metrics
    avg_tps   = sum(tps_values) / len(tps_values) if tps_values else 0.0
    hit_ratio = bh / max(bh + br, 1) * 100

    dtlb_miss  = perf_result.get("dTLB-load-misses", None)
    dtlb_loads = perf_result.get("dTLB-loads", None)
    cycles     = perf_result.get("cycles", None)
    instrs     = perf_result.get("instructions", None)
    cache_miss = perf_result.get("cache-misses", None)
    cache_ref  = perf_result.get("cache-references", None)

    txns_total = avg_tps * MEASURE_S
    dtlb_miss_rate  = (dtlb_miss  / max(dtlb_loads, 1) * 100) if dtlb_miss  is not None else None
    cycles_per_txn  = (cycles / max(txns_total, 1))            if cycles     is not None else None
    instrs_per_txn  = (instrs / max(txns_total, 1))            if instrs     is not None else None
    cache_miss_rate = (cache_miss / max(cache_ref, 1) * 100)   if cache_miss is not None else None

    m = {
        "label":           label,
        "sb_mb":           sb_mb,
        "avg_tps":         avg_tps,
        "blks_hit":        bh,
        "blks_read":       br,
        "hit_ratio_pct":   hit_ratio,
        "iowait_pct":      io_pct[0],
        "dtlb_miss":       dtlb_miss,
        "dtlb_loads":      dtlb_loads,
        "dtlb_miss_rate":  dtlb_miss_rate,
        "cycles":          cycles,
        "instructions":    instrs,
        "cycles_per_txn":  cycles_per_txn,
        "instrs_per_txn":  instrs_per_txn,
        "cache_miss":      cache_miss,
        "cache_miss_rate": cache_miss_rate,
    }

    # 7. Print per-experiment summary
    log(f"  TPS={avg_tps:.1f}  blks_hit={bh:,}  blks_read={br:,}  "
        f"hit%={hit_ratio:.1f}%  iowait%={io_pct[0]:.1f}%")
    if dtlb_miss_rate is not None:
        log(f"  dTLB-miss%={dtlb_miss_rate:.2f}%  "
            f"cycles/txn={cycles_per_txn:,.0f}  "
            f"L3-miss%={cache_miss_rate:.2f}%")
    else:
        log(f"  (perf counters unavailable — run sudo sysctl -w kernel.perf_event_paranoid=1)")

    return m

# ── report ────────────────────────────────────────────────────────────────────
def print_report(results):
    sep = "─" * 90
    print(f"\n{sep}")
    print("TLB Pressure Benchmark — Summary")
    print(sep)
    hdr = (f"{'Label':<35} {'SB':>6} {'TPS':>8} {'hit%':>7} "
           f"{'iowait%':>8} {'dTLB-miss%':>11} {'cyc/txn':>12} {'L3-miss%':>9}")
    print(hdr)
    print("─" * 90)
    for m in results:
        def fmt(v, fmt_str):
            return format(v, fmt_str) if v is not None else "  N/A "
        print(f"{m['label']:<35} "
              f"{m['sb_mb']:>6} "
              f"{m['avg_tps']:>8.1f} "
              f"{m['hit_ratio_pct']:>7.1f} "
              f"{m['iowait_pct']:>8.2f} "
              f"{fmt(m['dtlb_miss_rate'],  '>11.2f')} "
              f"{fmt(m['cycles_per_txn'],  '>12,.0f')} "
              f"{fmt(m['cache_miss_rate'], '>9.2f')}")
    print(sep)

    # ratio analysis
    if len(results) >= 2:
        r0, r1 = results[0], results[1]   # first two are no-hugepages pair
        print(f"\nRatio analysis (SB=6144 / SB=2048, no huge pages):")
        print(f"  TPS ratio:        {r1['avg_tps']  / max(r0['avg_tps'],  1):.3f}  "
              f"(model predicts {2048/6144:.3f})")
        if r0['dtlb_miss_rate'] and r1['dtlb_miss_rate']:
            print(f"  dTLB-miss% ratio: {r1['dtlb_miss_rate'] / max(r0['dtlb_miss_rate'], 1e-9):.3f}")
        if r0['cycles_per_txn'] and r1['cycles_per_txn']:
            print(f"  cycles/txn ratio: {r1['cycles_per_txn'] / max(r0['cycles_per_txn'], 1):.3f}")

    if len(results) >= 4:
        r0, r1, r2, r3 = results   # (2048 no-hp, 6144 no-hp, 2048 hp, 6144 hp)
        print(f"\nHuge pages effect on SB=6144:")
        print(f"  TPS:       {r1['avg_tps']:.1f} → {r3['avg_tps']:.1f}  "
              f"({r3['avg_tps']/max(r1['avg_tps'],1)*100:.0f}%)")
        if r1['dtlb_miss_rate'] is not None and r3['dtlb_miss_rate'] is not None:
            print(f"  dTLB-miss%: {r1['dtlb_miss_rate']:.2f}% → {r3['dtlb_miss_rate']:.2f}%")
        if r1['cycles_per_txn'] and r3['cycles_per_txn']:
            print(f"  cycles/txn: {r1['cycles_per_txn']:,.0f} → {r3['cycles_per_txn']:,.0f}")

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    log("TLB Pressure Benchmark starting")
    log(f"perf_event_paranoid = {open(PARANOID).read().strip()}")
    log(f"nr_hugepages        = {huge_pages_count()}")
    log(f"THP shmem           = {open(THP_SHMEM).read().strip()}")

    # Check perf availability
    perf_ok = check_perf()
    if not perf_ok:
        log("\n  To enable perf counters, run BEFORE this script:")
        log("    sudo sysctl -w kernel.perf_event_paranoid=1")
        log("  Continuing without perf (TPS + cache stats only)...\n")

    results = []

    # ── Phase 1: no huge pages ────────────────────────────────────────────────
    log("\n>>> Phase 1: no huge pages  (disabling THP shmem + explicit hugepages)")
    thp_was_on = open(THP_SHMEM).read().strip().startswith("[always]")
    needs_restart = False

    thp_off = disable_huge_pages()   # tries to set shmem_enabled=never
    if thp_off or not thp_was_on:
        set_pg_hugepages("off")
        needs_restart = True
    if needs_restart:
        restart_db()

    results.append(run_experiment("SB=2048MB  no-hugepages", sb_mb=2048))
    results.append(run_experiment("SB=6144MB  no-hugepages", sb_mb=6144))

    # ── Phase 2: huge pages ON ────────────────────────────────────────────────
    log("\n>>> Phase 2: huge pages ON  (2MB pages, nr_hugepages=3200)")

    hp_ok = enable_huge_pages(3200)
    thp_ok = set_thp_shmem("always")

    if not hp_ok and not thp_ok:
        log("\n  SKIP Phase 2: huge pages could not be enabled.")
        log("  To enable, run as root:")
        log("    sudo bash -c 'echo 3200 > /proc/sys/vm/nr_hugepages'")
        log("    sudo bash -c \"echo always > "
            "/sys/kernel/mm/transparent_hugepage/shmem_enabled\"")
        log("  Then re-run:  python3 tlb_bench.py --phase2-only")
    else:
        set_pg_hugepages("try")
        restart_db()
        results.append(run_experiment("SB=2048MB  huge-pages=try", sb_mb=2048))
        results.append(run_experiment("SB=6144MB  huge-pages=try", sb_mb=6144))

    # ── Report ────────────────────────────────────────────────────────────────
    print_report(results)

    # Save to file
    import json
    out_path = Path(__file__).parent / "run-logs" / "tlb_bench_results.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    log(f"\nResults saved to {out_path}")

    # Restore safe state
    log("\nRestoring: SB=2048MB, huge_pages=off, restart DB...")
    disable_huge_pages()
    set_pg_hugepages("off")
    set_guc("shared_buffers", "2048MB")
    restart_db()
    log("Done.")


if __name__ == "__main__":
    # --phase2-only: skip phase 1, only run huge-pages experiments
    if "--phase2-only" in sys.argv:
        check_perf()
        results = []
        hp_ok  = enable_huge_pages(3200)
        thp_ok = set_thp_shmem("always")
        if hp_ok or thp_ok:
            set_pg_hugepages("try")
            restart_db()
            results.append(run_experiment("SB=2048MB  huge-pages=try", sb_mb=2048))
            results.append(run_experiment("SB=6144MB  huge-pages=try", sb_mb=6144))
            print_report(results)
        else:
            log("Cannot enable huge pages without root.")
        sys.exit(0)
    main()
