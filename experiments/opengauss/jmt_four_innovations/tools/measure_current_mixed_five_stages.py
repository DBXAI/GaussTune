#!/usr/bin/env python3
"""Real, bounded five-stage overlap-window validation, NOT full AP lifecycles.

Freeze an existing recommendation before running. Keep continuing AP sessions
and their work_mem; only Q2 leaves at S5. TP baseline stays alive throughout;
S5's preconnected +16 opens its first event at the first scoring window.
Original configuration is restored without restarting the postmaster.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.common.db import Gsql, readonly_query
from jmt.common.machine import collect_machine
from jmt.innovation_4_performance.tp_window_observer import TPWindowObserver
from collect_current_tp_matrix import OnlineSB, dump, digest, cpu_snapshot, summarize_cpu
from collect_current_tp_surge import GATED_LUA, driver_argv, tids_for
from collect_current_ap import receive, mem_available
from regenerate_resource_pilot import SQL_SHA256

EXPECTED = [[18], [18, 21], [9, 13, 18, 21], [2, 9, 13, 18, 21], [9, 13, 18, 21]]


def validate_recommendation(document):
    rows = document["stages"]
    if len(rows) != 5:
        raise ValueError("exactly five stages required")
    previous = {}
    for i, row in enumerate(rows):
        selected = row["result"]["selected"]
        wm = dict(selected["work_mem"])
        if (row["stage"] != "S%d" % (i + 1)
                or sorted(row["AP_queries"]) != EXPECTED[i]
                or set(wm) != set(EXPECTED[i])
                or row["TP_baseline_terminals"] != 128
                or row["TP_surge_terminals"] != (16 if i == 4 else 0)):
            raise ValueError("PPT topology or selected WM membership mismatch")
        sb = selected["shared_buffers_mb"]
        if not isinstance(sb, int) or not 512 <= sb <= 8192 or sb % 256:
            raise ValueError("invalid bounded online SB target")
        for q, value in wm.items():
            if not isinstance(value, int) or not 1 <= value <= 4096:
                raise ValueError("invalid work_mem")
            if q in previous and value != previous[q]:
                raise ValueError("continuing AP work_mem cannot change")
        if selected["predicted_tps"] <= 0:
            raise ValueError("numeric positive frozen TPS required")
        previous = wm
    return rows


def summary(values, predicted):
    if not values or any(v <= 0 for v in values):
        raise ValueError("empty/invalid measured TPS is not a result")
    mean = statistics.mean(values)
    return {"repeat_TPS": values, "observed_mean_TPS": mean,
            "observed_min_TPS": min(values), "observed_max_TPS": max(values),
            "observed_sample_std_TPS": statistics.stdev(values) if len(values) > 1 else None,
            "frozen_predicted_TPS": predicted,
            "prediction_minus_actual_relative": (predicted - mean) / mean}


class AP:
    def __init__(self, run, q, wm):
        self.run, self.q, self.wm = run, q, wm
        self.application = run.tag + "_q%d" % q
        self.result = None
        self.closed = False
        self.errors = (run.out / ("q%d.worker.stderr" % q)).open("w")
        self.proc = subprocess.Popen(
            ["runuser", "-u", "omm", "--", "env", "LD_LIBRARY_PATH=/opt/openGauss/lib",
             "/usr/bin/python3", "-u", str(run.scratch / "worker.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors, text=True)
        self.send({"database": "h5_tpch", "postmaster_pid": run.sb.pid,
                   "application_name": self.application, "timeout_seconds": 1800,
                   "temp_limit_mib": 12288})
        identity = receive(self.proc)
        if identity["status"] != "connected":
            raise RuntimeError("AP connection failed")
        self.identity = identity
        self.started_ns = time.monotonic_ns()
        dump(run.out / ("q%d.START.json" % q), {
            "identity": identity, "application_name": self.application,
            "work_mem_mib": wm,
            "sql_sha256": getattr(run, "sql_digests", SQL_SHA256)[q],
            "request_sent_monotonic_ns": self.started_ns})
        self.send({"command": "execute", "sql": run.sql[q], "wm_mib": wm})

    def send(self, row):
        self.proc.stdin.write(json.dumps(row) + "\n")
        self.proc.stdin.flush()

    def poll(self):
        if self.result is None and select.select([self.proc.stdout], [], [], 0)[0]:
            self.result = receive(self.proc)
            dump(self.run.out / ("q%d.RESULT.json" % self.q), self.result)
        if self.result is None and self.proc.poll() is not None:
            raise RuntimeError("AP worker exited without a result")
        return self.result

    def stop(self, reason):
        if self.closed:
            return
        dump(self.run.out / ("q%d.STOP.json" % self.q), {
            "reason": reason, "requested_monotonic_ns": time.monotonic_ns(),
            "full_AP_lifecycle_validation": False})
        if not self.poll():
            # Match both our exact role/application identity and backend PID.
            self.run.client.json("postgres", """
SELECT coalesce(json_agg(pg_cancel_backend(pid)),'[]'::json)
FROM pg_stat_activity WHERE pid=%d AND application_name='%s'
""" % (self.identity["backend_pid"], self.application))
            deadline = time.monotonic() + 30
            while not self.poll():
                if time.monotonic() > deadline:
                    raise TimeoutError("owned AP did not cancel")
                time.sleep(.1)
        self.send({"command": "close"})
        self.proc.wait(timeout=15)
        self.errors.close()
        self.closed = True


class Run:
    def __init__(self, args, client, sb, scratch, sql):
        self.args, self.client, self.sb = args, client, sb
        self.scratch, self.sql, self.out = scratch, sql, args.out
        self.tag = "jmt_mix_" + uuid.uuid4().hex[:10]
        self.ap, self.drivers, self.logs = {}, [], []
        self.resources = []
        self.base = self.surge = None
        self.surge_gate = None

    def driver(self, script, terminals, name, gate=None):
        path = self.out / (name + ".sysbench.log")
        stream = path.open("w")
        env = dict(os.environ, PGOPTIONS="-c default_transaction_read_only=on -c query_dop=1",
                   PGAPPNAME=self.tag + "_" + name)
        if gate:
            env["JMT_SURGE_MONOTONIC_NS"] = str(gate)
        proc = subprocess.Popen(
            ["runuser", "-u", "omm", "--"] + driver_argv(
                script, terminals, getattr(self.args, "driver_seconds", 1800), 441112),
            stdout=stream, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        self.drivers.append(proc)
        self.logs.append((stream, path))
        return proc

    def guard(self):
        free = shutil.disk_usage("/opt/openGauss/data").free
        available = mem_available()
        status = Path("/proc/%d/status" % self.sb.pid).read_text()
        rss = int(re.search(r"^VmRSS:\s+(\d+)", status, re.M)[1]) * 1024
        self.resources.append({"monotonic_ns": time.monotonic_ns(),
                               "disk_free_bytes": free, "MemAvailable_bytes": available,
                               "whole_gaussdb_RSS_bytes": rss})
        if free < 8 * 2**30 or available < 3 * 2**30:
            raise RuntimeError("mixed-load aggregate disk/RAM safety guard triggered")
        if any(p.poll() is not None for p in self.drivers):
            raise RuntimeError("TP driver exited before planned stop")
        for ap in self.ap.values():
            if ap.poll() is not None:
                raise RuntimeError("Q%d ended before stage window finished" % ap.q)
        if int(Path("/opt/openGauss/data/postmaster.pid").read_text().splitlines()[0]) != self.sb.pid:
            raise RuntimeError("postmaster changed")

    def wait(self, until):
        while time.monotonic_ns() < until:
            self.guard()
            time.sleep(min(1, max(0, (until - time.monotonic_ns()) / 1e9)))
        self.guard()

    def members(self, expected_ap):
        if sorted(self.ap) != sorted(expected_ap):
            raise RuntimeError("wrong requested AP membership")
        rows = self.client.json("postgres", """
SELECT coalesce(json_agg(x),'[]'::json) FROM
(SELECT application_name,pid,state FROM pg_stat_activity
 WHERE datname='h5_tpch' AND application_name LIKE '%s_q%%')x
""" % self.tag)
        active = {r["application_name"] for r in rows if r["state"] == "active"}
        expected = {ap.application for ap in self.ap.values()}
        if active != expected:
            raise RuntimeError("actual active AP membership differs: %r != %r" % (active, expected))
        tids = tids_for(self.client, self.tag + "_base")
        if len(tids) != 128:
            raise RuntimeError("baseline TP backend membership changed")
        if self.surge:
            extra = tids_for(self.client, self.tag + "_plus16")
            if len(extra) != 16 or set(tids) & set(extra):
                raise RuntimeError("S5's distinct +16 backend membership changed")
            tids += extra
        return rows, sorted(tids)

    def window(self, stage, repeat, expected_ap, gate_surge=False):
        observer = TPWindowObserver("/opt/openGauss/bin/gaussdb", self.sb.pid, "/dev/nvme0n1")
        try:
            hook = getattr(self, "before_scored_window", None)
            if hook is not None:
                hook(stage, repeat, expected_ap, gate_surge, observer)
            start = time.monotonic_ns() + (10 if gate_surge else 2) * 10**9
            end = start + self.args.window_seconds * 10**9
            if gate_surge:
                self.surge_gate = start
                self.surge = self.driver(self.scratch / "gated.lua", 16, "plus16", start)
                deadline = start - 2 * 10**9
                while len(tids_for(self.client, self.tag + "_plus16")) != 16:
                    self.wait(time.monotonic_ns() + 100_000_000)
                    if time.monotonic_ns() > deadline:
                        raise TimeoutError("gated +16 not preconnected before S5")
            members_before, tids = self.members(expected_ap)
            observer.enroll(tids)
            observer.arm(start, end)
            self.wait(start)
            cpu_tids = sorted(tids + [ap.identity["linux_tid"] for ap in self.ap.values()])
            before = cpu_snapshot(self.sb.pid, cpu_tids, [p.pid for p in self.drivers])
            self.wait(end)
            after = cpu_snapshot(self.sb.pid, cpu_tids, [p.pid for p in self.drivers])
            members_after, after_tids = self.members(expected_ap)
            if after_tids != tids:
                raise RuntimeError("TP backend set changed during scored window")
            result = observer.result()
            tx = result["committed_transactions"]
            if tx <= 0:
                raise RuntimeError("no TP commits; not a numeric valid TPS")
            result.update({
                "stage": stage, "repeat": repeat,
                "shared_buffers_mib": self.sb.current_mib,
                "AP_work_mem_mib": {str(q): ap.wm for q, ap in self.ap.items()},
                "AP_members_before": members_before, "AP_members_after": members_after,
                "CPU_bracket": {"before": before, "after": after,
                                "qualification": "Own schedstat endpoints, not exact kernel window"},
                "AP_CPU_seconds": {str(q): (
                    after["backend_cpu_ns"][str(ap.identity["linux_tid"])]
                    - before["backend_cpu_ns"][str(ap.identity["linux_tid"])]) / 1e9
                    for q, ap in self.ap.items()},
                "TP_CPU_seconds": sum(
                    after["backend_cpu_ns"][str(t)] - before["backend_cpu_ns"][str(t)]
                    for t in tids) / 1e9,
                "AP_elapsed_at_window_start_seconds": {
                    str(q): (start - ap.started_ns) / 1e9 for q, ap in self.ap.items()},
                "S5_first_event_gate_ns": self.surge_gate,
                "full_AP_lifecycle_validation": False})
            dump(self.out / ("%s_r%02d.WINDOW.json" % (stage, repeat)), result)
            print(json.dumps({"stage": stage, "repeat": repeat,
                              "actual_TPS": result["observed_committed_TPS"],
                              "AP_CPU_seconds": result["AP_CPU_seconds"]}), flush=True)
            return result["observed_committed_TPS"]
        finally:
            observer.close()

    def cleanup(self):
        errors = []
        for q in list(self.ap):
            try:
                self.ap[q].stop("bounded five-stage window run ended; planned owned-query cancellation")
                del self.ap[q]
            except Exception as exc:
                errors.append("Q%d cleanup: %s" % (q, exc))
        for proc in self.drivers:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGINT)
        for proc in self.drivers:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
        driver_logs = []
        for stream, path in self.logs:
            stream.flush()
            stream.close()
            text = path.read_text()
            # Interval reports are still present when a planned SIGINT omits
            # the final summary. Never treat a missing summary as zero errors.
            intervals = re.findall(r"err/s:\s*([0-9.]+)", text)
            ignored = re.search(r"ignored errors:\s*(\d+)", text)
            row = {"path": str(path), "sha256": digest(path),
                   "interval_error_rates": [float(x) for x in intervals],
                   "final_ignored_errors": int(ignored[1]) if ignored else None}
            if not intervals or any(float(x) != 0 for x in intervals):
                errors.append("missing/error TP interval reports: " + path.name)
            driver_logs.append(row)
        dump(self.out / "DRIVER_VALIDATION.json", {"logs": driver_logs, "errors": errors,
             "intentional_driver_stop": True, "missing_final_summary_is_not_zero_errors": True})
        dump(self.out / "RESOURCE_SAMPLES.json", self.resources)
        return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--recommendation", type=Path, required=True)
    parser.add_argument("--sql-dir", type=Path, required=True)
    parser.add_argument("--window-seconds", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--settle-seconds", type=int, default=30)
    args = parser.parse_args()
    if not 10 <= args.window_seconds <= 60 or not 1 <= args.repeats <= 5 or not 15 <= args.settle_seconds <= 120:
        raise ValueError("bounded short-window validation arguments required")
    document = json.loads(args.recommendation.read_text())
    stages = validate_recommendation(document)
    sql = {}
    for q, sha in SQL_SHA256.items():
        path = args.sql_dir / ("q%d.sql" % q)
        if digest(path) != sha:
            raise ValueError("original full SQL changed")
        sql[q] = readonly_query(path.read_text())
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=20)
    active = client.json("postgres", """
SELECT coalesce(json_agg(application_name),'[]'::json) FROM pg_stat_activity
WHERE pid<>pg_backend_pid() AND state='active'
AND application_name NOT IN ('JobScheduler','PercentileJob','Asp','workload')
""")
    if active:
        raise RuntimeError("another client workload is active")
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    if machine["machine_fingerprint"] != document["machine_fingerprint"]:
        raise ValueError("recommendation and actual machine differ")
    dump(args.out / "MACHINE.json", machine)
    shutil.copyfile(str(args.recommendation), str(args.out / "FROZEN_RECOMMENDATION.json"))
    dump(args.out / "POLICY.json", {
        "created_at": datetime.now().astimezone().isoformat(),
        "recommendation_sha256": digest(args.recommendation),
        "runner_sha256": digest(Path(__file__)), "SQL_sha256": SQL_SHA256,
        "TP_profile": "current_16x1M_sysbench_readonly_omm_local_socket",
        "window_seconds": args.window_seconds, "repeats": args.repeats,
        "stage_settle_seconds": args.settle_seconds,
        "AP_continues_across_stages": True,
        "S5": "128 warmed, +16 first-event gated at first measurement window",
        "scope": "single continuous five-stage AP overlap trajectory, repeated windows, NOT independent lifecycle repeats",
        "full_AP_lifecycle_validation": False,
        "AP_completion": "owned AP cancelled at bounded run end; Q2 cancelled at S5 departure",
        "safety": {"min_disk_free_GiB": 8, "min_MemAvailable_GiB": 3,
                   "per_AP_temp_limit_MiB": 12288, "physical_peak_safety_qualified": False},
        "probe_overhead_qualified": False})
    sb = OnlineSB(client, args.out)
    results, control, error, cleanup_errors = [], None, None, []
    with tempfile.TemporaryDirectory(prefix="jmt-mixed-", dir="/dev/shm") as scratch:
        scratch = Path(scratch)
        scratch.chmod(0o755)
        for name, content in (("worker.py", (ROOT / "tools/current_ap_worker.py").read_text()),
                              ("gated.lua", GATED_LUA)):
            (scratch / name).write_text(content)
            (scratch / name).chmod(0o644)
        run = Run(args, client, sb, scratch, sql)
        try:
            sb.set(stages[0]["result"]["selected"]["shared_buffers_mb"])
            run.base = run.driver("/usr/share/sysbench/oltp_read_only.lua", 128, "base")
            run.wait(time.monotonic_ns() + 40 * 10**9)
            control = run.window("TP_ONLY_BEFORE", 1, [])
            for i, stage in enumerate(stages):
                selected = stage["result"]["selected"]
                for q in list(run.ap):
                    if q not in stage["AP_queries"]:
                        run.ap[q].stop("PPT stage %s AP departure" % stage["stage"])
                        del run.ap[q]
                sb.set(selected["shared_buffers_mb"])
                for q, wm in selected["work_mem"]:
                    if q not in run.ap:
                        run.ap[q] = AP(run, q, wm)
                run.wait(time.monotonic_ns() + args.settle_seconds * 10**9)
                values = [run.window(stage["stage"], r, stage["AP_queries"],
                                     gate_surge=(i == 4 and r == 1))
                          for r in range(1, args.repeats + 1)]
                result = {"stage": stage["stage"], "AP_queries": stage["AP_queries"],
                          "shared_buffers_mib": selected["shared_buffers_mb"],
                          "work_mem": selected["work_mem"],
                          **summary(values, selected["predicted_tps"])}
                dump(args.out / (stage["stage"] + ".SUMMARY.json"), result)
                results.append(result)
        except BaseException as exc:
            error = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            try:
                cleanup_errors = run.cleanup()
            finally:
                restored = sb.restore()
                dump(args.out / "MIXED_TPS_RESULT.json", {
                    "schema": "jmt.real-five-stage-mixed-overlap-TPS/v1",
                    "created_at": datetime.now().astimezone().isoformat(),
                    "recommendation_sha256": digest(args.recommendation),
                    "rows": results, "TP_only_control_before_TPS": control,
                    "error": error, "cleanup_errors": cleanup_errors,
                    "complete": len(results) == 5 and error is None and not cleanup_errors,
                    "restoration_verified": restored["config_restored_byte_exact"]
                                             and restored["server_status_matches_original"],
                    "full_AP_lifecycle_validation": False,
                    "configuration_optimality_validated": False,
                    "automatic_recommendation_qualified": False})


if __name__ == "__main__":
    main()
