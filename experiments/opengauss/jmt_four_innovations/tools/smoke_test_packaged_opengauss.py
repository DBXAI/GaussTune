#!/usr/bin/env python3
"""Smoke-test a relocated release on an ISOLATED disposable local instance.

Never starts/stops/reconfigures /opt/openGauss/data. No production data,
credentials or network trust are copied. Requires root only to run the new
instance as the existing local OS user omm; the instance itself is not root.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import tempfile
import time


SMOKE_HBA = (
    "local all all trust\n"
    "host all all 127.0.0.1/32 reject\n"
    "host all all ::1/128 reject\n"
)


def smoke_configuration(port, socket_directory):
    # openGauss rejects empty listen_addresses. Bind loopback only and reject
    # ALL TCP in pg_hba.conf; use the private 0700 Unix socket directory.
    # The custom interval GUC is milliseconds, not seconds (range 10..60000).
    return """
# Disposable smoke instance: loopback TCP REJECTED, private Unix socket.
listen_addresses = '127.0.0.1'
port = %d
unix_socket_directory = '%s'
shared_buffers = 512MB
shared_buffers_target = 512MB
shared_buffers_resize_granule = 256MB
shared_buffers_resize_interval = 1000
max_process_memory = 4GB
max_connections = 20
work_mem = 32MB
jmt_buffer_trace_epoch = 0
jmt_buffer_trace_sample_ppm = 0
""" % (port, socket_directory)


def tcp_rejection_probe_argv(home, port):
    # openGauss gsql is NOT psql: it has no "-w/never prompt" switch.
    # The caller closes stdin instead, and no password is supplied.
    return [str(home / "bin/gsql"), "-h", "127.0.0.1", "-p", str(port),
            "-d", "postgres", "-U", "omm", "-c", "SELECT 1;"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=15439)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 5432:
        raise ValueError("an independent non-production port is required")
    if args.out.exists():
        raise ValueError("refusing to overwrite old verification")
    home = (args.release / "opengauss").resolve()
    user = pwd.getpwnam("omm")
    production_pidfile = Path("/opt/openGauss/data/postmaster.pid")
    production_pid = production_pidfile.read_text().splitlines()[0]
    production_config_sha = hashlib.sha256(
        Path("/opt/openGauss/data/postgresql.conf").read_bytes()).hexdigest()
    commands = []
    def call(argv, timeout=90, check=True):
        proc = subprocess.run(
            ["runuser", "-u", "omm", "--", "env", "GAUSSHOME=" + str(home),
             "LD_LIBRARY_PATH=" + str(home / "lib"),
             "GAUSSLOG=" + str(scratch / "client-logs")] + argv,
            text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
        # argv contains only a private password FILE PATH, never a password.
        commands.append({"argv": argv, "returncode": proc.returncode,
                         "stdout": proc.stdout[-12000:], "stderr": proc.stderr[-12000:]})
        if check and proc.returncode:
            raise RuntimeError("isolated smoke command failed: " + str(argv))
        return proc

    result = {"schema": "jmt.relocated-opengauss-isolated-smoke/v1",
              "started_at": datetime.now().astimezone().isoformat(),
              "release": str(args.release.resolve()),
              "separate_port": args.port, "network_listen_enabled": True,
              "network_listen_scope": "loopback_only_all_TCP_rejected_by_pg_hba",
              "production_data_used": False, "commands": commands,
              "cross_machine_validation": False}
    scratch = Path(tempfile.mkdtemp(prefix="jmt-release-smoke-", dir="/var/tmp"))
    scratch.chmod(0o700)
    os.chown(str(scratch), user.pw_uid, user.pw_gid)
    data = scratch / "data"
    password_file = scratch / "init.password.private"
    password_file.write_text("Jmt!" + secrets.token_hex(9) + "9aA\n")
    password_file.chmod(0o600)
    os.chown(str(password_file), user.pw_uid, user.pw_gid)
    started = False
    failure = None
    try:
        for binary in ("gaussdb", "gsql"):
            call([str(home / "bin" / binary), "--version"])
        call([str(home / "bin/gs_initdb"), "-D", str(data),
              "--nodename=jmt_release_smoke", "--pwfile=" + str(password_file),
              "--auth-local=trust", "--auth-host=sha256", "--locale=C"], timeout=180)
        password_file.unlink()
        with (data / "postgresql.conf").open("a") as stream:
            stream.write(smoke_configuration(args.port, scratch))
        (data / "pg_hba.conf").write_text(SMOKE_HBA)
        call([str(home / "bin/gs_ctl"), "start", "-D", str(data), "-M", "primary",
              "-l", str(scratch / "start.log"), "-w", "-t", "60"], timeout=90)
        started = True
        smoke_pid = (data / "postmaster.pid").read_text().splitlines()[0]
        if smoke_pid == production_pid:
            raise RuntimeError("isolation PID guard failed")
        def sql(statement):
            return call([str(home / "bin/gsql"), "-X", "-A", "-t", "-q",
                         "-v", "ON_ERROR_STOP=1", "-h", str(scratch), "-p", str(args.port),
                         "-d", "postgres", "-c", statement])
        sql("SELECT version(); SHOW shared_buffers; SHOW shared_buffers_target; "
            "SHOW jmt_buffer_trace_epoch; SHOW jmt_buffer_trace_sample_ppm; "
            "SHOW listen_addresses; SHOW shared_buffers_resize_interval; SELECT 1;")
        rejected = call(tcp_rejection_probe_argv(home, args.port), timeout=10, check=False)
        if rejected.returncode == 0 or "reject" not in rejected.stderr.lower():
            raise RuntimeError("disposable instance TCP rejection was not verified")
        result["all_TCP_rejected_live_check"] = True
        resize_records = []
        for mib in (256, 512):
            positions = {p: p.stat().st_size for p in (data / "pg_log").glob("postgresql-*.log")}
            call([str(home / "bin/gs_guc"), "reload", "-D", str(data),
                  "-c", "shared_buffers_target=%dMB" % mib])
            deadline = time.monotonic() + 45
            found = []
            while time.monotonic() < deadline:
                for path in (data / "pg_log").glob("postgresql-*.log"):
                    with path.open("r", errors="replace") as stream:
                        stream.seek(positions.get(path, 0))
                        for line in stream:
                            if ("shared buffer resize committed: active buffers %d," % (mib * 128)) in line:
                                found.append(line.strip())
                if found:
                    break
                time.sleep(.2)
            if not found:
                raise TimeoutError("relocated binary did not commit actual online resize")
            resize_records.append({"target_mib": mib, "fresh_commit_log": found[-1]})
            sql("SHOW shared_buffers_target; SELECT 1;")
        result["actual_resize_commits"] = resize_records
        result["smoke_postmaster_pid"] = int(smoke_pid)
        result["passed"] = True
    except BaseException as exc:
        failure = exc
        result["passed"] = False
        result["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        # init/start may fail after creating a live PID; stop only THIS data dir.
        if started or (data / "postmaster.pid").exists():
            try:
                call([str(home / "bin/gs_ctl"), "stop", "-D", str(data), "-m", "fast",
                      "-t", "45"], timeout=60)
            except Exception as exc:
                result["cleanup_error"] = str(exc)
        for name in ("start.log",):
            if (scratch / name).exists():
                result[name] = (scratch / name).read_text(errors="replace")[-14000:]
        result["production_postmaster_unchanged"] = (
            production_pidfile.read_text().splitlines()[0] == production_pid)
        result["production_config_unchanged"] = (
            hashlib.sha256(Path("/opt/openGauss/data/postgresql.conf").read_bytes()).hexdigest()
            == production_config_sha)
        result["ended_at"] = datetime.now().astimezone().isoformat()
        args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        # Do not remove a directory while a server still owns it.
        if not (data / "postmaster.pid").exists():
            shutil.rmtree(str(scratch))
            result["disposable_directory_removed"] = True
        else:
            result["disposable_directory_removed"] = False
        args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    if failure:
        raise failure
    if (not result["production_postmaster_unchanged"] or not result["production_config_unchanged"]
            or not result.get("disposable_directory_removed") or "cleanup_error" in result):
        raise RuntimeError("smoke cleanup/isolation verification failed")
    print(json.dumps({"passed": True, "output": str(args.out),
                      "actual_resize_commits": result["actual_resize_commits"]}), flush=True)


if __name__ == "__main__":
    main()
