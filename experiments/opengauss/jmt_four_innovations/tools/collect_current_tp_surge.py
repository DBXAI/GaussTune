#!/usr/bin/env python3
"""S5 TP-only calibration: warm 128, then activate preconnected +16 at window.

Never substitutes a flat 144-terminal baseline for the measurement-phase surge.
The temporary Lua wrapper changes only first-event scheduling; the read-only
transaction body remains the installed original script.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.common.db import Gsql
from jmt.common.machine import collect_machine
from jmt.innovation_4_performance.tp_window_observer import TPWindowObserver
from collect_current_tp_matrix import (
    OnlineSB, dump, digest, sleep_until, native_snapshot, cpu_snapshot, summarize_cpu,
    CPUWindowBoundaryError, validate_cpu_window,
)

GATED_LUA = """
package.path = "/usr/share/sysbench/?.lua;" .. package.path
dofile("/usr/share/sysbench/oltp_read_only.lua")
local ffi = require("ffi")
ffi.cdef[[
  struct jmt_timespec { long tv_sec; long tv_nsec; };
  int clock_gettime(int clockid, struct jmt_timespec *tp);
  int usleep(unsigned int usec);
]]
local original_event = event
local gate_ns = tonumber(os.getenv("JMT_SURGE_MONOTONIC_NS"))
assert(gate_ns and gate_ns > 0)
local ts = ffi.new("struct jmt_timespec[1]")
local opened = false
function event()
  if not opened then
    while true do
      assert(ffi.C.clock_gettime(1, ts) == 0)
      local now_ns = tonumber(ts[0].tv_sec) * 1000000000 + tonumber(ts[0].tv_nsec)
      if now_ns >= gate_ns then break end
      ffi.C.usleep(math.min(10000, math.max(1, (gate_ns-now_ns)/1000)))
    end
    opened = true
  end
  original_event()
end
"""


def tids_for(client, app):
    rows = client.json("postgres", """
SELECT coalesce(json_agg(x),'[]'::json) FROM
(SELECT DISTINCT w.lwtid FROM pg_stat_activity a
 JOIN pg_thread_wait_status w ON w.tid=a.pid
 WHERE a.application_name='%s' AND a.datname='h5_tpcc')x
""" % app)
    return sorted(int(r["lwtid"]) for r in rows)


def driver_argv(script, terminals, duration, seed):
    return ["/usr/bin/sysbench", str(script), "--db-driver=pgsql",
            "--pgsql-host=/tmp", "--pgsql-port=5432", "--pgsql-user=omm",
            "--pgsql-db=h5_tpcc", "--tables=16", "--table-size=1000000",
            "--threads=%d" % terminals, "--time=%d" % duration, "--events=0",
            "--report-interval=1", "--rand-seed=%d" % seed, "--percentile=95", "run"]


def collect_arm(client, sb, args, out, repeat):
    observer = TPWindowObserver("/opt/openGauss/bin/gaussdb", sb.pid, "/dev/nvme0n1")
    baseline, surge, streams = None, None, []
    app = "jmt_surge_" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="jmt-surge-", dir="/dev/shm") as scratch:
        scratch = Path(scratch)
        scratch.chmod(0o755)
        lua = scratch / "surge_readonly.lua"
        lua.write_text(GATED_LUA)
        lua.chmod(0o644)
        try:
            base_log, surge_log = scratch / "baseline.log", scratch / "surge.log"
            streams = [base_log.open("w"), surge_log.open("w")]
            env = dict(os.environ, PGOPTIONS="-c default_transaction_read_only=on -c query_dop=1")
            started = time.monotonic_ns()
            start_ns = started + args.warmup_seconds * 10**9
            end_ns = start_ns + args.window_seconds * 10**9
            base_argv = driver_argv("/usr/share/sysbench/oltp_read_only.lua", 128,
                                    args.warmup_seconds + args.window_seconds + 8, 220000 + repeat)
            baseline = subprocess.Popen(
                ["runuser", "-u", "omm", "--"] + base_argv,
                stdout=streams[0], stderr=subprocess.STDOUT,
                env=dict(env, PGAPPNAME=app + "_base"), start_new_session=True)
            baseline_tids = []
            while time.monotonic_ns() < start_ns - 8 * 10**9:
                if baseline.poll() is not None:
                    raise RuntimeError("baseline driver failed")
                baseline_tids = tids_for(client, app + "_base")
                if len(baseline_tids) == 128:
                    break
                time.sleep(.1)
            if len(baseline_tids) != 128:
                raise RuntimeError("128 baseline not enrolled")
            sleep_until(start_ns - 6 * 10**9)
            surge_argv = driver_argv(lua, 16, args.window_seconds + 12, 330000 + repeat)
            surge = subprocess.Popen(
                ["runuser", "-u", "omm", "--"] + surge_argv,
                stdout=streams[1], stderr=subprocess.STDOUT,
                env=dict(env, PGAPPNAME=app + "_plus16",
                         JMT_SURGE_MONOTONIC_NS=str(start_ns)), start_new_session=True)
            surge_tids = []
            while time.monotonic_ns() < start_ns - 500_000_000:
                if surge.poll() is not None or baseline.poll() is not None:
                    raise RuntimeError("S5 driver failed before measurement")
                surge_tids = tids_for(client, app + "_plus16")
                if len(surge_tids) == 16:
                    break
                time.sleep(.1)
            if len(surge_tids) != 16 or set(baseline_tids) & set(surge_tids):
                raise RuntimeError("16 distinct preconnected gated backends not ready")
            tids = sorted(baseline_tids + surge_tids)
            observer.enroll(tids)
            observer.arm(start_ns, end_ns)
            sleep_until(start_ns - 1_000_000_000)
            native_before = native_snapshot(client, "h5_tpcc")
            sleep_until(start_ns)
            cpu_before = cpu_snapshot(sb.pid, tids, baseline.pid)
            sleep_until(end_ns)
            cpu_after = cpu_snapshot(sb.pid, tids, baseline.pid)
            native_after = native_snapshot(client, "h5_tpcc")
            result = observer.result()
            tx = result["committed_transactions"]
            if tx <= 0:
                raise RuntimeError("no exact window commits")
            baseline.wait(timeout=15)
            surge.wait(timeout=15)
            for stream in streams:
                stream.flush()
            for path, proc in ((base_log, baseline), (surge_log, surge)):
                match = re.search(r"ignored errors:\s*(\d+)", path.read_text())
                if proc.returncode != 0 or not match or int(match[1]):
                    raise RuntimeError("S5 driver errors")
            result.update({
                "shared_buffers_mib": sb.current_mib, "repeat": repeat,
                "topology": "128-warmed-plus-16-first-event-gated-at-measurement",
                "baseline_terminals": 128, "measurement_surge_terminals": 16,
                "baseline_backend_tids": baseline_tids, "surge_backend_tids": surge_tids,
                "surge_gate_monotonic_ns": start_ns,
                "surge_wrapper": GATED_LUA,
                "original_readonly_script_sha256": digest("/usr/share/sysbench/oltp_read_only.lua"),
                "driver_exit_codes": [baseline.returncode, surge.returncode],
                "native_snapshot_before": native_before, "native_snapshot_after": native_after,
                "CPU_snapshot_before": cpu_before, "CPU_snapshot_after": cpu_after,
                "CPU": summarize_cpu(cpu_before, cpu_after, tx),
                "role_profile": "current_16x1M_sysbench_readonly_omm_local_socket",
                "not_flat_144_baseline": True,
                "automatic_recommendation_qualified": False,
            })
            try:
                validate_cpu_window(result)
            except CPUWindowBoundaryError:
                dump(out / "REJECTED_CPU_WINDOW.json", result)
                raise
            dump(out / "WINDOW.json", result)
            return result
        finally:
            for proc in (baseline, surge):
                if proc is not None and proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=5)
            observer.close()
            for stream in streams:
                stream.close()
            for name in ("baseline.log", "surge.log"):
                if (scratch / name).is_file():
                    shutil.copyfile(str(scratch / name), str(out / name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--points", default="2048,4096,8192")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--window-seconds", type=int, default=20)
    args = parser.parse_args()
    if not 20 <= args.warmup_seconds <= 90 or not 5 <= args.window_seconds <= 60 or not 1 <= args.repeats <= 3:
        raise ValueError("bounded S5 warmup/window/repeats required")
    points = [int(v) for v in args.points.split(",")]
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=15)
    active = client.json("postgres", """
SELECT coalesce(json_agg(x),'[]'::json) FROM
(SELECT application_name FROM pg_stat_activity WHERE pid<>pg_backend_pid()
 AND state='active' AND application_name NOT IN
 ('JobScheduler','PercentileJob','Asp','workload'))x
""")
    if active:
        raise RuntimeError("other workload active; S5 calibration not started")
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    dump(args.out / "MACHINE.json", machine)
    dump(args.out / "SOURCE.json", {"script_sha256": digest(__file__),
                                   "wrapper": GATED_LUA, "points": points})
    sb, rows, error = OnlineSB(client, args.out), [], None
    try:
        sb.set(sb.original_mib - 256)
        sb.set(sb.original_mib)
        for repeat in range(1, args.repeats + 1):
            order = points if repeat % 2 else list(reversed(points))
            for point in order:
                sb.set(point)
                out = args.out / ("sb%d_r%02d" % (point, repeat))
                out.mkdir()
                row = collect_arm(client, sb, args, out, repeat)
                rows.append({"shared_buffers_mib": point, "repeat": repeat,
                             "TPS": row["observed_committed_TPS"],
                             "CPU_ms_per_tx": row["CPU"]["backend_cpu_ms_per_kernel_window_transaction"],
                             "window_path": str((out / "WINDOW.json").resolve()),
                             "window_sha256": digest(out / "WINDOW.json")})
                print(json.dumps(rows[-1]), flush=True)
    except BaseException as exc:
        error = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        restored = sb.restore()
        dump(args.out / "MATRIX.json", {
            "schema": "jmt.current-TP-surge-kernel-window-matrix/v1",
            "machine_fingerprint": machine["machine_fingerprint"],
            "topology": "128-warmed-plus-16-first-event-gated-at-measurement",
            "rows": rows, "error": error, "complete": len(rows) == len(points) * args.repeats,
            "restoration_verified": restored["config_restored_byte_exact"] and restored["server_status_matches_original"],
            "automatic_recommendation_qualified": False})


if __name__ == "__main__":
    main()
