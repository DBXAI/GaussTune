#!/usr/bin/env python3
"""Replay the coefficient-free CPU feedback and PRIOR TP-reference ablation."""
import argparse
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"),str(ROOT/"vendor"),str(ROOT/"tools")]
from audit_fresh_TP_baseline import audit as audit_independent_TP
from audit_configuration_TPS_matrix import rank_summary
from collect_current_tp_matrix import digest
from diagnose_AP_runnable import summarize as summarize_task
from jmt.innovation_4_performance.configuration_matrix import configurations, SB_POINTS, paired_effect
from jmt.innovation_4_performance.cpu_queue_feedback import (
    configuration_lanes, SCHEMA, FROZEN_LANE, CONTROL_LANE, CONTROL_ONE_WAY)
from jmt.innovation_4_performance.live_tp_reference import reference_from_window
from jmt.innovation_4_performance.tp_baseline_rebase import COLOCATED
from jmt.innovation_4_performance.tps_diagnostics import summarize_windows

LANES = (COLOCATED,FROZEN_LANE,CONTROL_ONE_WAY,CONTROL_LANE)


def read(path):
    return json.loads(path.read_text())


def average(values):
    values = list(values)
    return statistics.mean(values) if values else None


def runnable_diagnostic(window, starts):
    """Own /proc intervals; not relabeled as an exact commit/IO window."""
    before, after = (window["CPU_bracket"][k] for k in ("before","after"))
    duration = ((after["started_ns"]+after["finished_ns"]-
                 before["started_ns"]-before["finished_ns"])/2)/1e9
    tp_queue, tp_slices = 0, 0
    for tid in window["backend_linux_tids"]:
        key = str(tid)
        delta = after["backend_runqueue_wait_ns"][key]-before["backend_runqueue_wait_ns"][key]
        slices = after["backend_timeslices"][key]-before["backend_timeslices"][key]
        if min(delta,slices) < 0:
            raise ValueError("TP runqueue/timeslice counter decreased")
        tp_queue += delta/1e9
        tp_slices += slices
    queries = {}
    for q, start in starts.items():
        tid = str(start["identity"]["linux_tid"])
        a,b = before["AP_task_diagnostics"][tid],after["AP_task_diagnostics"][tid]
        if (a["identity"] != b["identity"] or a["identity"][1] != int(tid)
                or a["identity"][0] != window["gaussdb_pid"]
                or not before["started_ns"] <= a["sample_ns"] <= before["finished_ns"]
                or not after["started_ns"] <= b["sample_ns"] <= after["finished_ns"]
                or max(a["read_span_ns"],b["read_span_ns"]) >= 100_000_000):
            raise ValueError("AP /proc sample identity/own endpoint invalid")
        io = {k:b["io_submissions_not_completions"][k]-v
              for k,v in a["io_submissions_not_completions"].items()}
        if any(v<0 for k,v in io.items() if k != "cancelled_write_bytes"):
            raise ValueError("AP task IO counter decreased")
        queries[q] = {
            **summarize_task(a,b),"IO_submission_counter_deltas_not_spill_completions":io,
            "syscall_write_characters_are_not_query_owned_spill_bytes":True}
    return {
        "AP":queries, "TP_runqueue_wait_seconds_sum":tp_queue,
        "TP_mean_runqueue_fraction_per_backend":tp_queue/duration/128,
        "TP_timeslice_delta_sum":tp_slices,
        "exact_disjoint_kernel_states_or_IO_ownership_qualified":False}


def audit(run):
    run = Path(run)
    base, _ = audit_independent_TP(run)  # Replays 8 independent TP + 16 AP + 8 controls.
    prereg = read(run/"PRE_REGISTERED_POLICY.json")
    frozen = read(run/"mixed/frozen/FROZEN_MATRIX.json")
    policy = frozen["CPU_queue_feedback_policy"]
    if (prereg["schema"] != SCHEMA or policy["schema"] != SCHEMA
            or prereg["CPU_feedback_coefficient_fitting_or_target_TPS_scaling"] is not False
            or prereg["previous_AP_labels_used_for_fitting"] is not False
            or any(policy[k] != prereg[k] for k in (
                "frozen_TP_max_age_seconds","control_TP_max_age_seconds"))
            or policy["source_sha256"] != prereg["source_sha256"][
                "src/jmt/innovation_4_performance/cpu_queue_feedback.py"]):
        raise ValueError("preregistered feedback formula/age policy changed")
    inputs = {**base["inputs_sha256"],
              **{"mixed/"+k:v for k,v in base["mixed_inputs_sha256"].items()}}
    for relative, expected in prereg["source_sha256"].items():
        path = run/"TP_collection_source"/relative
        if digest(path) != expected:
            raise ValueError("pre-collection model or runtime source changed")
        inputs[str(path.relative_to(run))] = expected
    for path in (run/"LEGACY_RESOURCE_CALIBRATION.json",run/"RUN_STATUS.json"):
        inputs[str(path.relative_to(run))] = digest(path)
    if (digest(run/"LEGACY_RESOURCE_CALIBRATION.json") != prereg[
            "legacy_resource_calibration_source_sha256"]
            or frozen["calibration"] != read(run/"LEGACY_RESOURCE_CALIBRATION.json")
            or read(run/"RUN_STATUS.json")["complete"] is not True):
        raise ValueError("legacy work calibration refit or live run incomplete")
    earliest_TP = min(read(run/"TP_calibration"/("sb%d_r%02d"%(sb,r))/"WINDOW.json")[
        "measurement_window"]["start_ns"] for sb in SB_POINTS for r in (1,2))
    if not prereg["registered_monotonic_ns"] < earliest_TP:
        raise ValueError("policy not frozen before calibration")
    for prediction in frozen["initial_state_predictions"]:
        key = prediction["configuration"]["key"]
        replay = configuration_lanes(frozen,prediction,
            prediction_ns=frozen["frozen_before_experiment_monotonic_ns"])
        if replay != frozen["initial_CPU_queue_feedback_predictions"][key]:
            raise ValueError("common-initial-cache CPU feedback does not replay")
    rows, episodes, controls = [], [], {}
    for sb in SB_POINTS:
        for repeat in (1,2):
            folder = run/"mixed/live"/("TP_ONLY_SB%d_r%02d"%(sb,repeat))
            rp = folder/"TP_REFERENCE_FOR_NEXT_AP.json"
            wp = folder/("TP_ONLY_SB%d_r%02d.WINDOW.json"%(sb,repeat))
            saved, raw = read(rp),read(wp)
            replay = reference_from_window(raw,boot_id=frozen["boot_id"],
                postmaster_start_time=frozen["postmaster_start_time"],
                machine_fingerprint=frozen["machine_fingerprint"],
                source_path=str(wp.relative_to(run/"mixed/live")),source_sha256=digest(wp),
                frozen_ns=saved["frozen_monotonic_ns"])
            if replay != saved:
                raise ValueError("prior TP reference not derived from its raw AP-free window")
            if raw["measurement_window"]["start_ns"] <= frozen["frozen_before_experiment_monotonic_ns"]:
                raise ValueError("new TP control predates model freezing")
            controls[(sb,repeat)] = saved
            inputs[str(rp.relative_to(run))] = digest(rp)
    for config in configurations():
        case_episodes = []
        for repeat in (1,2):
            case = run/"mixed/live"/("%s_r%02d"%(config["key"],repeat))
            pred = read(case/"PRE_AP_PREDICTION.json")
            window = read(case/("%s_r%02d.WINDOW.json"%(config["key"],repeat)))
            saved = pred["CPU_queue_feedback"]
            reference = controls[(config["shared_buffers_mib"],repeat)]
            if saved["control_reference"] != reference:
                raise ValueError("prediction not bound to prior exact-SB control")
            replay = configuration_lanes(frozen,pred,prediction_ns=saved["prediction_monotonic_ns"],
                                          control_reference=reference)
            if replay != {k:v for k,v in saved.items() if k not in (
                    "control_reference","compute_milliseconds")}:
                raise ValueError("CPU feedback target forecast does not replay")
            if saved["prediction_monotonic_ns"] >= pred["frozen_before_AP_monotonic_ns"]:
                raise ValueError("forecast was not saved before AP start")
            starts = {str(q):read(case/("q%d.START.json"%q)) for q in frozen["cohort"]}
            for start in starts.values():
                if not reference["frozen_monotonic_ns"] < saved["prediction_monotonic_ns"] < start[
                        "request_sent_monotonic_ns"] < window["measurement_window"]["start_ns"]:
                    raise ValueError("prior control -> prediction -> AP -> target chronology invalid")
            observed = summarize_windows([window])
            # Enrolled TP CPU sum and AP CPU sum must both bind their endpoints.
            first,last = (window["CPU_bracket"][k]["backend_cpu_ns"] for k in ("before","after"))
            tp_deltas = [last[str(t)]-first[str(t)] for t in window["backend_linux_tids"]]
            if min(tp_deltas)<0 or sum(tp_deltas)/1e9 != window["TP_CPU_seconds"]:
                raise ValueError("TP CPU sum differs from raw enrolled endpoints")
            for q,start in starts.items():
                tid = str(start["identity"]["linux_tid"])
                if (last[tid]-first[tid])/1e9 != window["AP_CPU_seconds"][q]:
                    raise ValueError("AP CPU differs from its owned task endpoints")
            diagnostics = runnable_diagnostic(window,starts)
            forecasts = {COLOCATED:pred[COLOCATED],**saved["lanes"]}
            actual = observed["committed_TPS"]
            true_ap = observed["AP_CPU_cores_bracketed"]
            control = reference["point"]
            lanes = {}
            for name in LANES:
                value = forecasts[name]
                if value is None:
                    lanes[name] = {"predicted_TPS":None,"absolute_relative_error":None,
                                   "unavailable_reason":saved["unavailable_reasons"].get(name)}
                    continue
                solved = value["resource_fixed_point"]
                feedback = name in (FROZEN_LANE,CONTROL_LANE)
                ap_cpu = solved["ap_cpu_seconds_per_second_after_CPU_and_media_closure"
                                if feedback else "ap_cpu_seconds_per_second_after_media_closure"]
                rate = solved["predicted_tps"]
                tp = value["cache_to_TP"]
                baseline = tp["fresh_exact_physical_SB_training_point"]["baseline_tps"]
                backend = tp["TP_CPU_seconds_per_transaction"]
                queue = solved.get("ap_queue_wait_fraction_hypothesis")
                lanes[name] = {
                    "predicted_TPS":rate,"absolute_relative_error":abs(rate/actual-1),
                    "predicted_AP_CPU_cores":ap_cpu,"AP_CPU_error_cores":ap_cpu-true_ap,
                    "predicted_TP_backend_CPU_seconds_per_tx":backend,
                    "TP_backend_CPU_absolute_relative_error":abs(
                        backend/observed["TP_CPU_seconds_per_commit_proxy"]-1),
                    "AP_queue_fraction_hypothesis":queue,
                    "AP_queue_fraction_absolute_errors": [
                        abs(p-diagnostics["AP"][str(q)]["runqueue_wait_fraction"])
                        for q,p in zip(frozen["cohort"],queue)] if queue is not None else None,
                    "TP_baseline_TPS":baseline,
                    "TP_baseline_error_vs_prior_control_TPS":baseline-control["baseline_tps"],
                    "predicted_AP_degradation_TPS":baseline-rate,
                    "observed_control_to_AP_degradation_TPS":control["baseline_tps"]-actual,
                    "AP_degradation_error_TPS":(baseline-rate)-(control["baseline_tps"]-actual),
                    "TP_reference_age_seconds":value.get("TP_reference_age_seconds")}
            budget = control["baseline_tps"]*(control["backend_cpu_seconds_per_tx"]+
                                               control["driver_cpu_seconds_per_tx"])
            actual_service = observed["TP_CPU_seconds_per_commit_proxy"]+observed[
                "driver_CPU_seconds_per_commit_proxy"]
            oracle = min(control["baseline_tps"],max(0.,budget-true_ap)/actual_service)
            oracle_AP_only = min(control["baseline_tps"],max(0.,budget-true_ap)/(
                control["backend_cpu_seconds_per_tx"]+control["driver_cpu_seconds_per_tx"]))
            oracle_AP_and_backend = min(control["baseline_tps"],max(0.,budget-true_ap)/(
                observed["TP_CPU_seconds_per_commit_proxy"]+control["driver_cpu_seconds_per_tx"]))
            episode = {
                "configuration":config,"repeat":repeat,"actual_TPS":actual,
                "prior_control_TPS":control["baseline_tps"],
                "observed":observed,"runnable":diagnostics,"lanes":lanes,
                "prediction_compute_milliseconds":pred["prediction_compute_milliseconds"],
                "feedback_lanes_compute_milliseconds":saved["compute_milliseconds"],
                "AP_age_at_window_start_seconds":window["AP_elapsed_at_window_start_seconds"],
                "offline_oracle_diagnostic": {
                    "given_actual_AP_CPU_only_TPS":oracle_AP_only,
                    "AP_only_absolute_relative_error":abs(oracle_AP_only/actual-1),
                    "given_actual_AP_CPU_and_TP_backend_CPU_TPS":oracle_AP_and_backend,
                    "AP_and_backend_absolute_relative_error":abs(oracle_AP_and_backend/actual-1),
                    "given_actual_AP_CPU_and_actual_TP_backend_driver_CPU_TPS":oracle,
                    "absolute_relative_error":abs(oracle/actual-1),
                    "prior_control_total_CPU_budget_cores":budget,
                    "not_a_forecast_or_fit_input":True,
                    "zero_extra_TP_IO_and_unchanged_other_host_load_conditional":True}}
            case_episodes.append(episode)
            episodes.append(episode)
        actual_mean = average(e["actual_TPS"] for e in case_episodes)
        summary = {}
        for lane in LANES:
            complete = all(e["lanes"][lane]["predicted_TPS"] is not None for e in case_episodes)
            predicted = average(e["lanes"][lane]["predicted_TPS"] for e in case_episodes) if complete else None
            summary[lane] = {"complete_predictions":complete,"predicted_mean_TPS":predicted,
                "absolute_relative_error":abs(predicted/actual_mean-1) if complete else None}
        rows.append({"configuration":config,"actual_mean_TPS":actual_mean,"lanes":summary,
                     "episodes":case_episodes})
    by_key = {r["configuration"]["key"]:r for r in rows}
    contrasts = []

    def contrast(a_key,b_key,kind):
        a,b = by_key[a_key],by_key[b_key]
        deltas = [b["episodes"][i]["actual_TPS"]-a["episodes"][i]["actual_TPS"] for i in (0,1)]
        comparisons = {}
        for lane in LANES:
            comparisons[lane] = paired_effect(
                a["lanes"][lane]["predicted_mean_TPS"],b["lanes"][lane]["predicted_mean_TPS"],
                a["actual_mean_TPS"],b["actual_mean_TPS"],repeat_differences=deltas) if all(
                    row["lanes"][lane]["complete_predictions"] for row in (a,b)) else None
        contrasts.append({"from":a_key,"to":b_key,"kind":kind,"lanes":comparisons})

    for sb in SB_POINTS:
        contrast("SB%d_LOW"%sb,"SB%d_HIGH"%sb,"double_AP_WM_same_SB")
    for profile in ("LOW","HIGH"):
        for a,b in ((2048,4096),(4096,6144),(6144,8192),(2048,8192)):
            contrast("SB%d_%s"%(a,profile),"SB%d_%s"%(b,profile),"increase_SB_same_WM")
    metrics = {}
    for lane in LANES:
        usable = [e for e in episodes if e["lanes"][lane]["predicted_TPS"] is not None]
        metrics[lane] = {
            "numeric_episode_coverage":len(usable),
            "configuration_mean_MAPE":average(r["lanes"][lane]["absolute_relative_error"]
                for r in rows if r["lanes"][lane]["complete_predictions"]),
            "window_MAPE":average(e["lanes"][lane]["absolute_relative_error"] for e in usable),
            "AP_CPU_MAE_cores":average(abs(e["lanes"][lane]["AP_CPU_error_cores"]) for e in usable),
            "TP_backend_CPU_MAPE":average(e["lanes"][lane][
                "TP_backend_CPU_absolute_relative_error"] for e in usable),
            "AP_degradation_MAE_TPS":average(abs(e["lanes"][lane][
                "AP_degradation_error_TPS"]) for e in usable),
            "AP_queue_wait_fraction_MAE":average(v for e in usable
                for v in (e["lanes"][lane]["AP_queue_fraction_absolute_errors"] or [])),
            "paired_directions_matched":sum(bool(c["lanes"][lane][
                "predicted_direction_matches_mean"]) for c in contrasts if c["lanes"][lane] is not None),
            "paired_contrasts":len(contrasts),"ranking":rank_summary(rows,lane)}
    return {
        "schema":"jmt.independent-CPU-queue-feedback-audit/v1",
        "created_at":datetime.now().astimezone().isoformat(),
        "current_TP_training_points":frozen["fresh_TP_points"]["points"],
        "independent_TP_only_heldout_MAPE":base["heldout_TP_only_MAPE"],
        "metrics":metrics,"configurations":rows,"paired_changes":contrasts,
        "maximum_feedback_lanes_compute_milliseconds":max(
            e["feedback_lanes_compute_milliseconds"] for e in episodes),
        "maximum_all_lanes_compute_milliseconds":max(
            e["prediction_compute_milliseconds"] for e in episodes),
        "TP_reference_age_range_seconds": {
            lane: ([min(values),max(values)] if values else None)
            for lane in (FROZEN_LANE,CONTROL_LANE)
            for values in [[e["lanes"][lane]["TP_reference_age_seconds"] for e in episodes
                            if e["lanes"][lane]["predicted_TPS"] is not None]]},
        "offline_given_actual_CPU_oracle_window_MAPE":average(e["offline_oracle_diagnostic"][
            "absolute_relative_error"] for e in episodes),
        "offline_CPU_budget_oracle_chain": {
            "given_actual_AP_CPU_only_MAPE":average(e["offline_oracle_diagnostic"][
                "AP_only_absolute_relative_error"] for e in episodes),
            "plus_actual_TP_backend_CPU_MAPE":average(e["offline_oracle_diagnostic"][
                "AP_and_backend_absolute_relative_error"] for e in episodes),
            "plus_actual_TP_driver_CPU_MAPE":average(e["offline_oracle_diagnostic"][
                "absolute_relative_error"] for e in episodes),
            "CPU_conservation_scenario_not_same_as_scheduler_model_or_forecast":True},
        "actual_AP_age_range_seconds":[min(v for e in episodes for v in e[
            "AP_age_at_window_start_seconds"].values()),max(v for e in episodes for v in e[
            "AP_age_at_window_start_seconds"].values())],
        "same_new_windows_all_lanes_no_target_fit":True,
        "pure_TP_controls_are_prior_inputs_not_this_lane_accuracy_holdout":True,
        "query_ID_or_stage_ID_coefficients":False,
        "scheduler_model_IO_ownership_or_full_AP_phase_qualified":False,
        "full_five_stages_or_global_WM_SB_optimum_verified":False,
        "restoration_verified":True,"candidate_promoted_to_default":False,
        "automatic_recommendation_qualified":False,"inputs_sha256":inputs}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--csv",type=Path,required=True)
    args = p.parse_args()
    result = audit(args.run)
    with args.out.open("x") as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write("\n")
    with args.csv.open("x",newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["SB_mib","WM","actual_repeat1_TPS","actual_repeat2_TPS","actual_mean_TPS"]+
                        [lane+"_predicted_mean_TPS" for lane in LANES])
        for row in result["configurations"]:
            writer.writerow([row["configuration"]["shared_buffers_mib"],row["configuration"]["WM_profile"],
                *[e["actual_TPS"] for e in row["episodes"]],row["actual_mean_TPS"],
                *[row["lanes"][lane]["predicted_mean_TPS"] for lane in LANES]])
    print(json.dumps(result["metrics"],ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
