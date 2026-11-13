#!/usr/bin/env python3
"""Collect fresh, spatially sampled TP trace for counterfactual SB replay.

No SB response sweep is used to choose B_high. A single continuous TP stream
at one real capacity supplies all counterfactual capacities. S5 preserves the
128-warmed/+16-first-event-gated topology. No benchmark data is changed.
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
from jmt.innovation_1_cache.cache_boundary import (
    MISS_PLATEAU_CRITERION, DEFAULT_MAX_MISS_REDUCTION_PER_10000_PER_GIB,
)
from collect_current_tp_matrix import OnlineSB, dump, digest, native_snapshot
from collect_current_tp_surge import GATED_LUA, driver_argv, tids_for
from collect_current_ap import mem_available

TRACE = Path("/dev/shm/jmt_buffer_trace")
PROFILE = "current_16x1M_sysbench_readonly_omm_local_socket"


def guc(name, value):
    if name not in ("jmt_buffer_trace_epoch", "jmt_buffer_trace_sample_ppm"):
        raise ValueError("not a trace control")
    subprocess.run(["runuser", "-u", "omm", "--", "env", "GAUSSHOME=/opt/openGauss",
                    "LD_LIBRARY_PATH=/opt/openGauss/lib", "/opt/openGauss/bin/gs_guc",
                    "reload", "-D", "/opt/openGauss/data", "-c", "%s=%d" % (name, value)],
                   check=True, capture_output=True, timeout=15)
    time.sleep(.3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sample-ppm", type=int, default=2000)
    parser.add_argument("--replay-warmup-seconds", type=int, default=180)
    parser.add_argument("--window-seconds", type=int, default=120)
    parser.add_argument("--source-sb-mib", type=int, default=2048)
    args = parser.parse_args()
    if not 1000 <= args.sample_ppm <= 10000 or not 120 <= args.replay_warmup_seconds <= 600:
        raise ValueError("bounded spatial sample/warmup required")
    if not 60 <= args.window_seconds <= 600:
        raise ValueError("bounded complete kernel window required")
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=30)
    active = client.json("postgres", """
SELECT coalesce(json_agg(application_name),'[]'::json) FROM pg_stat_activity
WHERE pid<>pg_backend_pid() AND state='active'
AND application_name NOT IN ('JobScheduler','PercentileJob','Asp','workload')
""")
    if active:
        raise RuntimeError("another client workload is active")
    settings = client.json("postgres", """
SELECT json_build_object('epoch',current_setting('jmt_buffer_trace_epoch'),
'ppm',current_setting('jmt_buffer_trace_sample_ppm'),
'pagewriters',current_setting('pagewriter_thread_num'),
'incremental_checkpoint',current_setting('enable_incremental_checkpoint'),
'oid',(SELECT oid FROM pg_database WHERE datname='h5_tpcc'))
""")
    if int(settings["epoch"]) or int(settings["ppm"]):
        raise RuntimeError("existing trace control is active")
    if settings["incremental_checkpoint"] != "on":
        raise RuntimeError("candidate-list replay requires actual incremental checkpoint")
    if not TRACE.is_dir() or TRACE.is_symlink() or TRACE.stat().st_mode & 0o077:
        raise RuntimeError("native trace directory is not private")
    epoch = max([int(m[1]) for p in TRACE.iterdir()
                 for m in [re.match(r"e(\d+)-t\d+-\d+\.bin$", p.name)] if m] + [0]) + 1
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    dump(args.out / "POLICY.json", {
        "created_at": datetime.now().astimezone().isoformat(),
        "TP_profile": PROFILE, "source_SB_mib": args.source_sb_mib,
        "sample_ppm": args.sample_ppm, "epoch": epoch,
        "spatial_page_sampling_all_selected_page_events": True,
        "replay_warmup_seconds": args.replay_warmup_seconds,
        "measure_window_seconds": args.window_seconds,
        "S5_topology": "128-warmed-plus-16-first-event-gated-at-measurement",
        "cache_grid_mib": [128,256,512,768,1024,1536,2048,3072,4096,6144,8192],
        "B_high_criterion": MISS_PLATEAU_CRITERION,
        "max_miss_reduction_per_10000_per_gib": DEFAULT_MAX_MISS_REDUCTION_PER_10000_PER_GIB,
        "endpoint_policy": "right endpoint of first negligible SB-miss reduction",
        "criterion_frozen_before_trace": True,
        "P_SB_not_TPS_and_not_P_SB_plus_P_OS": True,
        "minimum_left_rising_point_required": True,
        "actual_capacity_hit_abs_tolerance": .002,
        "actual_capacity_miss_relative_tolerance": .20,
        "runtime_physical_IO_attribution_qualified": False,
        "maximum_trace_bytes": 3 * 2**30, "trace_has_no_credentials": True})
    dump(args.out / "MACHINE.json", machine)
    sb = OnlineSB(client, args.out)
    drivers, logs, windows = [], [], []
    app = "jmt_cache_" + uuid.uuid4().hex[:10]
    error = None
    resources = []
    trace_started = False
    with tempfile.TemporaryDirectory(prefix="jmt-cache-trace-", dir="/dev/shm") as scratch:
        scratch = Path(scratch)
        scratch.chmod(0o755)
        lua = scratch / "gated.lua"
        lua.write_text(GATED_LUA)
        lua.chmod(0o644)
        def driver(n, name, script, gate=None):
            stream = (args.out / (name + ".sysbench.log")).open("w")
            logs.append(stream)
            env = dict(os.environ, PGAPPNAME=app+"_"+name,
                       PGOPTIONS="-c default_transaction_read_only=on -c query_dop=1")
            if gate:
                env["JMT_SURGE_MONOTONIC_NS"] = str(gate)
            proc = subprocess.Popen(
                ["runuser", "-u", "omm", "--"] + driver_argv(script, n, 1800, 551112),
                stdout=stream, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            drivers.append(proc)
            return proc
        def wait(until):
            while time.monotonic_ns() < until:
                if any(p.poll() is not None for p in drivers):
                    raise RuntimeError("TP driver ended before the planned stop")
                free = shutil.disk_usage("/opt/openGauss/data").free
                avail = mem_available()
                trace_bytes = sum(p.stat().st_size for p in TRACE.glob("e%d-t*-*.bin" % epoch))
                resources.append({"ns": time.monotonic_ns(), "disk_free": free,
                                  "MemAvailable": avail, "trace_bytes": trace_bytes})
                if free < 8*2**30 or avail < 3*2**30 or trace_bytes > 3*2**30:
                    raise RuntimeError("bounded trace safety guard triggered")
                time.sleep(min(1, max(0, (until-time.monotonic_ns())/1e9)))
        def score(name, seconds, surge=False):
            observer = TPWindowObserver("/opt/openGauss/bin/gaussdb", sb.pid, "/dev/nvme0n1")
            try:
                start = time.monotonic_ns() + (8 if surge else 2)*10**9
                if surge:
                    driver(16, "plus16", lua, start)
                    while len(tids_for(client, app+"_plus16")) != 16:
                        if time.monotonic_ns() > start-2*10**9:
                            raise RuntimeError("gated extra 16 not connected")
                        wait(time.monotonic_ns()+100_000_000)
                tids = tids_for(client, app+"_base")
                if len(tids) != 128:
                    raise RuntimeError("128 TP backends not ready")
                if len(drivers) == 2:
                    extra = tids_for(client, app+"_plus16")
                    if len(extra) != 16 or set(extra)&set(tids):
                        raise RuntimeError("extra 16 backend membership differs")
                    tids += extra
                observer.enroll(sorted(tids))
                end = start+seconds*10**9
                observer.arm(start,end)
                before = native_snapshot(client,"h5_tpcc")
                wait(end)
                after = native_snapshot(client,"h5_tpcc")
                row = observer.result()
                row.update({"name":name,"native_before":before,"native_after":after,
                            "surge_first_event_gate_ns":start if surge else None,
                            "shared_buffers_mib":sb.current_mib})
                dump(args.out/(name+".WINDOW.json"),row)
                windows.append(row)
                print(json.dumps({"window":name,"TPS":row["observed_committed_TPS"]}),flush=True)
                return row
            finally:
                observer.close()
        try:
            sb.set(args.source_sb_mib)
            driver(128,"base","/usr/share/sysbench/oltp_read_only.lua")
            wait(time.monotonic_ns()+30*10**9)
            score("TP128_TRACE_OFF_BEFORE",20)
            # Snapshot is metadata only and not an initial-hit label for
            # counterfactual capacities. Cold virtual warmup remains explicit.
            snapshot = client.execute("postgres",
                "COPY (SELECT bufferid,reltablespace,reldatabase,relfilenode,bucketid,"
                "relforknumber,relblocknumber,usage_count FROM pg_buffercache_pages() "
                "WHERE reldatabase=%d) TO STDOUT" % settings["oid"])
            if snapshot["exit_code"]:
                raise RuntimeError("read-only pg_buffercache snapshot failed")
            (args.out/"buffer-snapshot.tsv").write_text(snapshot["stdout"])
            guc("jmt_buffer_trace_sample_ppm",args.sample_ppm)
            guc("jmt_buffer_trace_epoch",epoch)
            trace_started = True
            trace_begin = time.monotonic_ns()
            dump(args.out/"TRACE_START.json",{"epoch":epoch,"started_ns":trace_begin})
            wait(trace_begin+args.replay_warmup_seconds*10**9)
            score("TP128_TRACE_ON",args.window_seconds)
            score("S5_TRACE_ON",args.window_seconds,surge=True)
            guc("jmt_buffer_trace_epoch",0)
            guc("jmt_buffer_trace_sample_ppm",0)
            trace_started = False
            score("S5_TRACE_OFF_AFTER",20)
        except BaseException as exc:
            error = type(exc).__name__+": "+str(exc)
            raise
        finally:
            if trace_started:
                guc("jmt_buffer_trace_epoch",0)
                guc("jmt_buffer_trace_sample_ppm",0)
            for proc in drivers:
                if proc.poll() is None:
                    os.killpg(proc.pid,signal.SIGINT)
            for proc in drivers:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid,signal.SIGTERM)
                    proc.wait(timeout=10)
            for stream in logs:
                stream.flush()
                stream.close()
            restored=sb.restore()
            dump(args.out/"COLLECTION.json", {
                "epoch":epoch,"settings":settings,"windows":[w["name"] for w in windows],
                "error":error,"complete":len(windows)==4 and error is None,
                "restoration_verified":restored["config_restored_byte_exact"]
                                      and restored["server_status_matches_original"],
                "all_native_trace_GUCs_restored_to_original_zero":True,
                "source_trace_directory":str(TRACE),
                "machine_fingerprint":machine["machine_fingerprint"]})
            dump(args.out/"RESOURCE_SAMPLES.json",resources)
    # Archive only this run's epoch after all session writers have exited.
    archive=args.out/"native"
    archive.mkdir()
    manifest=[]
    for p in sorted(TRACE.glob("e%d-t*-*.bin"%epoch)):
        target=archive/p.name
        shutil.copyfile(str(p),str(target))
        if digest(p)!=digest(target):
            raise RuntimeError("trace archive verification failed")
        manifest.append({"name":p.name,"bytes":target.stat().st_size,"sha256":digest(target)})
    dump(args.out/"TRACE_FILES.json",{"epoch":epoch,"files":manifest,
                                     "total_bytes":sum(r["bytes"] for r in manifest)})
    print(json.dumps({"trace_archived":str(archive),"files":len(manifest)}),flush=True)


if __name__ == "__main__":
    main()
