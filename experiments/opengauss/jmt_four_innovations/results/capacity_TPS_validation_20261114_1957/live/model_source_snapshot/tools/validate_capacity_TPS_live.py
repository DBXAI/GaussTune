#!/usr/bin/env python3
"""Bounded true Sysbench + full AP overlap validation, no permanent tuning.

Reuses proven enrolled-commit BPF windows and owned AP worker cancellation.
Added resource/counter/cache snapshots are outside scored windows and retain
their own timestamps; asynchronously published counters are NOT exact truth.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.common.db import Gsql, readonly_query
from jmt.innovation_4_performance.tp_window_observer import TPWindowObserver
from collect_current_tp_matrix import OnlineSB, dump, digest, native_snapshot, validate_cpu_window
from collect_current_tp_surge import GATED_LUA
from measure_current_mixed_five_stages import Run, AP, summary, validate_recommendation
from regenerate_resource_pilot import SQL_SHA256


class DiagnosticRun(Run):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.next_memory_check = 0

    def guard(self):
        super().guard()
        now = time.monotonic_ns()
        if now >= self.next_memory_check:
            self.next_memory_check = now + 5 * 10 ** 9
            rows = self.client.json("postgres", """SELECT json_agg(t) FROM
                (SELECT memorytype,memorymbytes FROM pg_total_memory_detail
                 WHERE memorytype IN ('dynamic_used_memory','max_dynamic_memory',
                                      'process_used_memory','max_process_memory'))t""")
            values = {r["memorytype"]: r["memorymbytes"] for r in rows}
            self.resources.append({"monotonic_ns": now, "native_memory_mib": values})
            if values["dynamic_used_memory"] > .93 * values["max_dynamic_memory"]:
                raise RuntimeError("owned pilot stopped before native dynamic protection margin")

    def diagnostic_snapshot(self):
        started = time.monotonic_ns()
        tp = native_snapshot(self.client, "h5_tpcc")
        memory = self.client.json("postgres", """SELECT json_agg(t) FROM
            (SELECT memorytype,memorymbytes FROM pg_total_memory_detail
             WHERE memorytype IN ('dynamic_used_memory','max_dynamic_memory',
                                  'process_used_memory','max_process_memory'))t""")
        cache = self.client.json("postgres", """SELECT json_agg(t) FROM (
            SELECT reldatabase,count(*) AS resident_pages FROM pg_buffercache_pages()
            WHERE isvalid GROUP BY reldatabase)t""")
        members = self.client.json("postgres", """SELECT coalesce(json_agg(t),'[]'::json)
            FROM (SELECT application_name,pid,state,query_start FROM pg_stat_activity
            WHERE application_name LIKE '%s_q%%')t""" % self.tag)
        return {"started_ns": started, "finished_ns": time.monotonic_ns(),
                "published_TP_counters": tp, "native_memory": memory,
                "resident_pages_by_database": cache, "AP_members": members,
                "not_exact_commit_window_or_owned_IO": True}

    def window(self, stage, repeat, expected_ap, gate_surge=False):
        before = self.diagnostic_snapshot()
        tps = super().window(stage, repeat, expected_ap, gate_surge)
        after = self.diagnostic_snapshot()
        path = self.out / ("%s_r%02d.WINDOW.json" % (stage, repeat))
        raw = json.loads(path.read_text())
        cpu = raw["CPU_bracket"]
        try:
            validate_cpu_window({
                "measurement_window": raw["measurement_window"],
                "CPU_snapshot_before": cpu["before"], "CPU_snapshot_after": cpu["after"]})
            timing = {"CPU_100ms_boundary_screen_passed": True}
        except ValueError as error:
            timing = {"CPU_100ms_boundary_screen_passed": False, "reason": str(error)}
        dump(self.out / ("%s_r%02d.INTERMEDIATES.json" % (stage, repeat)), {
            "before": before, "after": after, **timing,
            "counter_publication_lag_not_corrected_by_empirical_multiplier": True})
        return tps


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-bounded-mixed-validation", action="store_true", required=True)
    p.add_argument("--prediction", type=Path, required=True)
    p.add_argument("--sql-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--window-seconds", type=int, default=15)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--settle-seconds", type=int, default=15)
    args = p.parse_args()
    if (not 10 <= args.window_seconds <= 30 or not 1 <= args.repeats <= 3
            or not 15 <= args.settle_seconds <= 60):
        raise ValueError("bounded overlap-window policy required")
    prediction = json.loads(args.prediction.read_text())
    if prediction["schema"] != "jmt.experimental-fixed-control-capacity-TPS-validation/v1":
        raise ValueError("explicit experimental fixed-control prediction required")
    stages = validate_recommendation(prediction)
    sql = {}
    for q, digest_value in SQL_SHA256.items():
        path = args.sql_dir / ("q%d.sql" % q)
        if digest(path) != digest_value:
            raise ValueError("full AP SQL identity changed")
        sql[q] = readonly_query(path.read_text())
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=20)
    active = client.json("postgres", """SELECT coalesce(json_agg(application_name),'[]'::json)
        FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND state='active'
        AND application_name NOT IN ('JobScheduler','PercentileJob','Asp','workload')""")
    if active:
        raise ValueError("another client workload is active")
    shutil.copy2(args.prediction, args.out / "FROZEN_TPS_PREDICTION.json")
    sources = [Path(__file__).resolve(), ROOT / "tools/measure_current_mixed_five_stages.py",
               ROOT / "tools/collect_current_tp_matrix.py", ROOT / "tools/current_ap_worker.py",
               ROOT / "src/jmt/innovation_4_performance/tp_window_observer.py"]
    for source in sources:
        target = args.out / "model_source_snapshot" / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    dump(args.out / "POLICY.json", {
        "created_at": datetime.now().astimezone().isoformat(),
        "prediction_sha256": digest(args.prediction),
        "new_statistical_fit_or_target_label_used": False,
        "fixed_WMs_not_step3_optima": True, "full_AP_SQL_not_simplified": True,
        "window_seconds": args.window_seconds, "repeats": args.repeats,
        "AP_continues_across_stages": True,
        "stage_semantics": "one continuous trajectory, repeat windows not independent lifecycle repeats",
        "S5": "128 warmed +16 first-event gated, not flat 144",
        "safety": "8GiB disk, 3GiB MemAvailable, native dynamic used<=93% threshold; restore SB exactly",
        "cache_and_native_counters": "diagnostic explicit brackets, not exact TP-owned IO truth",
        "source_code": {str(s.relative_to(ROOT)): digest(s) for s in sources},
        "probe_overhead_qualified": False, "optimality_validated": False})
    sb = OnlineSB(client, args.out)
    rows, error, cleanup_errors = [], None, []
    with tempfile.TemporaryDirectory(prefix="jmt-capacity-tps-", dir="/dev/shm") as scratch:
        scratch = Path(scratch)
        scratch.chmod(0o755)
        for name, content in (("worker.py", (ROOT / "tools/current_ap_worker.py").read_text()),
                              ("gated.lua", GATED_LUA)):
            (scratch / name).write_text(content)
            (scratch / name).chmod(0o644)
        run = DiagnosticRun(args, client, sb, scratch, sql)
        controls = []
        try:
            sb.set(5120)
            run.base = run.driver("/usr/share/sysbench/oltp_read_only.lua", 128, "base")
            run.wait(time.monotonic_ns() + 40 * 10 ** 9)
            for target in (5120, 5376):
                sb.set(target)
                run.wait(time.monotonic_ns() + 15 * 10 ** 9)
                tps = run.window("TP_ONLY_%d" % target, 1, [])
                controls.append({"SB_mib": target, "TPS": tps})
            for i, stage in enumerate(stages):
                selected = stage["result"]["selected"]
                for q in list(run.ap):
                    if q not in stage["AP_queries"]:
                        run.ap[q].stop("PPT stage departure")
                        del run.ap[q]
                sb.set(selected["shared_buffers_mb"])
                for q, wm in selected["work_mem"]:
                    if q not in run.ap:
                        run.ap[q] = AP(run, q, wm)
                run.wait(time.monotonic_ns() + args.settle_seconds * 10 ** 9)
                values = [run.window(stage["stage"], r, stage["AP_queries"],
                                     gate_surge=(i == 4 and r == 1))
                          for r in range(1, args.repeats + 1)]
                row = {"stage": stage["stage"], "SB_mib": selected["shared_buffers_mb"],
                       "AP_work_mem_mib": dict(selected["work_mem"]),
                       **summary(values, selected["predicted_tps"])}
                dump(args.out / (stage["stage"] + ".SUMMARY.json"), row)
                rows.append(row)
        except BaseException as exc:
            error = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            try:
                cleanup_errors = run.cleanup()
            finally:
                restored = sb.restore()
                dump(args.out / "CAPACITY_TPS_VALIDATION.json", {
                    "schema": "jmt.actual-capacity-TPS-overlap-validation/v1",
                    "created_at": datetime.now().astimezone().isoformat(),
                    "prediction_sha256": digest(args.prediction),
                    "TP_only_controls": controls, "stages": rows,
                    "error": error, "cleanup_errors": cleanup_errors,
                    "complete": len(rows) == 5 and error is None and not cleanup_errors,
                    "restoration_verified": restored["config_restored_byte_exact"]
                                             and restored["server_status_matches_original"],
                    "full_AP_lifecycle_or_optimal_SB_verified": False,
                    "fixed_controls_not_recommended_WMs_validated": True,
                    "automatic_recommendation_qualified": False})


if __name__ == "__main__":
    main()
