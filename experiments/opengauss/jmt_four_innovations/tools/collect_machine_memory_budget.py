#!/usr/bin/env python3
"""Read-only memory census + bounded TP-only memory probes; never change GUCs."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql
from jmt.common.machine import collect_machine
from jmt.innovation_3_search.memory_budget import derive_database_memory_budget
from jmt.common.database_memory_domain import collect_work_mem_domain


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def kv_memory(path):
    result = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1].isdigit():
            result[parts[0].rstrip(":")] = int(parts[1]) * (1024 if len(parts) > 2 and parts[2] == "kB" else 1)
    return result


def host_census(pid):
    memory = kv_memory(Path("/proc/meminfo"))
    ps, skipped = 0, []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == pid:
            continue
        try:
            ps += kv_memory(proc / "smaps_rollup").get("Pss", 0)
        except (OSError, ProcessLookupError):
            skipped.append(int(proc.name))
    limits, paths = [], []
    # Include all cgroup ancestors, for the DATABASE's group, not just this
    # collecting shell. Large v1 "unlimited" sentinels are harmless to min().
    for line in Path("/proc/%d/cgroup" % pid).read_text().splitlines():
        _, controllers, group = line.split(":", 2)
        if "memory" in controllers.split(","):
            root, name = Path("/sys/fs/cgroup/memory"), "memory.limit_in_bytes"
        elif controllers == "":
            root, name = Path("/sys/fs/cgroup"), "memory.max"
        else:
            continue
        current = root / group.lstrip("/")
        while True:
            path = current / name
            if path.exists():
                raw = path.read_text().strip()
                paths.append({"path": str(path), "value": raw})
                if raw != "max":
                    limits.append(int(raw))
            if current == root:
                break
            current = current.parent
    return {
        "physical_memory_bytes": memory["MemTotal"],
        "MemAvailable_bytes_diagnostic_only": memory.get("MemAvailable"),
        "nonreclaimable_kernel_bytes": sum(memory.get(k, 0) for k in ("SUnreclaim", "KernelStack", "PageTables")),
        "non_database_process_pss_bytes": ps, "unreadable_or_exited_PIDs": skipped,
        "cgroup_hard_limits_bytes": limits, "cgroup_sources": paths,
        "reclaimable_file_cache_not_subtracted": True,
    }


def memory_snapshot(client, database):
    rows = client.json(database, "SELECT json_agg(t) FROM (SELECT * FROM pg_total_memory_detail)t")
    if len({r["nodename"] for r in rows}) != 1:
        raise ValueError("this collector currently requires a single openGauss node")
    return {r["memorytype"]: r["memorymbytes"] for r in rows}


def active_sessions(client, app=None):
    clause = ("application_name='%s'" % app) if app else (
        "datname='h5_tpch' AND state='active' AND pid <> pg_backend_pid()")
    return int(client.json("h5_tpch", "SELECT row_to_json(t) FROM (SELECT count(*) AS n FROM pg_stat_activity WHERE " + clause + ")t")["n"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--postmaster-pid", type=int, required=True)
    parser.add_argument("--probe-seconds", type=int, default=30)
    parser.add_argument("--host-safety-fraction", type=float, default=.10)
    parser.add_argument("--host-min-reserve-mib", type=int, default=2048)
    parser.add_argument("--host-extra-headroom-mib", type=int, default=512)
    parser.add_argument("--dynamic-headroom-mib", type=int, default=256)
    parser.add_argument("--host-reserve-mib", type=int)
    args = parser.parse_args()
    if not 15 <= args.probe_seconds <= 120:
        raise ValueError("TP-only probe duration must be in 15..120 seconds")
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=10)
    settings = client.json("h5_tpch", """SELECT json_agg(t) FROM
        (SELECT name,setting,unit,context FROM pg_settings WHERE name IN
        ('shared_buffers','max_process_memory','enable_memory_limit','cstore_buffers'))t""")
    work_mem_domain = collect_work_mem_domain(client, "h5_tpch")
    dump(args.out / "DATABASE_WORK_MEM_DOMAIN.json", work_mem_domain)
    identity_sql = "SELECT row_to_json(t) FROM (SELECT pg_postmaster_start_time()::text AS start)t"
    identity = client.json("h5_tpch", identity_sql)
    if int(Path("/opt/openGauss/data/postmaster.pid").read_text().splitlines()[0]) != args.postmaster_pid:
        raise ValueError("postmaster PID mismatch")
    if active_sessions(client):
        raise ValueError("AP queries active; refuse to mislabel TP-only memory")
    prior_TP = client.json("h5_tpch", """SELECT row_to_json(t) FROM
        (SELECT count(*) AS n FROM pg_stat_activity WHERE datname='h5_tpcc')t""")["n"]
    if prior_TP:
        raise ValueError("existing TP sessions present; memory probe must own the workload")
    host = host_census(args.postmaster_pid)
    before = memory_snapshot(client, "h5_tpch")
    probes, samples = [], []
    # 144 is a conservative CONNECTION/MEMORY probe, explicitly not the S5
    # one-event-gated cache/TPS experiment. No throughput model uses this run.
    for threads in (128, 144):
        app = "jmt_mem_" + uuid.uuid4().hex[:12]
        log = args.out / ("TP%d_MEMORY_ONLY.sysbench.log" % threads)
        argv = ["/usr/bin/sysbench", "/usr/share/sysbench/oltp_read_only.lua",
                "--db-driver=pgsql", "--pgsql-host=/tmp", "--pgsql-port=5432",
                "--pgsql-user=omm", "--pgsql-db=h5_tpcc", "--tables=16",
                "--table-size=1000000", "--threads=%d" % threads,
                "--time=%d" % (args.probe_seconds + 10), "--events=0",
                "--report-interval=1", "--rand-seed=611122025", "run"]
        started, enrolled, proc = time.time(), False, None
        arm = []
        try:
            with log.open("x") as stream:
                proc = subprocess.Popen(["runuser", "-u", "omm", "--"] + argv,
                                        stdout=stream, stderr=subprocess.STDOUT,
                                        env=dict(os.environ, PGAPPNAME=app,
                                                 PGOPTIONS="-c default_transaction_read_only=on -c query_dop=1"),
                                        start_new_session=True)
                while proc.poll() is None:
                    n = active_sessions(client, app)
                    enrolled = enrolled or n == threads
                    present = active_sessions(client) != 0
                    if present:
                        raise ValueError("AP appeared during TP-only memory sample")
                    if kv_memory(Path("/proc/meminfo")).get("MemAvailable", 0) < 3 * 2**30:
                        raise ValueError("memory probe safety stop: MemAvailable below 3 GiB")
                    item = {"unix": time.time(), "threads_requested": threads,
                            "observed_sessions": n, "memory_mib": memory_snapshot(client, "h5_tpch")}
                    arm.append(item)
                    dump(args.out / ("memory_%d_%03d.json" % (threads, len(arm))), item)
                    time.sleep(1)
                proc.wait(timeout=5)
                if proc.returncode or not enrolled:
                    raise RuntimeError("TP memory probe failed or never enrolled all requested connections")
        finally:
            if proc is not None and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        samples.extend(arm)
        probes.append({"threads": threads, "fully_enrolled": enrolled,
                       "started_unix": started, "finished_unix": time.time(),
                       "samples": len(arm), "driver_exit_code": proc.returncode,
                       "AP_queries_present": False, "memory_only_not_S5_TPS_or_cache_evidence": True,
                       "driver_log_sha256": hashlib.sha256(log.read_bytes()).hexdigest()})
    after = memory_snapshot(client, "h5_tpch")
    after_settings = client.json("h5_tpch", """SELECT json_agg(t) FROM
        (SELECT name,setting,unit,context FROM pg_settings WHERE name IN
        ('shared_buffers','max_process_memory','enable_memory_limit','cstore_buffers'))t""")
    if settings != after_settings or identity != client.json("h5_tpch", identity_sql):
        raise ValueError("database process/settings changed during census")
    if work_mem_domain != collect_work_mem_domain(client, "h5_tpch"):
        raise ValueError("database memory SET domain changed during census")
    startup = next(r for r in settings if r["name"] == "shared_buffers")
    if startup["unit"] != "8kB":
        raise ValueError("unexpected SB unit")
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    observation = {
        "schema": "jmt.readonly-machine-and-TP-memory-observation/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "machine_fingerprint": machine["machine_fingerprint"], "host": host,
        "database": {"postmaster_pid": args.postmaster_pid, "postmaster_identity": identity,
                     "settings": settings, "memory_mib": after,
                     "memory_before_mib": before,
                     "startup_shared_buffers_mib": int(startup["setting"]) * 8192 / 2**20},
        "TP_only_probe": {"arms": probes, "AP_queries_present": False,
                          "database": "h5_tpcc",
                          "driver_script_sha256": hashlib.sha256(
                              Path("/usr/share/sysbench/oltp_read_only.lua").read_bytes()).hexdigest(),
                          "max_dynamic_used_memory_mib": max(
                              [before["dynamic_used_memory"], after["dynamic_used_memory"]]
                              + [s["memory_mib"]["dynamic_used_memory"] for s in samples]),
                          "observed_not_a_lifetime_peak_upper_bound": True},
        "database_settings_and_startup_unchanged": True,
    }
    dump(args.out / "MEMORY_OBSERVATION.json", observation)
    budget = derive_database_memory_budget(
        observation, host_safety_fraction=args.host_safety_fraction,
        host_min_reserve_mib=args.host_min_reserve_mib,
        host_extra_headroom_mib=args.host_extra_headroom_mib,
        dynamic_headroom_mib=args.dynamic_headroom_mib,
        host_reserve_override_mib=args.host_reserve_mib)
    budget.update({"created_at": datetime.now().astimezone().isoformat(),
                   "machine_fingerprint": machine["machine_fingerprint"],
                   "postmaster_identity": identity, "postmaster_pid": args.postmaster_pid,
                   "observation_path": str((args.out / "MEMORY_OBSERVATION.json").resolve()),
                   "observation_sha256": hashlib.sha256((args.out / "MEMORY_OBSERVATION.json").read_bytes()).hexdigest(),
                   "database_work_mem_domain_path": str((args.out / "DATABASE_WORK_MEM_DOMAIN.json").resolve()),
                   "database_work_mem_domain_sha256": hashlib.sha256(
                       (args.out / "DATABASE_WORK_MEM_DOMAIN.json").read_bytes()).hexdigest(),
                   "model_code_sha256": {
                       str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in (Path(__file__).resolve(),
                                 ROOT / "src/jmt/innovation_3_search/memory_budget.py",
                                 ROOT / "src/jmt/innovation_3_search/legacy_memory_budget.py",
                                 ROOT / "src/jmt/common/db.py",
                                 ROOT / "src/jmt/common/database_memory_domain.py",
                                 ROOT / "src/jmt/common/memory_units.py")}})
    dump(args.out / "MEMORY_BUDGET.json", budget)
    print(json.dumps(budget, indent=2))


if __name__ == "__main__":
    main()
