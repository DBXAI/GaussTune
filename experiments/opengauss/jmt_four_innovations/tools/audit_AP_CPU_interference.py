#!/usr/bin/env python3
"""Strict independent CPU calibration + fresh heldout configuration replay."""
import argparse
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"), str(ROOT/"vendor"), str(ROOT/"tools")]
from collect_current_tp_matrix import dump, digest
from freeze_AP_CPU_interference import load, replay_calibration
from audit_configuration_TPS_matrix import audit as base_audit, rank_summary
from jmt.innovation_4_performance.cpu_interference import (
    fit_interference, configuration_cpu_lanes, backend_multiplier,
)
from jmt.innovation_4_performance.configuration_matrix import paired_effect, SB_POINTS
from jmt.innovation_4_performance.phase_bridge import cpu_rate_from_probe
from jmt.innovation_4_performance.tp_baseline_rebase import COLOCATED
from jmt.innovation_4_performance.colocated_tp import exact_tp_point

STATIC = ("static_CPU_budget_no_inflation", "static_CPU_budget_independent_inflation")
PHASE = ("prior_4s_CPU_budget_no_inflation", "prior_4s_CPU_budget_independent_inflation")
INITIAL = tuple("common_initial_"+key for key in STATIC)
ALL = (COLOCATED,)+INITIAL+STATIC+PHASE


def average(values):
    values = list(values)
    return statistics.mean(values) if values else None


def validate_TP_CPU_sum(window):
    first, last = (window["CPU_bracket"][key]["backend_cpu_ns"] for key in ("before", "after"))
    deltas = [last[str(t)]-first[str(t)] for t in window["backend_linux_tids"]]
    if min(deltas) < 0 or sum(deltas)/1e9 != window["TP_CPU_seconds"]:
        raise ValueError("TP CPU total does not reproduce from enrolled schedstat endpoints")


def audit(run, calibration_name="calibration_clean"):
    run = Path(run)
    calibration = run/calibration_name
    model_path = run/"CPU_INTERFERENCE_MODEL.json"
    model = load(model_path)
    prereg, clean_policy = load(run/"PRE_REGISTERED_POLICY.json"), load(run/"CLEAN_COLLECTION_POLICY.json")
    if (clean_policy["clean_collection_directory"] != calibration_name
            or clean_policy["no_partial_prior_window_reused"] is not True
            or clean_policy["model_code_sha256"] != model["training_model_source_sha256"]
            or prereg["no_target_label_refit_or_online_TP_CPU_per_commit_used_for_prediction"] is not True
            or prereg["created_monotonic_ns"] >= clean_policy["monotonic_ns"]
            or clean_policy["monotonic_ns"] >= model["frozen_monotonic_ns"]):
        raise ValueError("pre-registration/calibration-source chronology mismatch")
    raw_rows, machine = replay_calibration(calibration)
    rows = [r for r in raw_rows if r["role"] == "calibration"]
    replayed = fit_interference(rows, excluded_target_sql_sha256s=model["excluded_target_SQL_sha256s"])
    for key, value in replayed.items():
        if model[key] != value:
            raise ValueError("independent CPU coefficient does not reproduce")
    heldout = []
    for row in raw_rows:
        if row["role"] != "heldout":
            continue
        actual = row["mixed_backend_cpu_seconds_per_tx"]
        forecast = row["control_backend_cpu_seconds_per_tx"]*backend_multiplier(model, row["AP_CPU_cores"])
        heldout.append({
            "key": row["key"], "measured_AP_CPU_cores_used_for_CPU_service_test_only": row["AP_CPU_cores"],
            "predicted_backend_cpu_seconds_per_tx": forecast, "actual_backend_cpu_seconds_per_tx": actual,
            "absolute_relative_error": abs(forecast/actual-1),
            "no_inflation_absolute_relative_error": abs(row["control_backend_cpu_seconds_per_tx"]/actual-1)})
    if (heldout != model["heldout_CPU_service_diagnostic"] or len(heldout) != 6
            or sum(r["absolute_relative_error"] for r in heldout)/6 != model["heldout_CPU_service_MAPE"]
            or sum(r["no_inflation_absolute_relative_error"] for r in heldout)/6
            != model["heldout_no_inflation_CPU_service_MAPE"]):
        raise ValueError("heldout CPU service diagnostic does not reproduce")
    for relative, expected in model["source_inventory"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("unsafe CPU source inventory")
        if digest(calibration/relative) != expected:
            raise ValueError("independent raw CPU training evidence changed")
    mixed = run/"mixed"
    frozen = load(mixed/"frozen/FROZEN_MATRIX.json")
    if (frozen["CPU_interference_model"] != model
            or digest(model_path) != frozen["CPU_interference_model_sha256"]
            or digest(mixed/"frozen/CPU_INTERFERENCE_MODEL.json") != digest(model_path)
            or frozen["machine_fingerprint"] != machine["machine_fingerprint"]
            or frozen["boot_id"] != model["boot_id"]
            or frozen["postmaster_start_time"] != model["postmaster_start_time"]
            or frozen["fresh_TP_points_sha256"] != prereg["baseline_model_sha256"]
            or model["frozen_monotonic_ns"] >= frozen["frozen_before_experiment_monotonic_ns"]):
        raise ValueError("CPU calibration and validation identity/chronology mismatch")
    target_hashes = sorted({r["sql_sha256"] for r in frozen["AP_profiles"]["LOW"].values()})
    if target_hashes != model["excluded_target_SQL_sha256s"]:
        raise ValueError("target SQL train/validation split changed")
    if digest(mixed/"live/source_snapshot/src/jmt/innovation_4_performance/cpu_interference.py") != model[
            "training_model_source_sha256"]:
        raise ValueError("CPU model source changed between training and validation")
    base = base_audit(mixed)  # Recheck 16 windows, 8 controls, plans, CPU timing, restoration.
    controls = {(r["shared_buffers_mib"], r["repeat"]): r
                for r in base["TP_only_controls"]}
    for (sb, repeat), control in controls.items():
        validate_TP_CPU_sum(load(mixed/"live"/control["directory"]/(
            "TP_ONLY_SB%d_r%02d.WINDOW.json" % (sb, repeat))))
    initial_by_key = {r["configuration"]["key"]: r for r in frozen["initial_state_predictions"]}
    output_rows, episodes, inputs = [], [], [model_path, run/"PRE_REGISTERED_POLICY.json",
                                           run/"CLEAN_COLLECTION_POLICY.json"]
    for row in base["configurations"]:
        config = row["configuration"]
        case_episodes = []
        for original in row["episodes"]:
            repeat = original["repeat"]
            case = mixed/original["directory"]
            pre = load(case/"PRE_AP_PREDICTION.json")
            prefix = config["key"]+"_r%02d" % repeat
            raw = load(case/(prefix+".WINDOW.json"))
            validate_TP_CPU_sum(raw)
            probe_path = case/(prefix+".CPU_PREWINDOW.json")
            prior = load(probe_path)
            inputs.append(probe_path)
            if (prior["stage"] != config["key"] or prior["repeat"] != repeat
                    or prior.get("observer_attached_during_probe") is not True
                    or prior.get("commit_count_window_disarmed_during_probe") is not True
                    or prior.get("target_window_TPS_or_CPU_used") is not False
                    or not prior["AP_probe"]["finished_ns"] <= prior["prediction_monotonic_ns"]
                    <= prior["forecast_saved_monotonic_ns"] < raw["measurement_window"]["start_ns"]):
                raise ValueError("phase forecast used future or mismatched observer context")
            phase = cpu_rate_from_probe(prior["AP_probe"], prediction_ns=prior["prediction_monotonic_ns"])
            if phase != prior["phase"] or prior["lanes"] != configuration_cpu_lanes(
                    frozen, pre, AP_cores=phase["AP_CPU_cores"]):
                raise ValueError("phase forecast does not reproduce from prior observations")
            actual_ap = 0.
            for q, samples in prior["AP_probe"]["queries"].items():
                start = load(case/("q%s.START.json" % q))
                tid = start["identity"]["linux_tid"]
                if samples["before"]["identity"][:2] != [raw["gaussdb_pid"], tid]:
                    raise ValueError("phase task identity differs from target AP backend")
                b, a = (raw["CPU_bracket"][key]["backend_cpu_ns"][str(tid)]
                        for key in ("before", "after"))
                if a-b < 0 or (a-b)/1e9 != raw["AP_CPU_seconds"][q]:
                    raise ValueError("AP target CPU does not reproduce from schedstat")
                actual_ap += (a-b)/1e9/raw["scored_seconds"]
            if set(prior["AP_probe"]["queries"]) != set(raw["AP_CPU_seconds"]):
                raise ValueError("phase AP membership mismatch")
            observed = original["observed"]
            actual = original["actual_TPS"]
            forecasts = {
                COLOCATED: {"predicted_tps": original["lanes"][COLOCATED]["predicted_TPS"]},
                **{key: pre[key] for key in STATIC},
                **{key: prior["lanes"][key] for key in PHASE},
                **{"common_initial_"+key: initial_by_key[config["key"]][key] for key in STATIC}}
            lane_values = {}
            tp_control = controls[(config["shared_buffers_mib"], repeat)]["TPS"]
            point = exact_tp_point(frozen["fresh_TP_points"], topology="TP128",
                                   shared_buffers_mib=config["shared_buffers_mib"])
            ref = pre[COLOCATED]["cache_to_TP"]
            for key, forecast in forecasts.items():
                value = forecast["predicted_tps"] if forecast is not None else None
                cpu = (forecast.get("TP_loaded_backend_CPU_seconds_per_tx")
                       if forecast is not None else None)
                actual_cpu = raw["TP_CPU_seconds"]/raw["committed_transactions"]
                lane_values[key] = {
                    "predicted_TPS": value,
                    "absolute_relative_error": abs(value/actual-1) if value is not None else None,
                    "signed_relative_error": value/actual-1 if value is not None else None,
                    "backend_CPU_service_absolute_relative_error": (
                        abs(cpu/actual_cpu-1) if cpu is not None else None),
                    "AP_degradation_error_TPS_diagnostic": (
                        point["baseline_tps"]-value-(tp_control-actual) if value is not None else None)}
            oracle = configuration_cpu_lanes(frozen, pre, AP_cores=actual_ap)
            base_budget = ref["baseline_tps"] * (
                ref["TP_CPU_seconds_per_transaction"]+point["driver_cpu_seconds_per_tx"])
            oracle_both = min(ref["baseline_tps"], max(0., base_budget-actual_ap) / (
                raw["TP_CPU_seconds"]/raw["committed_transactions"]+point["driver_cpu_seconds_per_tx"]))
            control = controls[(config["shared_buffers_mib"], repeat)]
            control_backend = control["observed"]["TP_CPU_seconds_per_commit_proxy"]
            control_driver = control["observed"]["driver_CPU_seconds_per_commit_proxy"]
            if control_driver is None:
                raise ValueError("missing control driver CPU is not zero")
            # Additional oracle chain: baseline drift is itself an upstream
            # error. These controls and FUTURE CPU labels remain diagnosis
            # only; do not pass them off as calibration points/forecasts.
            actual_driver = observed["driver_CPU_seconds_per_commit_proxy"]
            if actual_driver is None:
                raise ValueError("missing target driver CPU is not zero")
            current_busy_budget = control["TPS"]*(control_backend+control_driver)
            static_ap = pre[COLOCATED]["resource_fixed_point"][
                "ap_cpu_seconds_per_second_after_media_closure"]
            def control_oracle(ap, backend, driver):
                return min(control["TPS"], max(0., current_busy_budget-ap)/(backend+driver))
            refreshed_static = control_oracle(
                static_ap, control_backend*backend_multiplier(model, static_ap), control_driver)
            refreshed_actual_ap = control_oracle(
                actual_ap, control_backend*backend_multiplier(model, actual_ap), control_driver)
            all_CPU_actual = control_oracle(
                actual_ap, raw["TP_CPU_seconds"]/raw["committed_transactions"], actual_driver)
            episode = {
                "configuration": config["key"], "repeat": repeat, "directory": original["directory"],
                "actual_TPS": actual, "lanes": lane_values,
                "AP_CPU_static_forecast": pre[COLOCATED]["resource_fixed_point"][
                    "ap_cpu_seconds_per_second_after_media_closure"],
                "AP_CPU_prior_4s_forecast": phase["AP_CPU_cores"],
                "AP_CPU_future_window_actual": actual_ap,
                "actual_backend_CPU_seconds_per_commit": raw["TP_CPU_seconds"]/raw["committed_transactions"],
                "frozen_TP_only_baseline_TPS": point["baseline_tps"],
                "adjacent_TP_only_control_TPS_NOT_fit": control["TPS"],
                "frozen_TP_only_baseline_bias_TPS_diagnostic": point["baseline_tps"]-control["TPS"],
                "AP_probe_to_window_start_gap_seconds": (
                    raw["measurement_window"]["start_ns"]-prior["AP_probe"]["finished_ns"])/1e9,
                "online_prediction_compute_milliseconds": prior["prediction_compute_milliseconds"],
                "static_prediction_compute_milliseconds": pre["prediction_compute_milliseconds"],
                "CPU_oracle_diagnostics_NOT_forecasts": {
                    "actual_AP_CPU_with_independent_backend_model": (
                        oracle[PHASE[1]]["predicted_tps"] if oracle[PHASE[1]] is not None else None),
                    "actual_AP_CPU_and_actual_TP_backend_service": oracle_both,
                    "adjacent_TP_control_baseline_static_AP_and_independent_service": refreshed_static,
                    "adjacent_TP_control_baseline_actual_AP_and_independent_service": refreshed_actual_ap,
                    "adjacent_TP_control_baseline_actual_AP_and_all_actual_TP_CPU": all_CPU_actual,
                    "adjacent_control_not_fit_and_historical_cache_loss_not_reapplied": True,
                    "only_offline_stepwise_error_localization": True},
            }
            case_episodes.append(episode)
            episodes.append(episode)
        lane_summary = {}
        for key in ALL:
            values = [e["lanes"][key]["predicted_TPS"] for e in case_episodes]
            complete = all(v is not None for v in values)
            predicted = average(values) if complete else None
            lane_summary[key] = {
                "complete_predictions": complete, "predicted_mean_TPS": predicted,
                "absolute_relative_error": abs(predicted/row["actual_mean_TPS"]-1) if complete else None}
        output_rows.append({
            "configuration": config, "actual_mean_TPS": row["actual_mean_TPS"],
            "actual_repeat_TPS": [e["actual_TPS"] for e in case_episodes],
            "lanes": lane_summary, "episodes": case_episodes})
    by_key = {r["configuration"]["key"]: r for r in output_rows}
    comparisons = []
    pairs = [("SB%d_LOW" % sb, "SB%d_HIGH" % sb) for sb in SB_POINTS]
    pairs += [("SB%d_%s" % (a, w), "SB%d_%s" % (b, w))
              for w in ("LOW", "HIGH") for a, b in ((2048,4096),(4096,6144),(6144,8192),(2048,8192))]
    for ak, bk in pairs:
        a, b = by_key[ak], by_key[bk]
        lanes = {}
        for key in ALL:
            av, bv = a["lanes"][key], b["lanes"][key]
            lanes[key] = paired_effect(
                av["predicted_mean_TPS"], bv["predicted_mean_TPS"],
                a["actual_mean_TPS"], b["actual_mean_TPS"],
                repeat_differences=[b["actual_repeat_TPS"][i]-a["actual_repeat_TPS"][i] for i in (0,1)]
            ) if av["complete_predictions"] and bv["complete_predictions"] else None
        comparisons.append({"from": ak, "to": bk, "lanes": lanes})
    summary = {}
    for key in ALL:
        summary[key] = {
            "numeric_episode_coverage": sum(e["lanes"][key]["predicted_TPS"] is not None for e in episodes),
            "episode_MAPE": average(e["lanes"][key]["absolute_relative_error"] for e in episodes
                                    if e["lanes"][key]["predicted_TPS"] is not None),
            "configuration_mean_MAPE": average(r["lanes"][key]["absolute_relative_error"] for r in output_rows
                                                if r["lanes"][key]["complete_predictions"]),
            "backend_CPU_service_MAPE": average(e["lanes"][key][
                "backend_CPU_service_absolute_relative_error"] for e in episodes if
                e["lanes"][key]["backend_CPU_service_absolute_relative_error"] is not None),
            "AP_degradation_MAE_TPS_diagnostic": average(abs(e["lanes"][key][
                "AP_degradation_error_TPS_diagnostic"]) for e in episodes
                if e["lanes"][key]["AP_degradation_error_TPS_diagnostic"] is not None),
            "paired_contrasts_with_numeric_predictions": sum(c["lanes"][key] is not None for c in comparisons),
            "paired_delta_MAE_TPS": average(abs(c["lanes"][key]["delta_error_TPS"])
                for c in comparisons if c["lanes"][key] is not None),
            "paired_mean_direction_match_count": sum(c["lanes"][key][
                "predicted_direction_matches_mean"] is True for c in comparisons if c["lanes"][key] is not None),
            "ranking": rank_summary(output_rows, key),
            "is_online_conditional_not_config_search": key in PHASE}
    oracle_summary = {}
    for key in ("actual_AP_CPU_with_independent_backend_model", "actual_AP_CPU_and_actual_TP_backend_service",
                "adjacent_TP_control_baseline_static_AP_and_independent_service",
                "adjacent_TP_control_baseline_actual_AP_and_independent_service",
                "adjacent_TP_control_baseline_actual_AP_and_all_actual_TP_CPU"):
        values = [(e["CPU_oracle_diagnostics_NOT_forecasts"][key], e["actual_TPS"]) for e in episodes]
        usable = [(p, a) for p, a in values if p is not None]
        oracle_summary[key] = {
            "numeric_coverage": len(usable), "episode_MAPE": average(abs(p/a-1) for p,a in usable),
            "NOT_a_prediction_or_validation_accuracy": True}
    result = {
        "schema": "jmt.independent-CPU-interference-validation/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "calibration_directory": calibration_name, "model_sha256": digest(model_path),
        "coefficient_per_AP_core": model["coefficient_per_AP_core"],
        "independent_training_episodes": 6, "independent_heldout_calibration_episodes": 6,
        "heldout_CPU_service_MAPE": model["heldout_CPU_service_MAPE"],
        "heldout_no_inflation_CPU_service_MAPE": model["heldout_no_inflation_CPU_service_MAPE"],
        "configurations": output_rows, "paired_changes": comparisons, "summary": summary,
        "stepwise_oracle_error_localization_NOT_forecasts": oracle_summary,
        "AP_CPU_forecast_MAE_cores": {
            "static_full_query_average": average(abs(e["AP_CPU_static_forecast"]-
                e["AP_CPU_future_window_actual"]) for e in episodes),
            "prior_4s_phase_persistence": average(abs(e["AP_CPU_prior_4s_forecast"]-
                e["AP_CPU_future_window_actual"]) for e in episodes)},
        "TP_only_baseline_drift_diagnostic": {
            "baseline_level_MAE_TPS": average(abs(e["frozen_TP_only_baseline_bias_TPS_diagnostic"])
                                             for e in episodes),
            "baseline_level_MAPE": average(abs(e["frozen_TP_only_baseline_TPS"]/
                e["adjacent_TP_only_control_TPS_NOT_fit"]-1) for e in episodes),
            "controls_not_used_to_rebase_any_forecast": True},
        "actual_AP_age_at_scored_start_range_seconds": base["actual_AP_age_at_scored_start_range_seconds"],
        "max_static_prediction_compute_milliseconds": max(e["static_prediction_compute_milliseconds"] for e in episodes),
        "max_online_prediction_compute_milliseconds": max(e["online_prediction_compute_milliseconds"] for e in episodes),
        "AP_episodes": 16, "TP_only_target_controls_diagnostic_never_fit": 8,
        "highest_observed_mean_in_tested_set": base["highest_observed_mean_in_tested_set"],
        "complete": True, "restoration_verified": True,
        "same_new_targets_for_all_lane_comparisons": True,
        "all_four_preregistered_CPU_scenarios_reported": True,
        "probe_IO_submissions_not_used_as_physical_completion_or_TP_owned_IO": True,
        "online_conditional_accuracy_not_cold_start_config_recommendation": True,
        "cross_configuration_ranking_qualified": False,
        "full_AP_lifecycles_or_PPT_five_stages_or_S5_verified": False,
        "automatic_recommendation_qualified": False,
        "base_validation_inputs_sha256": base["inputs_sha256"],
        "additional_inputs_sha256": {str(p.relative_to(run)): digest(p) for p in inputs},
    }
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--csv", type=Path)
    args = p.parse_args()
    result = audit(args.run)
    dump(args.out, result)
    if args.csv:
        with args.csv.open("x", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["SB_GiB", "WM", "actual_mean_TPS", "actual_r1", "actual_r2"]+[
                key+"_mean_TPS" for key in ALL])
            for row in result["configurations"]:
                writer.writerow([row["configuration"]["shared_buffers_mib"]/1024,
                    row["configuration"]["WM_profile"], row["actual_mean_TPS"],
                    *row["actual_repeat_TPS"]]+[row["lanes"][k]["predicted_mean_TPS"] for k in ALL])
    print(json.dumps({"summary": result["summary"],
                      "CPU_oracles_NOT_forecasts": result["stepwise_oracle_error_localization_NOT_forecasts"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
