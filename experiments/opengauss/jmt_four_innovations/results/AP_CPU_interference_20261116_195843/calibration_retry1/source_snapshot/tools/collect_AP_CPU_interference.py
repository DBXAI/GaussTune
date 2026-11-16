#!/usr/bin/env python3
"""Separate read-only scan/aggregate interference calibration, NOT target fit."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"), str(ROOT/"vendor"), str(ROOT/"tools")]
from jmt.common.db import Gsql, readonly_query, extract_json
from jmt.common.machine import collect_machine
from collect_current_tp_matrix import OnlineSB, dump, digest, validate_cpu_window
from measure_current_mixed_five_stages import Run, AP
from validate_capacity_TPS_live import DiagnosticRun
from validate_configuration_TPS_matrix import interrupt_for_cleanup
from jmt.innovation_4_performance.colocated_tp import summarize_point

# Explicit microbenchmarks, not relabeled TPC-H queries and not target variants.
# Scalar aggregates keep modeled private memory tiny; all inputs are scanned.
SQL = {
    101: """SELECT count(*),sum(l_extendedprice*l_discount),sum(l_quantity)
            FROM lineitem WHERE l_shipdate>=DATE '1994-01-01'
            AND l_shipdate<DATE '1995-01-01'""",
    102: """SELECT count(*),sum(o_totalprice)
            FROM orders WHERE o_orderdate>=DATE '1993-01-01'""",
    103: """SELECT count(*),sum(ps_supplycost*ps_availqty) FROM partsupp""",
}
GROUPS = ((101,), (102,), (103,), (101, 102), (101, 103), (102, 103))


def point_from_window(raw):
    copied = dict(raw, CPU_snapshot_before=raw["CPU_bracket"]["before"],
                  CPU_snapshot_after=raw["CPU_bracket"]["after"])
    validate_cpu_window(copied)
    return summarize_point(copied, topology="TP128")


class CalibrationRun(DiagnosticRun):
    # Keep safety guards, but no extra whole-cache metadata scans around CPU
    # windows. These scalar microbenchmarks do not use AP capacity predictions.
    def window(self, *args, **kwargs):
        return Run.window(self, *args, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-independent-calibration", action="store_true", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.window_seconds, args.driver_seconds, args.phase_model = 20, 1800, None
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=30)
    active = client.json("postgres", """SELECT coalesce(json_agg(application_name),'[]'::json)
        FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND state='active'
        AND application_name NOT IN ('JobScheduler','PercentileJob','Asp','workload')""")
    if active:
        raise ValueError("other active client workloads present")
    machine = collect_machine(
        Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
        "local-source-b314224-not-asserted-binary-commit")
    dump(args.out/"MACHINE.json", machine)
    sql = {q: readonly_query(text) for q, text in SQL.items()}
    hashes = {q: hashlib.sha256(text.encode()).hexdigest() for q, text in sql.items()}
    queries = args.out/"queries"
    queries.mkdir()
    plans = {}
    for q, text in sql.items():
        (queries/("q%d.sql" % q)).write_text(text + "\n")
        plan = client.explain("h5_tpch", text, 64)
        if plan["exit_code"]:
            raise RuntimeError("calibration blind EXPLAIN failed")
        plans[str(q)] = extract_json(plan["stdout"])
    dump(args.out/"BLIND_PLANS.json", plans)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    sources = [Path(__file__).resolve(), ROOT/"tools/measure_current_mixed_five_stages.py",
               ROOT/"tools/collect_current_tp_matrix.py", ROOT/"tools/current_ap_worker.py",
               ROOT/"tools/validate_capacity_TPS_live.py",
               ROOT/"src/jmt/common/process_cpu.py",
               ROOT/"src/jmt/innovation_4_performance/tp_window_observer.py"]
    for source in sources:
        target = args.out/"source_snapshot"/source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    dump(args.out/"POLICY.json", {
        "created_at": datetime.now().astimezone().isoformat(),
        "boot_id": boot, "machine_fingerprint": machine["machine_fingerprint"],
        "groups": GROUPS, "SQL_sha256s": hashes, "shared_buffers_mib": 4096,
        "training_repeat": 1, "heldout_repeat": 2,
        "TP128_one_continuous_driver": True,
        "independent_fresh_AP_episodes_not_independent_TP_processes": True,
        "AP_settle_seconds": 10, "window_seconds": 20,
        "counterbalanced_AP_order": True, "work_mem_mib": 64,
        "original_target_queries_or_variants_used_for_training": False,
        "bounded_EA_JSON_instrumented_microbenchmarks_not_full_lifecycle_truth": True,
        "source_sha256": {str(s.relative_to(ROOT)): digest(s) for s in sources}})
    sb = OnlineSB(client, args.out)
    rows, controls, error, cleanup_errors = [], [], None, []
    original_out = args.out
    with tempfile.TemporaryDirectory(prefix="jmt-cpu-cal-", dir="/dev/shm") as scratch:
        scratch = Path(scratch)
        scratch.chmod(0o755)
        shutil.copy2(ROOT/"tools/current_ap_worker.py", scratch/"worker.py")
        (scratch/"worker.py").chmod(0o644)
        run = CalibrationRun(args, client, sb, scratch, sql)
        run.sql_digests = hashes
        try:
            sb.set(4096)
            run.base = run.driver("/usr/share/sysbench/oltp_read_only.lua", 128, "base")
            run.wait(time.monotonic_ns()+40*10**9)
            for repeat in (1, 2):
                control_dir = original_out/("control_r%02d" % repeat)
                control_dir.mkdir()
                run.out = control_dir
                run.window("TP_ONLY", repeat, [])
                control = json.loads((control_dir/("TP_ONLY_r%02d.WINDOW.json" % repeat)).read_text())
                point = point_from_window(control)
                controls.append({"repeat": repeat, "directory": control_dir.name, "point": point})
                for group in (GROUPS if repeat == 1 else tuple(reversed(GROUPS))):
                    key = "_".join(map(str, group))+"_r%02d" % repeat
                    case_dir = original_out/key
                    case_dir.mkdir()
                    run.out = case_dir
                    # Freeze identity and role before any AP execution.
                    dump(case_dir/"PRE_AP.json", {
                        "key": key, "role": "calibration" if repeat == 1 else "heldout",
                        "AP_queries": group, "AP_sql_sha256s": [hashes[q] for q in group],
                        "control": controls[-1], "saved_monotonic_ns": time.monotonic_ns()})
                    for q in group:
                        run.ap[q] = AP(run, q, 64)
                    run.wait(time.monotonic_ns()+10*10**9)
                    run.window("CAL", repeat, list(group))
                    raw = json.loads((case_dir/("CAL_r%02d.WINDOW.json" % repeat)).read_text())
                    for q in list(run.ap):
                        run.ap[q].stop("bounded independent CPU calibration window ended")
                        del run.ap[q]
                    measured = point_from_window(dict(raw, CPU_bracket={
                        name: dict(snap, backend_cpu_ns={
                            str(t): snap["backend_cpu_ns"][str(t)] for t in raw["backend_linux_tids"]})
                        for name, snap in raw["CPU_bracket"].items()}))
                    row = {
                        "key": key, "role": "calibration" if repeat == 1 else "heldout",
                        "boot_id": boot, "postmaster_start_time": sb.original_status["postmaster_start_time"],
                        "machine_fingerprint": machine["machine_fingerprint"],
                        "shared_buffers_mib": 4096, "TP_terminals": 128,
                        "AP_sql_sha256s": [hashes[q] for q in group],
                        "AP_CPU_cores": sum(raw["AP_CPU_seconds"].values())/raw["scored_seconds"],
                        "control_backend_cpu_seconds_per_tx": point["backend_cpu_seconds_per_tx"],
                        "mixed_backend_cpu_seconds_per_tx": measured["backend_cpu_seconds_per_tx"],
                        "CPU_endpoints_within_100ms": True, "transaction_count_rescaled": False,
                        "control_finished_before_episode": (
                            point["calibration_window_end_ns"] < min(
                                json.loads((case_dir/("q%d.START.json" % q)).read_text())[
                                    "request_sent_monotonic_ns"] for q in group)),
                        "window_sha256": digest(case_dir/("CAL_r%02d.WINDOW.json" % repeat)),
                    }
                    dump(case_dir/"CPU_TRAINING_ROW.json", row)
                    rows.append(row)
                    run.wait(time.monotonic_ns()+5*10**9)
        except BaseException as exc:
            error = type(exc).__name__+": "+str(exc)
            raise
        finally:
            run.out = original_out
            try:
                cleanup_errors = run.cleanup()
            finally:
                restored = sb.restore()
                dump(original_out/"COLLECTION.json", {
                    "created_at": datetime.now().astimezone().isoformat(),
                    "rows": rows, "controls": controls, "error": error,
                    "cleanup_errors": cleanup_errors, "complete": (
                        len(rows) == 12 and error is None and not cleanup_errors),
                    "restoration_verified": restored["config_restored_byte_exact"]
                        and restored["server_status_matches_original"]})


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt_for_cleanup)
    main()
