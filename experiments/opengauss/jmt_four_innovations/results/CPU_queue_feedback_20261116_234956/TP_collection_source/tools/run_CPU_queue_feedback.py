#!/usr/bin/env python3
"""Preregister, recollect TP, freeze, then run the opt-in AP feedback matrix."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"),str(ROOT/"vendor"),str(ROOT/"tools")]
from collect_current_tp_matrix import main as collect, dump, digest
from freeze_current_TP_points import freeze
from prepare_configuration_TPS_matrix import main as prepare
from validate_configuration_TPS_matrix import main as validate, interrupt_for_cleanup
from jmt.innovation_4_performance.cpu_queue_feedback import SCHEMA
from jmt.common.db import Gsql


def call(main, argv):
    previous = sys.argv
    try:
        sys.argv = [main.__module__]+list(map(str,argv))
        return main()
    finally:
        sys.argv = previous


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--calibration-inputs",type=Path,required=True)
    p.add_argument("--sql-dir",type=Path,required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    runtime_sources = [
        "tools/run_CPU_queue_feedback.py","tools/collect_current_tp_matrix.py",
        "tools/freeze_current_TP_points.py","tools/prepare_configuration_TPS_matrix.py",
        "tools/validate_configuration_TPS_matrix.py","tools/CPU_queue_feedback_run.py",
        "tools/measure_current_mixed_five_stages.py","tools/validate_capacity_TPS_live.py",
        "tools/diagnose_AP_runnable.py","tools/current_ap_worker.py",
        "src/jmt/innovation_4_performance/cpu_queue_feedback.py",
        "src/jmt/innovation_4_performance/live_tp_reference.py",
        "src/jmt/innovation_4_performance/cpu_surface.py",
        "src/jmt/innovation_4_performance/capped_device.py",
        "src/jmt/innovation_4_performance/colocated_tp.py",
        "src/jmt/innovation_4_performance/tp_baseline_rebase.py",
        "src/jmt/innovation_4_performance/preview_cost.py",
        "src/jmt/innovation_4_performance/configuration_matrix.py",
        "src/jmt/innovation_4_performance/tp_window_observer.py",
        "src/jmt/common/process_cpu.py"]
    for relative in runtime_sources:
        target = args.out/"TP_collection_source"/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/relative,target)
    dump(args.out/"PRE_REGISTERED_POLICY.json", {
        "created_at":datetime.now().astimezone().isoformat(),
        "registered_monotonic_ns":time.monotonic_ns(),
        "boot_id":Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "schema":SCHEMA, "training_repeat":1,"heldout_repeat":2,
        "points_mib":[2048,4096,6144,8192],
        "frozen_TP_max_age_seconds":3600,"control_TP_max_age_seconds":300,
        "no_AP_during_independent_TP_collection":True,
        "matrix_TP_controls_are_prior_calibration_inputs_not_TP_holdout":True,
        "CPU_feedback_coefficient_fitting_or_target_TPS_scaling":False,
        "previous_AP_labels_used_for_fitting":False,
        "historical_AP_costs_IO_proxies_and_relative_cache_transfer_not_refit":True,
        "legacy_resource_calibration_source_sha256":digest(args.calibration_inputs),
        "no_development_tests_packaging_or_other_benchmark_during_live_windows":True,
        "source_sha256":{s:digest(ROOT/s) for s in runtime_sources},
        "automatic_recommendation_qualified":False})
    shutil.copy2(args.calibration_inputs,args.out/"LEGACY_RESOURCE_CALIBRATION.json")
    error = None
    previous = signal.signal(signal.SIGTERM,interrupt_for_cleanup)
    try:
        call(collect,["--out",args.out/"TP_calibration","--points","8192,2048,6144,4096",
                      "--repeats",2,"--warmup-seconds",30,"--window-seconds",20])
        dump(args.out/"FRESH_TP_POINTS.json",freeze(args.out))
        call(prepare,["--calibration-inputs",args.out/"LEGACY_RESOURCE_CALIBRATION.json",
                      "--sql-dir",args.sql_dir,"--out",args.out/"mixed/frozen",
                      "--fresh-tp-points",args.out/"FRESH_TP_POINTS.json","--cpu-queue-feedback"])
        call(validate,["--run-bounded-matrix","--frozen",args.out/"mixed/frozen/FROZEN_MATRIX.json",
                       "--out",args.out/"mixed/live"])
    except BaseException as exc:
        error = type(exc).__name__+": "+str(exc)
        raise
    finally:
        signal.signal(signal.SIGTERM,previous)
        dump(args.out/"RUN_STATUS.json",{
            "finished_at":datetime.now().astimezone().isoformat(),"error":error,
            "complete":error is None,"automatic_recommendation_qualified":False})
        client = Gsql("/opt/openGauss/bin/gsql",run_as_user="omm",timeout_s=30)
        dump(args.out/"FINAL_DB_STATE.json",client.json("postgres", """SELECT json_build_object(
            'start',pg_postmaster_start_time(),
            'target',(SELECT setting FROM pg_settings WHERE name='shared_buffers_target'),
            'native_memory',(SELECT json_agg(t) FROM pg_total_memory_detail t),
            'owned_active',(SELECT coalesce(json_agg(application_name),'[]'::json)
              FROM pg_stat_activity WHERE application_name LIKE 'jmt_mix_%'
                AND state='active'))"""))


if __name__ == "__main__":
    main()
