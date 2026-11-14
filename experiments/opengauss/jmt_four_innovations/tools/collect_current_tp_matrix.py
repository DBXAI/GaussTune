#!/usr/bin/env python3
"""Real read-only multi-SB Sysbench matrix, with exact commit/device windows.

Offline collection only. Native cache snapshots and schedstat endpoints keep
their own measured times; they are never relabeled exact kernel-window events.
The original benchmark role needs an unavailable password. This fresh matrix
uses one explicitly declared local omm profile throughout, without altering
roles, pg_hba, benchmark tables, resets, or cache dropping.
"""
import argparse
from datetime import datetime
import hashlib
import json
import math
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
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql
from jmt.common.machine import collect_machine
from jmt.common.process_cpu import snapshot_process_trees, process_tree_cpu_delta
from jmt.innovation_4_performance.tp_window_observer import TPWindowObserver


def dump(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sleep_until(ns):
    while True:
        remaining = (ns - time.monotonic_ns()) / 1e9
        if remaining <= 0:
            return
        time.sleep(min(.05, remaining))


class OnlineSB:
    """Scoped real online resize; wait for a NEW actual commit log record."""
    def __init__(self, client, evidence, data_dir=Path("/opt/openGauss/data")):
        self.client, self.evidence, self.data = client, evidence, data_dir
        self.pid = int((self.data / "postmaster.pid").read_text().splitlines()[0])
        self.config = self.data / "postgresql.conf"
        self.original = self.config.read_bytes()
        self.metadata = self.config.stat()
        self.original_status = self.status()
        self.original_mib = int(self.original_status["target_buffers"]) // 128
        self.current_mib = self.original_mib
        self.reloads = []
        self.changed = False
        backup = evidence / "postgresql.conf.original.private"
        fd = os.open(str(backup), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(self.original)

    def status(self):
        return self.client.json("postgres", """
SELECT json_build_object('target_buffers',
 (SELECT setting::bigint FROM pg_settings WHERE name='shared_buffers_target'),
 'maximum_buffers',(SELECT setting::bigint FROM pg_settings WHERE name='shared_buffers'),
 'postmaster_start_time',pg_postmaster_start_time())
""")

    def positions(self):
        return {str(p): p.stat().st_size for p in (self.data / "pg_log").glob("postgresql-*.log")}

    def fresh_commits(self, positions, mib):
        result = []
        for path in (self.data / "pg_log").glob("postgresql-*.log"):
            with path.open("rb") as stream:
                offset = positions.get(str(path), 0)
                if path.stat().st_size < offset:
                    raise RuntimeError("database log was truncated during resize")
                stream.seek(offset)
                text = stream.read().decode(errors="replace")
            for line in text.splitlines():
                match = re.search(
                    r"shared buffer resize committed: active buffers (\d+), released (\d+) bytes", line)
                if match and int(match[1]) == mib * 128:
                    result.append({"path": str(path), "line": line,
                                   "active_buffers": int(match[1]), "released_bytes": int(match[2])})
        return result

    def wait_commit(self, positions, mib, seconds=90):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if int((self.data / "postmaster.pid").read_text().splitlines()[0]) != self.pid:
                raise RuntimeError("postmaster changed during SB resize")
            rows = self.fresh_commits(positions, mib)
            if rows:
                self.current_mib = mib
                return rows[-1]
            time.sleep(.2)
        raise TimeoutError("actual active-buffer commit did not reach %dMiB" % mib)

    def wait_status_target(self, mib, seconds=5):
        """SIGHUP propagates asynchronously to session-local GUC views too.

        A fresh physical commit alone is insufficient; conversely, do not
        reject a correct physical commit just because one new-session view
        was initialized immediately before the reload. Wait for BOTH.
        """
        deadline=time.monotonic()+seconds
        observations=[]
        while True:
            status=self.status()
            observations.append({"monotonic_ns":time.monotonic_ns(),"status":status})
            if (status["postmaster_start_time"] != self.original_status["postmaster_start_time"]
                    or status["maximum_buffers"] != self.original_status["maximum_buffers"]):
                raise RuntimeError("postmaster identity/maximum changed during GUC propagation")
            if int(status["target_buffers"]) == mib*128:
                return status,observations
            if time.monotonic()>=deadline:
                raise RuntimeError("fresh resize commit but GUC target did not converge: %r"
                                   % observations)
            time.sleep(.1)

    def set(self, mib):
        if (isinstance(mib, bool) or not isinstance(mib, int) or mib < 512
                or mib * 128 > int(self.original_status["maximum_buffers"]) or mib % 256):
            raise ValueError("SB must be a 256MiB point inside [512,startup maximum]")
        if mib == self.current_mib:
            return {"target_mib": mib, "unchanged_from_last_verified_point": True}
        positions = self.positions()
        started = time.monotonic_ns()
        self.changed = True
        proc = subprocess.run([
            "runuser", "-u", "omm", "--", "env", "GAUSSHOME=/opt/openGauss",
            "LD_LIBRARY_PATH=/opt/openGauss/lib", "/opt/openGauss/bin/gs_guc",
            "reload", "-D", str(self.data), "-c", "shared_buffers_target=%dMB" % mib,
        ], text=True, capture_output=True, timeout=15)
        if proc.returncode:
            raise RuntimeError("gs_guc reload failed: " + proc.stderr[-1000:])
        commit=self.wait_commit(positions,mib)
        configured,observations=self.wait_status_target(mib)
        row = {"target_mib": mib, "commit": commit,
               "elapsed_seconds": (time.monotonic_ns() - started) / 1e9,
               "configured_status": configured,
               "GUC_propagation_observations":observations}
        if int(row["configured_status"]["target_buffers"]) != mib * 128:
            raise RuntimeError("actual commit and configured SB target differ")
        self.reloads.append(row)
        return row

    def restore(self):
        positions = self.positions()
        temporary = self.data / (".jmt_matrix_restore_%d" % os.getpid())
        fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), self.metadata.st_uid, self.metadata.st_gid)
            os.fchmod(stream.fileno(), self.metadata.st_mode & 0o777)
            stream.write(self.original)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(self.config))
        if int((self.data / "postmaster.pid").read_text().splitlines()[0]) != self.pid:
            raise RuntimeError("config restored, but postmaster changed; refusing to signal")
        os.kill(self.pid, signal.SIGHUP)
        commit = (self.wait_commit(positions, self.original_mib)
                  if self.current_mib != self.original_mib else None)
        after,propagation = self.wait_status_target(self.original_mib)
        result = {"config_restored_byte_exact": self.config.read_bytes() == self.original,
                  "server_status_matches_original": after == self.original_status,
                  "restoration_commit": commit, "after": after, "reloads": self.reloads,
                  "restoration_GUC_propagation":propagation}
        dump(self.evidence / "SB_RESTORE.json", result)
        if not result["config_restored_byte_exact"] or not result["server_status_matches_original"]:
            raise RuntimeError("SB restoration guard failed")
        return result


def native_snapshot(client, database):
    started = time.monotonic_ns()
    result = client.json("postgres", """
SELECT json_build_object('datid',datid,'datname',datname,
 'xact_commit',xact_commit,'xact_rollback',xact_rollback,
 'blks_read',blks_read,'blks_hit',blks_hit,'stats_reset',stats_reset)
 FROM pg_stat_database WHERE datname='%s'
""" % database)
    return {"started_ns": started, "finished_ns": time.monotonic_ns(), "value": result,
            "qualification": "Asynchronously published native counters, not exact per-event kernel timestamps"}


def cpu_snapshot(pid, tids, driver_pid):
    started = time.monotonic_ns()
    rows = {}
    for tid in tids:
        rows[str(tid)] = int(Path("/proc/%d/task/%d/schedstat" % (pid, tid)).read_text().split()[0])
    roots = [driver_pid] if isinstance(driver_pid, int) else list(driver_pid)
    driver = 0
    for root in roots:
        for path in Path("/proc/%d/task" % root).glob("*/schedstat"):
            try:
                driver += int(path.read_text().split()[0])
            except FileNotFoundError:
                raise ValueError("driver wrapper disappeared during CPU snapshot")
    tree = snapshot_process_trees(roots)
    return {"started_ns": started, "finished_ns": time.monotonic_ns(),
            "backend_cpu_ns": rows, "driver_thread_cpu_ns": driver,
            "driver_process_tree": tree,
            "legacy_driver_thread_cpu_ns_is_wrapper_only": True}


def summarize_cpu(before, after, tx):
    if set(before["backend_cpu_ns"]) != set(after["backend_cpu_ns"]):
        raise ValueError("CPU backend membership changed")
    cpu = sum(after["backend_cpu_ns"][tid] - ns for tid, ns in before["backend_cpu_ns"].items())
    if cpu < 0:
        raise ValueError("CPU counter moved backwards")
    driver_seconds = process_tree_cpu_delta(
        before["driver_process_tree"], after["driver_process_tree"])
    return {
        "backend_cpu_seconds": cpu / 1e9,
        "backend_cpu_ms_per_kernel_window_transaction": cpu / tx / 1e6,
        "driver_cpu_seconds": driver_seconds,
        "driver_wrapper_only_cpu_seconds": (
            after["driver_thread_cpu_ns"] - before["driver_thread_cpu_ns"]) / 1e9,
        "driver_process_tree_membership_checked": True,
        "qualification": "CPU snapshots bracket their own read intervals; slight boundary error is explicit",
    }


class CPUWindowBoundaryError(ValueError):
    """A schedstat endpoint cannot represent the exact kernel counter window."""


def validate_cpu_window(result):
    """Same 100ms endpoint gate as the recommendation loader; never rescale."""
    window = result["measurement_window"]
    for name, endpoint in (("CPU_snapshot_before", "start_ns"),
                           ("CPU_snapshot_after", "end_ns")):
        snap = result[name]
        span = snap["finished_ns"] - snap["started_ns"]
        offset = snap["started_ns"] - window[endpoint]
        if not 0 <= span < 100_000_000 or abs(offset) > 100_000_000:
            raise CPUWindowBoundaryError(
                "%s: endpoint_offset_ns=%d read_span_ns=%d" % (name, offset, span))


def collect_arm(client, sb, args, out, repeat):
    app = "jmt_matrix_" + uuid.uuid4().hex[:12]
    duration = args.warmup_seconds + args.window_seconds + 8
    argv = ["/usr/bin/sysbench", "/usr/share/sysbench/oltp_read_only.lua",
            "--db-driver=pgsql", "--pgsql-host=/tmp", "--pgsql-port=5432",
            "--pgsql-user=omm", "--pgsql-db=h5_tpcc", "--tables=16",
            "--table-size=1000000", "--threads=128", "--time=%d" % duration,
            "--events=0", "--report-interval=1", "--rand-seed=%d" % (110000 + repeat),
            "--percentile=95", "run"]
    observer = TPWindowObserver("/opt/openGauss/bin/gaussdb", sb.pid, "/dev/nvme0n1")
    proc = None
    started = time.monotonic_ns()
    result = None
    with tempfile.TemporaryDirectory(prefix="jmt-matrix-", dir="/dev/shm") as scratch:
        Path(scratch).chmod(0o755)
        logfile = Path(scratch) / "sysbench.log"
        try:
            with logfile.open("w") as stream:
                proc = subprocess.Popen(["runuser", "-u", "omm", "--"] + argv,
                                        stdout=stream, stderr=subprocess.STDOUT,
                                        env=dict(os.environ, PGAPPNAME=app,
                                                 PGOPTIONS="-c default_transaction_read_only=on -c query_dop=1"),
                                        start_new_session=True)
                tids = []
                for _ in range(70):
                    if proc.poll() is not None:
                        raise RuntimeError("Sysbench startup failed")
                    rows = client.json("postgres", """
SELECT coalesce(json_agg(x),'[]'::json) FROM
(SELECT DISTINCT w.lwtid FROM pg_stat_activity a
 JOIN pg_thread_wait_status w ON w.tid=a.pid
 WHERE a.application_name='%s' AND a.datname='h5_tpcc')x
""" % app)
                    tids = sorted(int(row["lwtid"]) for row in rows)
                    if len(tids) == 128:
                        break
                    time.sleep(.1)
                if len(tids) != 128:
                    raise RuntimeError("cannot enroll exactly 128 backend threads")
                observer.enroll(tids)
                start_ns = max(started + args.warmup_seconds * 10**9,
                               time.monotonic_ns() + 2 * 10**9)
                end_ns = start_ns + args.window_seconds * 10**9
                if end_ns >= started + (duration - 3) * 10**9:
                    raise RuntimeError("Sysbench does not safely cover scheduled counter window")
                observer.arm(start_ns, end_ns)
                # Native SQL is a separately timestamped publication diagnostic.
                # Do not let it delay the kernel-window CPU endpoint.
                sleep_until(start_ns - 1_000_000_000)
                native_before = native_snapshot(client, "h5_tpcc")
                sleep_until(start_ns)
                cpu_before = cpu_snapshot(sb.pid, tids, proc.pid)
                sleep_until(end_ns)
                cpu_after = cpu_snapshot(sb.pid, tids, proc.pid)
                native_after = native_snapshot(client, "h5_tpcc")
                result = observer.result()
                result.update({
                    "shared_buffers_mib": sb.current_mib, "repeat": repeat,
                    "driver_application_name": app, "driver_argv": argv,
                    "benchmark_profile": "current_16x1M_sysbench_readonly_omm_local_socket",
                    "native_snapshot_before": native_before,
                    "native_snapshot_after": native_after,
                    "CPU_snapshot_before": cpu_before, "CPU_snapshot_after": cpu_after,
                })
                tx = result["committed_transactions"]
                if tx <= 0:
                    raise RuntimeError("no committed transactions in exact kernel window")
                result["CPU"] = summarize_cpu(cpu_before, cpu_after, tx)
                try:
                    validate_cpu_window(result)
                except CPUWindowBoundaryError:
                    dump(out / "REJECTED_CPU_WINDOW.json", result)
                    raise
                first, last = native_before["value"], native_after["value"]
                if first["stats_reset"] != last["stats_reset"]:
                    raise RuntimeError("native stats reset during arm")
                hits, reads = last["blks_hit"] - first["blks_hit"], last["blks_read"] - first["blks_read"]
                result["native_cache_window"] = {
                    "published_hits_delta": hits, "published_reads_delta": reads,
                    "shared_buffer_hit_ratio": hits / (hits + reads) if hits + reads > 0 else None,
                    "used_for_exact_requests_per_transaction": False,
                    "qualification": "Native publication-delay interval, separate from exact commit/device window",
                }
                proc.wait(timeout=15)
            text = logfile.read_text()
            ignored = re.search(r"ignored errors:\s*(\d+)", text)
            if proc.returncode or ignored is None or int(ignored[1]):
                raise RuntimeError("Sysbench arm failed/ignored errors; see log")
            result["driver_exit_code"], result["driver_ignored_errors"] = proc.returncode, 0
            rates = [float(tps) for seconds, tps in re.findall(
                r"\[\s*([\d.]+)s\s*\].*?tps:\s*([\d.]+)", text)
                     if args.warmup_seconds <= float(seconds) <= args.warmup_seconds + args.window_seconds]
            mean = sum(rates) / len(rates) if rates else 0
            result["driver_one_second_TPS_diagnostic"] = {
                "values": rates, "CV": math.sqrt(sum((x - mean)**2 for x in rates) / len(rates)) / mean if mean else None,
                "used_as_exact_kernel_transaction_count": False,
            }
            result["role_profile_held_constant_across_SB"] = True
            result["automatic_recommendation_qualified"] = False
            dump(out / "WINDOW.json", result)
            return result
        finally:
            if proc is not None and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
            observer.close()
            if logfile.is_file():
                shutil.copyfile(str(logfile), str(out / "sysbench.log"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--points", default="8192,2048,6144,4096")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--window-seconds", type=int, default=20)
    args = parser.parse_args()
    if not (1 <= args.repeats <= 5 and 10 <= args.warmup_seconds <= 120
            and 5 <= args.window_seconds <= 90):
        raise ValueError("bounded repeat/window/warmup arguments required")
    points = [int(x) for x in args.points.split(",")]
    if len(set(points)) != len(points):
        raise ValueError("duplicate SB points")
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=15)
    active = client.json("postgres", """
SELECT coalesce(json_agg(x),'[]'::json) FROM
(SELECT application_name FROM pg_stat_activity WHERE pid<>pg_backend_pid()
 AND state='active' AND application_name NOT IN
 ('JobScheduler','PercentileJob','Asp','workload'))x
""")
    if active:
        raise RuntimeError("other active client workload present")
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    dump(args.out / "MACHINE.json", machine)
    dump(args.out / "COLLECTION_POLICY.json", {
        "selection_policy": "max_tps", "SB_points": points, "repeats": args.repeats,
        "warmup_seconds": args.warmup_seconds, "measurement_seconds": args.window_seconds,
        "benchmark_profile": "current_16x1M_sysbench_readonly_omm_local_socket",
        "driver_Lua_sha256": digest("/usr/share/sysbench/oltp_read_only.lua"),
        "not_original_h7v_role_TCP_profile": True, "no_password_or_HBA_changes": True,
        "script_sha256": digest(__file__),
    })
    sb, rows = OnlineSB(client, args.out), []
    error = None
    try:
        # Establish a fresh actual commit at the startup maximum, not only a GUC string.
        sb.set(sb.original_mib - 256)
        sb.set(sb.original_mib)
        for repeat in range(1, args.repeats + 1):
            order = points if repeat % 2 else list(reversed(points))
            for point in order:
                sb.set(point)
                arm = args.out / ("sb%d_r%02d" % (point, repeat))
                arm.mkdir()
                dump(arm / "SB_STATUS.json", sb.status())
                row = collect_arm(client, sb, args, arm, repeat)
                rows.append({
                    "shared_buffers_mib": point, "repeat": repeat,
                    "TPS": row["observed_committed_TPS"],
                    "native_hit_ratio": row["native_cache_window"]["shared_buffer_hit_ratio"],
                    "CPU_ms_per_tx": row["CPU"]["backend_cpu_ms_per_kernel_window_transaction"],
                    "read_completion_events_per_tx_whole_device": row["device_read_completion_events"] / row["committed_transactions"],
                    "write_completion_events_per_tx_whole_device": row["device_write_completion_events"] / row["committed_transactions"],
                    "driver_one_second_TPS_CV": row["driver_one_second_TPS_diagnostic"]["CV"],
                    "window_path": str((arm / "WINDOW.json").resolve()),
                    "window_sha256": digest(arm / "WINDOW.json"),
                })
                print(json.dumps(rows[-1]), flush=True)
    except BaseException as exc:
        error = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        restoration = sb.restore()
        dump(args.out / "MATRIX.json", {
            "schema": "jmt.current-TP-kernel-window-matrix/v1",
            "created_at": datetime.now().astimezone().isoformat(),
            "machine_fingerprint": machine["machine_fingerprint"],
            "rows": rows, "collection_error": error,
            "complete": len(rows) == len(points) * args.repeats,
            "restoration_verified": restoration["config_restored_byte_exact"] and restoration["server_status_matches_original"],
            "automatic_recommendation_qualified": False,
            "qualification": "Fresh TP-only measurements; not joint five-stage predictions or physical TP-attribution proof",
        })


if __name__ == "__main__":
    main()
