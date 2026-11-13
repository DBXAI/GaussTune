#!/usr/bin/env python3
"""Bounded live TP-window validation; never changes SB, restarts, or prepares data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
for directory in ("src", "vendor"):
    sys.path.insert(0, str(ROOT / directory))
from jmt.common.db import Gsql
from jmt.common.machine import collect_machine
from jmt.common.transaction_evidence import (
    build_kernel_window_transaction_evidence, read_transaction_evidence,
    validate_transaction_window,
)
from jmt.innovation_4_performance.tp_window_observer import TPWindowObserver


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def receive(process, seconds=15):
    if not select.select([process.stdout], [], [], seconds)[0]:
        raise RuntimeError("worker RPC timed out")
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("worker RPC ended")
    return json.loads(line)


def sleep_until(end):
    while time.monotonic_ns() < end:
        time.sleep(min(.05, (end - time.monotonic_ns()) / 1e9))


def check_alignment(result):
    window = result["measurement_window"]
    transaction = {
        "scored_seconds": result["scored_seconds"], "measurement_window": window,
        "transactions": result["committed_transactions"],
    }
    collection = {
        "warmup_end_ns": window["start_ns"], "measure_end_ns": window["end_ns"],
        "block_summary": {"start_ns": window["start_ns"], "end_ns": window["end_ns"],
                          "duration_seconds": result["scored_seconds"]},
    }
    return validate_transaction_window(collection, transaction)


def selftest(args, pid):
    rows = []
    with tempfile.TemporaryDirectory(prefix="jmt-commit-worker-") as scratch:
        scratch = Path(scratch)
        scratch.chmod(0o755)
        worker = scratch / "worker.py"
        shutil.copyfile(ROOT / "tools/tp_commit_test_worker.py", worker)
        worker.chmod(0o644)
        process = subprocess.Popen([
            "runuser", "-u", "omm", "--", "/usr/bin/python3", "-u", str(worker),
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        try:
            process.stdin.write(json.dumps({
                "database": args.database, "application_name": "jmt_tp_known_count",
            }) + "\n")
            process.stdin.flush()
            identity = receive(process)
            assert identity["status"] == "ready"
            for rollback in (False, True):
                observer = TPWindowObserver("/opt/openGauss/bin/gaussdb", pid, "/dev/nvme0n1")
                try:
                    observer.enroll([identity["linux_tid"]])
                    start = time.monotonic_ns() + 500_000_000
                    end = start + 2_000_000_000
                    observer.arm(start, end)
                    process.stdin.write(json.dumps({
                        "command": "run", "transactions": 100, "rollback": rollback,
                        "start_ns": start + 100_000_000,
                    }) + "\n")
                    process.stdin.flush()
                    completed = receive(process)
                    assert completed["started_ns"] >= start and completed["ended_ns"] < end
                    sleep_until(end + 100_000_000)
                    row = observer.result()
                    row["known_count_worker"] = completed
                    row["expected_commits"] = 0 if rollback else 100
                    row["count_exact"] = row["committed_transactions"] == row["expected_commits"]
                    row["alignment"] = check_alignment(row)
                    dump(args.out / ("rollback.json" if rollback else "commits.json"), row)
                    rows.append(row)
                    assert row["count_exact"], "commit hook does not match known transaction semantics"
                finally:
                    observer.close()
            process.stdin.write('{"command":"close"}\n')
            process.stdin.flush()
            process.wait(timeout=10)
            assert process.returncode == 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
    return {"mode": "known_count", "cases": len(rows),
            "all_exact": all(r["count_exact"] for r in rows),
            "commits": [r["committed_transactions"] for r in rows]}


def sysbench(args, pid, client):
    if args.command_artifact is None:
        raise ValueError("--command-artifact required; credentials are never printed")
    command = json.loads(args.command_artifact.read_text())
    if command["benchmark"] != "sysbench" or command["dataset"]["database"] != args.database:
        raise ValueError("Sysbench database does not match audited command")
    source = command["workload_contract"]["script_artifact"]
    script = Path(source["path"])
    if (script.name != "oltp_read_only.lua" or
            hashlib.sha256(script.read_bytes()).hexdigest() != source["sha256"]):
        raise ValueError("exact read-only Lua source is required")
    argv = list(command["drivers"][0]["argv"])
    password = next((a.split("=", 1)[1] for a in argv if a.startswith("--pgsql-password=")), None)
    password_name = command.get("password_env")
    if password is None and isinstance(password_name, str):
        password = os.environ.get(password_name)
    remove = ("--pgsql-password=", "--threads=", "--time=", "--events=", "--report-interval=")
    if args.peer_auth_user:
        remove += ("--pgsql-host=", "--pgsql-user=", "--config-file=")
    elif not password:
        raise RuntimeError("required benchmark password environment is unset; "
                           "supply it or explicitly select --peer-auth-user for a counter pilot")
    argv = [a for a in argv if not a.startswith(remove) and a != "run"]
    if args.peer_auth_user:
        argv.extend(["--pgsql-host=/tmp", "--pgsql-user=" + args.peer_auth_user])
    argv.extend(["--threads=%d" % args.terminals, "--time=15",
                 "--events=0", "--report-interval=1", "run"])
    application = "jmt_tp_window_" + uuid.uuid4().hex[:12]
    env = dict(os.environ, PGAPPNAME=application,
               PGOPTIONS="-c default_transaction_read_only=on -c query_dop=1")
    if password is not None:
        env["PGPASSWORD"] = password
    observer = TPWindowObserver("/opt/openGauss/bin/gaussdb", pid, "/dev/nvme0n1")
    process = None
    # No capture-file writes to the measured physical device during the arm.
    with tempfile.TemporaryDirectory(prefix="jmt-tp-window-", dir="/dev/shm") as scratch:
        logfile = Path(scratch) / "sysbench.log"
        try:
            driver_argv = list(argv)
            if args.peer_auth_user:
                Path(scratch).chmod(0o755)
                driver_argv = ["runuser", "-u", args.peer_auth_user, "--"] + driver_argv
            else:
                if any(c in password for c in ("\n", "\r", "\0")):
                    raise ValueError("password cannot be represented in a sysbench config line")
                secret_config = Path(scratch) / "sysbench-secret.conf"
                secret_config.write_text("pgsql-password=%s\n" % password)
                secret_config.chmod(0o600)
                driver_argv.insert(1, "--config-file=" + str(secret_config))
            with logfile.open("w") as stream:
                started = time.monotonic_ns()
                process = subprocess.Popen(driver_argv, stdout=stream, stderr=subprocess.STDOUT,
                                           env=env, start_new_session=True)
                tids = []
                while time.monotonic_ns() - started < 5_000_000_000:
                    if process.poll() is not None:
                        raise RuntimeError("Sysbench exited before enrollment; inspect preserved log")
                    tids = client.json("postgres", (
                        "SELECT coalesce(json_agg(x),'[]'::json) FROM "
                        "(SELECT DISTINCT w.lwtid FROM pg_stat_activity a "
                        "JOIN pg_thread_wait_status w ON w.tid=a.pid "
                        "WHERE a.application_name='%s' AND a.datname='%s')x"
                    ) % (application, args.database))
                    if len(tids) == args.terminals:
                        break
                    time.sleep(.1)
                if len(tids) != args.terminals:
                    raise RuntimeError("did not enroll all expected Sysbench backend threads")
                enrolled = sorted(int(row["lwtid"]) for row in tids)
                observer.enroll(enrolled)
                start = time.monotonic_ns() + 1_000_000_000
                end = start + int(args.window_seconds * 1e9)
                if end >= started + 13_000_000_000:
                    raise RuntimeError("driver does not safely cover planned measurement")
                observer.arm(start, end)
                sleep_until(end + 100_000_000)
                result = observer.result()
                result["alignment"] = check_alignment(result)
                result["terminals"] = args.terminals
                result["driver_application_name"] = application
                result["driver_argv_without_password"] = argv
                result["auth_profile"] = (
                    "local-peer-%s-counter-pilot-NOT-original-benchmark-role" % args.peer_auth_user
                    if args.peer_auth_user else "audited-benchmark-role-secret-config")
                result["read_only_script_sha256"] = source["sha256"]
                process.wait(timeout=20)
                result["driver_exit_code"] = process.returncode
                assert process.returncode == 0
            text = logfile.read_text()
            match = re.search(r"ignored errors:\s*(\d+)", text)
            result["driver_ignored_errors"] = int(match.group(1)) if match else None
            if result["driver_ignored_errors"] != 0 or result["committed_transactions"] <= 0:
                raise RuntimeError("Sysbench error/zero transaction window")
            dump(args.out / "SYSBENCH_WINDOW.json", result)
            transaction = build_kernel_window_transaction_evidence(
                args.out / "SYSBENCH_WINDOW.json",
                machine_fingerprint=args.machine_fingerprint, trace_id=application)
            dump(args.out / "TRANSACTIONS.json", transaction)
            transactions, seconds, transaction_sha = read_transaction_evidence(
                args.out / "TRANSACTIONS.json",
                machine_fingerprint=args.machine_fingerprint,
                trace_id=application, benchmark="sysbench")
            return {"mode": "sysbench", "terminals": args.terminals,
                    "window_seconds": result["scored_seconds"],
                    "committed_transactions": result["committed_transactions"],
                    "committed_TPS": result["observed_committed_TPS"],
                    "alignment": result["alignment"],
                    "transaction_evidence_readback": {
                        "transactions": transactions, "seconds": seconds,
                        "sha256": transaction_sha}}
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            observer.close()
            if logfile.is_file():
                shutil.copyfile(logfile, args.out / "sysbench.log")
                (args.out / "sysbench.log").chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("selftest", "sysbench"), default="selftest")
    parser.add_argument("--database", default="h5_tpcc")
    parser.add_argument("--terminals", type=int, default=128, choices=(128, 144))
    parser.add_argument("--window-seconds", type=int, default=6, choices=range(2, 7))
    parser.add_argument("--command-artifact", type=Path)
    parser.add_argument("--peer-auth-user", choices=("omm",),
                        help="Explicit local read-only counter pilot, not the original TP role/profile")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    pid = int(Path("/opt/openGauss/data/postmaster.pid").read_text().splitlines()[0])
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=15)
    settings_sql = (
        "SELECT json_build_object('shared_buffers',current_setting('shared_buffers'),"
        "'shared_buffers_target',current_setting('shared_buffers_target'))")
    before = client.json("postgres", settings_sql)
    active = client.json("postgres", (
        "SELECT coalesce(json_agg(x),'[]'::json) FROM "
        "(SELECT application_name FROM pg_stat_activity WHERE pid<>pg_backend_pid() "
        "AND state='active' AND application_name NOT IN "
        "('JobScheduler','PercentileJob','Asp','workload'))x"))
    if active:
        raise RuntimeError("other active client workloads present; no live arm started")
    machine = collect_machine(
        Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
        "local-source-b314224-not-asserted-binary-commit")
    args.machine_fingerprint = machine["machine_fingerprint"]
    dump(args.out / "MACHINE.json", machine)
    if args.mode == "selftest":
        summary = selftest(args, pid)
    else:
        summary = sysbench(args, pid, client)
    after = client.json("postgres", settings_sql)
    summary.update(
        SB_settings_unchanged=before == after,
        before_SB=before, after_SB=after,
        postmaster_unchanged=pid == int(
            Path("/opt/openGauss/data/postmaster.pid").read_text().splitlines()[0]),
        automatic_recommendation_qualified=False,
        qualification="Live synchronous-counter validation, not a qualified TP model "
        "or new five-stage recommendation. No prepare/reset/restart/SB changes.")
    dump(args.out / "SUMMARY.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
