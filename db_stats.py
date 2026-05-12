#!/usr/bin/env python3
"""
db_stats.py — Correct helper for measuring GaussDB hit ratio and spill
over a time window, using delta snapshots (no pg_stat_reset needed).

Usage:
    from db_stats import DBStats
    s = DBStats()

    snap0 = s.snapshot()
    time.sleep(window_s)
    snap1 = s.snapshot()

    metrics = s.delta(snap0, snap1, window_s)
    print(metrics)

Or as a CLI:
    python3 db_stats.py --window 60
    python3 db_stats.py --window 60 --repeat 5   # measure 5 consecutive windows
"""

import os, re, subprocess, tempfile, time, argparse

GSQL       = ("/opt/openGauss/app/bin/gsql -U omm -p 5432 -d sbtest")
OMM_PASS   = "1997"
OMM_ENV    = ("export LD_LIBRARY_PATH=/opt/openGauss/app/lib; "
              "export GAUSSHOME=/opt/openGauss/app; "
              "export PATH=$GAUSSHOME/bin:$PATH; ")


def _omm(cmd: str, timeout: int = 30) -> tuple[str, str]:
    """Run a shell command as omm via su."""
    full_cmd = OMM_ENV + cmd
    r = subprocess.run(
        ["su", "-", "omm", "-c", full_cmd],
        input=OMM_PASS + "\n",
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout, r.stderr


def _gsql(sql: str, timeout: int = 15) -> str:
    """Execute SQL via gsql, connecting to sbtest. Returns stdout."""
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".sql", delete=False, dir="/tmp", prefix="dbstats_"
    )
    tmp.write(sql)
    tmp.flush()
    tmp_name = tmp.name
    tmp.close()
    os.chmod(tmp_name, 0o644)          # omm must be able to read this
    try:
        out, err = _omm(f"{GSQL} -f {tmp_name}", timeout=timeout)
    finally:
        os.unlink(tmp_name)
    return out


def _parse_row(out: str) -> list[str]:
    """
    Parse a single-row gsql result.
    Finds the '---+---' separator line, then takes the first data line.
    Returns list of stripped string values.
    """
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s*-+", line):   # separator: ---+---+--- (uses + not |)
            for data_line in lines[i + 1:]:
                data_line = data_line.strip()
                if data_line and not data_line.startswith("("):
                    return [v.strip() for v in data_line.split("|")]
    return []


class DBStats:
    """
    Snapshot-based measurement of GaussDB sbtest database stats.

    All measurements use the delta between two snapshots so they are
    unaffected by pg_stat_reset() calls elsewhere, and never need to
    issue a reset themselves.
    """

    # ── snapshot ─────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """
        Read current cumulative stats from pg_stat_database and
        pg_stat_bgwriter. Returns a dict with all raw counter values
        and a timestamp.
        """
        # pg_stat_database for sbtest
        out = _gsql(
            "SELECT blks_hit, blks_read, temp_files, temp_bytes, xact_commit "
            "FROM pg_stat_database WHERE datname='sbtest';"
        )
        row = _parse_row(out)
        if len(row) < 5:
            raise RuntimeError(
                f"pg_stat_database query returned unexpected output:\n{out!r}"
            )
        blks_hit    = int(row[0])
        blks_read   = int(row[1])
        temp_files  = int(row[2])
        temp_bytes  = int(row[3])
        xact_commit = int(row[4])

        return {
            "ts":          time.time(),
            "blks_hit":    blks_hit,
            "blks_read":   blks_read,
            "temp_files":  temp_files,
            "temp_bytes":  temp_bytes,
            "xact_commit": xact_commit,
        }

    # ── delta ─────────────────────────────────────────────────────────────────
    @staticmethod
    def delta(snap0: dict, snap1: dict, window_s: float | None = None) -> dict:
        """
        Compute per-window metrics from two snapshots.

        Returns:
            hit_ratio       fraction of buffer requests served from cache (0–1)
            blks_hit_delta  blocks served from cache during window
            blks_read_delta blocks read from disk during window
            hit_pct         hit_ratio × 100 for display
            spill_files     new temp files created (AP sort/hash spill)
            spill_bytes     new temp bytes written (AP spill volume, MB)
            tps             transactions per second (xact_commit delta / window)
            window_s        actual elapsed seconds between snapshots
        """
        elapsed = snap1["ts"] - snap0["ts"]
        if window_s is None:
            window_s = elapsed

        dh  = snap1["blks_hit"]    - snap0["blks_hit"]
        dr  = snap1["blks_read"]   - snap0["blks_read"]
        dtf = snap1["temp_files"]  - snap0["temp_files"]
        dtb = snap1["temp_bytes"]  - snap0["temp_bytes"]
        dxc = snap1["xact_commit"] - snap0["xact_commit"]

        total     = dh + dr
        hit_ratio = dh / total if total > 0 else 1.0

        return {
            "window_s":        round(elapsed, 1),
            "hit_ratio":       round(hit_ratio, 4),
            "hit_pct":         round(hit_ratio * 100, 2),
            "blks_hit_delta":  dh,
            "blks_read_delta": dr,
            "spill_files":     dtf,
            "spill_mb":        round(dtb / 1024 / 1024, 2),
            "tps":             round(dxc / elapsed, 1) if elapsed > 0 else 0.0,
        }

    # ── measure window ────────────────────────────────────────────────────────
    def measure(self, window_s: float) -> dict:
        """
        Take two snapshots window_s seconds apart and return delta metrics.
        Blocks for window_s seconds.
        """
        snap0 = self.snapshot()
        time.sleep(window_s)
        snap1 = self.snapshot()
        return self.delta(snap0, snap1, window_s)


# ── CLI ───────────────────────────────────────────────────────────────────────
def _fmt(m: dict) -> str:
    spill = f"{m['spill_mb']:.1f} MB ({m['spill_files']} files)" if m["spill_mb"] > 0 else "none"
    return (
        f"  window={m['window_s']:.0f}s  "
        f"TPS={m['tps']:.1f}  "
        f"hit%={m['hit_pct']:.1f}%  "
        f"(hit={m['blks_hit_delta']:,}  read={m['blks_read_delta']:,})  "
        f"spill={spill}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure GaussDB hit ratio and spill")
    parser.add_argument("--window", type=float, default=15.0,
                        help="Measurement window in seconds (default 15)")
    parser.add_argument("--repeat", type=int, default=1,
                        help="Number of consecutive windows to measure (default 1)")
    args = parser.parse_args()

    s = DBStats()
    print(f"[db_stats] window={args.window}s  repeat={args.repeat}")
    print(f"[db_stats] Verify connectivity: ", end="", flush=True)
    snap = s.snapshot()
    print(f"OK  (blks_hit={snap['blks_hit']:,}  blks_read={snap['blks_read']:,})")

    for i in range(args.repeat):
        ts = time.strftime("%H:%M:%S")
        m = s.measure(args.window)
        print(f"[{ts}] window {i+1}/{args.repeat}{_fmt(m)}")
