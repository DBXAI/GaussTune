#!/usr/bin/env python3
"""Real bounded SB x WM matrix; independently restart AP episodes, restore SB."""
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
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.common.db import Gsql, readonly_query, extract_json
from jmt.innovation_2_spill.ap_plan_model import blind_root, _shape
from jmt.innovation_4_performance.configuration_matrix import QUERIES, predict_configuration
from collect_current_tp_matrix import OnlineSB, dump, digest
from measure_current_mixed_five_stages import AP
from validate_capacity_TPS_live import DiagnosticRun
from prepare_configuration_TPS_matrix import incoming_cache


def interrupt_for_cleanup(signum, frame):
    """SIGTERM must run the same owned-workload/config cleanup as Ctrl-C."""
    raise KeyboardInterrupt("matrix interrupted by signal %d" % signum)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-bounded-matrix", action="store_true", required=True)
    p.add_argument("--frozen", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.window_seconds, args.phase_model, args.driver_seconds = 20, None, 3600
    frozen = json.loads(args.frozen.read_text())
    if frozen["schema"] != "jmt.frozen-configuration-TPS-matrix/v1":
        raise ValueError("wrong matrix schema")
    if frozen["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
        raise ValueError("machine rebooted after freezing; do not join monotonic timelines")
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=60)
    if client.json("postgres", "SELECT json_build_object('start',pg_postmaster_start_time())")[
            "start"] != frozen[
            "postmaster_start_time"]:
        raise ValueError("database restarted after freezing")
    active = client.json("postgres", """SELECT coalesce(json_agg(application_name),'[]'::json)
        FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND state='active'
        AND application_name NOT IN ('JobScheduler','PercentileJob','Asp','workload')""")
    if active:
        raise ValueError("other client workload active")
    sql = {}
    for query in QUERIES:
        path = args.frozen.parent / "queries" / ("q%d.sql" % query)
        if digest(path) != frozen["AP_profiles"]["LOW"][str(query)]["sql_sha256"]:
            raise ValueError("frozen original SQL hash mismatch")
        sql[query] = readonly_query(path.read_text())
    shutil.copy2(args.frozen, args.out / "FROZEN_MATRIX.json")
    sources = [Path(__file__).resolve(), ROOT / "tools/prepare_configuration_TPS_matrix.py",
               ROOT / "tools/validate_capacity_TPS_live.py",
               ROOT / "tools/measure_current_mixed_five_stages.py",
               ROOT / "tools/collect_current_tp_matrix.py", ROOT / "tools/current_ap_worker.py"]
    sources += [ROOT / "src/jmt/innovation_4_performance" / name for name in (
        "configuration_matrix.py", "capacity_tps_validation.py", "preview_cost.py",
        "config_evaluation.py", "tp_window_observer.py")]
    sources += [ROOT / "src/jmt/innovation_3_search/cardinality_cache_capacity.py",
                ROOT / "src/jmt/common/process_cpu.py"]
    if frozen.get("fresh_TP_points") is not None:
        sources += [ROOT / "src/jmt/innovation_4_performance/tp_baseline_rebase.py",
                    ROOT / "src/jmt/innovation_4_performance/colocated_tp.py"]
    if frozen.get("CPU_interference_model") is not None:
        sources += [ROOT / "src/jmt/innovation_4_performance/cpu_interference.py",
                    ROOT / "src/jmt/innovation_4_performance/phase_bridge.py",
                    ROOT / "tools/CPU_interference_run.py"]
    if frozen.get("CPU_queue_feedback_policy") is not None:
        sources += [ROOT / "src/jmt/innovation_4_performance/cpu_queue_feedback.py",
                    ROOT / "src/jmt/innovation_4_performance/live_tp_reference.py",
                    ROOT / "src/jmt/innovation_4_performance/cpu_surface.py",
                    ROOT / "src/jmt/innovation_4_performance/capped_device.py",
                    ROOT / "tools/CPU_queue_feedback_run.py", ROOT / "tools/diagnose_AP_runnable.py"]
    if frozen.get("prior_CPU_service_policy") is not None:
        sources += [ROOT / "src/jmt/innovation_4_performance/prior_cpu_service.py",
                    ROOT / "tools/prior_CPU_service_run.py"]
    for source in sources:
        target = args.out / "source_snapshot" / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    dump(args.out / "POLICY.json", {
        "created_at": datetime.now().astimezone().isoformat(),
        "boot_id": frozen["boot_id"], "postmaster_start_time": frozen["postmaster_start_time"],
        "frozen_sha256": digest(args.frozen), "AP_cohort": list(QUERIES),
        "window_seconds": 20, "independent_AP_episodes_per_configuration": 2,
        "AP_settle_seconds": 15, "TP_settle_after_SB_seconds": 20,
        "TP_recovery_after_AP_seconds": 10,
        "AP_starts_fresh_each_episode_WM_not_changed_in_running_query": True,
        "metadata_statement_timeout_seconds": 60,
        "TP_driver_max_seconds": args.driver_seconds,
        "metadata_queries_outside_scored_commit_CPU_device_window": True,
        "TP128_same_process_and_seed_throughout": True,
        "case_predictions_use_only_frozen_costs_and_pre_AP_cache_state": True,
        "TP_only_controls_diagnostic_not_fit": frozen.get("CPU_queue_feedback_policy") is None,
        "prior_TP_controls_used_as_independent_inputs_not_future_mixed_labels":
            frozen.get("CPU_queue_feedback_policy") is not None,
        "prior_CPU_service_policy": frozen.get("prior_CPU_service_policy"),
        "cache_drop_or_clean_state_or_randomized_causal_effect_verified": False,
        "block_orders": frozen["execution_blocks"],
        "source_sha256": {str(s.relative_to(ROOT)): digest(s) for s in sources},
        "full_AP_lifecycle_or_global_optimum_verified": False,
        "automatic_recommendation_qualified": False})
    configurations = {c["key"]: c for c in frozen["configurations"]}
    sb = OnlineSB(client, args.out)
    rows, controls, error, cleanup_errors = [], [], None, []
    with tempfile.TemporaryDirectory(prefix="jmt-config-matrix-", dir="/dev/shm") as scratch:
        scratch = Path(scratch)
        scratch.chmod(0o755)
        (scratch / "worker.py").write_bytes((ROOT / "tools/current_ap_worker.py").read_bytes())
        (scratch / "worker.py").chmod(0o644)
        if frozen.get("prior_CPU_service_policy") is not None:
            from prior_CPU_service_run import PriorCPUServiceRun
            args.collect_runnable = True
            run = PriorCPUServiceRun(args, client, sb, scratch, sql)
            run.frozen = frozen
        elif frozen.get("CPU_queue_feedback_policy") is not None:
            if frozen.get("CPU_interference_model") is not None:
                raise ValueError("separate CPU feedback and interference experiments")
            from CPU_queue_feedback_run import CPUQueueFeedbackRun
            args.collect_runnable = True
            run = CPUQueueFeedbackRun(args, client, sb, scratch, sql)
            run.frozen = frozen
        elif frozen.get("CPU_interference_model") is not None:
            from CPU_interference_run import CPUInterferenceRun
            run = CPUInterferenceRun(args, client, sb, scratch, sql)
            run.frozen = frozen
        else:
            run = DiagnosticRun(args, client, sb, scratch, sql)
        previous_signal = signal.signal(signal.SIGTERM, interrupt_for_cleanup)
        try:
            run.base = run.driver("/usr/share/sysbench/oltp_read_only.lua", 128, "base")
            run.wait(time.monotonic_ns() + 40 * 10**9)
            # Fail an expensive 8GiB/TP128 cache reader before spending time
            # on AP episodes. This observation is diagnostic, never refitted.
            preflight = incoming_cache(client)
            dump(args.out / "TP128_CACHE_READER_PREFLIGHT.json", preflight)
            for block in frozen["execution_blocks"]:
                repeat, target = block["repeat"], block["shared_buffers_mib"]
                sb.set(target)
                run.wait(time.monotonic_ns() + 20 * 10**9)
                control_path = args.out / ("TP_ONLY_SB%d_r%02d" % (target, repeat))
                control_path.mkdir()
                run.out = control_path
                value = run.window("TP_ONLY_SB%d" % target, repeat, [])
                if hasattr(run, "freeze_TP_control"):
                    run.freeze_TP_control("TP_ONLY_SB%d" % target, repeat)
                controls.append({"repeat": repeat, "shared_buffers_mib": target, "TPS": value,
                                 "directory": str(control_path.relative_to(args.out))})
                for profile in block["WM_profiles"]:
                    key = "SB%d_%s" % (target, profile)
                    config = configurations[key]
                    case = args.out / ("%s_r%02d" % (key, repeat))
                    case.mkdir()
                    run.out = case
                    plans = {}
                    for q in QUERIES:
                        raw = client.explain("h5_tpch", sql[q], config["AP_work_mem_mib"][str(q)],
                                             analyze=False)
                        if raw["exit_code"]:
                            raise ValueError("pre-episode blind plan check failed")
                        doc = extract_json(raw["stdout"])
                        structure = hashlib.sha256(repr(_shape(blind_root(doc))).encode()).hexdigest()
                        if structure != frozen["AP_profiles"][profile][str(q)]["forecast"][
                                "plan_structure_sha256"]:
                            raise ValueError("SB changes plan shape; cannot silently reuse frozen work")
                        plans[str(q)] = {"blind_plan": doc, "structure_sha256": structure}
                    dump(case / "BLIND_PLAN_RECHECKS.json", plans)
                    before = incoming_cache(client)
                    started = time.perf_counter_ns()
                    prediction = predict_configuration(
                        frozen, config, before["incoming_AP_pages"], before["incoming_AP_other_pages"])
                    if hasattr(run, "augment_prediction"):
                        run.augment_prediction(prediction)
                    prediction.update({
                        "repeat": repeat, "pre_AP_cache": before,
                        "prediction_compute_milliseconds": (time.perf_counter_ns() - started) / 1e6,
                        "frozen_before_AP_monotonic_ns": time.monotonic_ns()})
                    dump(case / "PRE_AP_PREDICTION.json", prediction)
                    run.cpu_context = prediction
                    try:
                        for q in QUERIES:
                            run.ap[q] = AP(run, q, config["AP_work_mem_mib"][str(q)])
                        run.wait(time.monotonic_ns() + 15 * 10**9)
                        actual = run.window(key, repeat, list(QUERIES))
                        rows.append({"configuration": config, "repeat": repeat,
                                     "TPS": actual, "directory": case.name})
                    finally:
                        for q in list(run.ap):
                            run.ap[q].stop("planned bounded matrix AP episode end; not completion label")
                            del run.ap[q]
                    run.wait(time.monotonic_ns() + 10 * 10**9)
                    print(json.dumps({"completed_episode": case.name, "actual_TPS": actual,
                                      "primary_predicted_TPS": (
                                          prediction["historical_TP_static_with_AP_capacity_proxy"][
                                              "resource_fixed_point"]["predicted_tps"]
                                          if prediction["historical_TP_static_with_AP_capacity_proxy"]
                                          else None)}), flush=True)
        except BaseException as exc:
            error = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            # On an exception keep AP stop evidence inside its case directory.
            for q in list(run.ap):
                try:
                    run.ap[q].stop("matrix exception cleanup")
                    del run.ap[q]
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            run.out = args.out
            try:
                cleanup_errors += run.cleanup()
            finally:
                restored = sb.restore()
                dump(args.out / "MATRIX_RESULT.json", {
                    "schema": "jmt.actual-SB-WM-TPS-matrix/v1",
                    "created_at": datetime.now().astimezone().isoformat(),
                    "frozen_sha256": digest(args.frozen), "episodes": rows, "TP_only_controls": controls,
                    "complete": len(rows) == 16 and len(controls) == 8
                                and error is None and not cleanup_errors,
                    "error": error, "cleanup_errors": cleanup_errors,
                    "restoration_verified": restored["config_restored_byte_exact"]
                                             and restored["server_status_matches_original"],
                    "global_optimum_or_full_AP_lifecycle_verified": False,
                    "automatic_recommendation_qualified": False})
                signal.signal(signal.SIGTERM, previous_signal)


if __name__ == "__main__":
    main()
