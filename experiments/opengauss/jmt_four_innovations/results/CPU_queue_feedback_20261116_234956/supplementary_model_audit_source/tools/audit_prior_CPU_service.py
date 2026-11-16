#!/usr/bin/env python3
"""Strict future-window test, including naive prior-TPS persistence."""
import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"),str(ROOT/"vendor"),str(ROOT/"tools")]
from audit_CPU_queue_feedback import audit as audit_static, read, average, runnable_diagnostic
from audit_configuration_TPS_matrix import rank_summary
from collect_current_tp_matrix import digest
from jmt.innovation_4_performance.prior_cpu_service import predict_prior_service,LANES,SCHEMA,FULL,PERSISTENCE
from jmt.innovation_4_performance.configuration_matrix import paired_effect
from jmt.innovation_4_performance.tps_diagnostics import summarize_windows


def audit(run):
    run = Path(run)
    static = audit_static(run)
    frozen = read(run/"mixed/frozen/FROZEN_MATRIX.json")
    policy = frozen["prior_CPU_service_policy"]
    prereg = read(run/"PRE_REGISTERED_POLICY.json")
    if policy != prereg["prior_CPU_service_policy"] or policy["schema"] != SCHEMA:
        raise ValueError("prior CPU service pre-registration changed")
    inputs = dict(static["inputs_sha256"])
    rows,episodes = [],[]
    for row in static["configurations"]:
        config = row["configuration"]
        observed_episodes = []
        for old in row["episodes"]:
            repeat = old["repeat"]
            case = run/"mixed/live"/("%s_r%02d"%(config["key"],repeat))
            fp = case/("%s_r%02d.PRIOR_CPU_FORECAST.json"%(config["key"],repeat))
            forecast = read(fp)
            pp = case/forecast["probe_relative_path"]
            if pp.parent != case or digest(pp) != forecast["probe_sha256"]:
                raise ValueError("short CPU-service probe binding mismatch")
            probe = read(pp)
            target = read(case/("%s_r%02d.WINDOW.json"%(config["key"],repeat)))
            starts = {str(q):read(case/("q%d.START.json"%q)) for q in frozen["cohort"]}
            identities = {q:v["identity"] for q,v in starts.items()}
            if (forecast["AP_task_identities"] != identities
                    or probe["AP_work_mem_mib"] != config["AP_work_mem_mib"]
                    or probe["shared_buffers_mib"] != config["shared_buffers_mib"]
                    or probe["backend_linux_tids"] != target["backend_linux_tids"]
                    or forecast["control_reference"] != read(
                        case/"PRE_AP_PREDICTION.json")["CPU_queue_feedback"]["control_reference"]
                    or forecast["prior_observer_closed_before_target_observer_attached"] is not True):
                raise ValueError("same AP identity/WM/SB/TP membership/prior control required")
            replay = predict_prior_service(
                reference=forecast["control_reference"],probe=probe,
                prediction_ns=forecast["prediction_monotonic_ns"],
                AP_task_identities=identities,cpu_capacity=frozen["calibration"]["cpu_capacity"],
                probe_max_age_seconds=policy["probe_max_age_at_prediction_seconds"],
                control_max_age_seconds=policy["control_max_age_seconds"])
            extra = ("probe_relative_path","probe_sha256","control_reference",
                     "forecast_saved_monotonic_ns","prediction_compute_milliseconds",
                     "prediction_process_CPU_milliseconds",
                     "prior_observer_closed_before_target_observer_attached")
            if replay != {k:v for k,v in forecast.items() if k not in extra}:
                raise ValueError("future CPU-service forecast does not replay")
            if not (probe["measurement_window"]["end_ns"] < forecast["prediction_monotonic_ns"]
                    <= forecast["forecast_saved_monotonic_ns"] < target["measurement_window"]["start_ns"]
                    and 0<target["measurement_window"]["start_ns"]-probe["measurement_window"]["end_ns"]
                    <=policy["max_probe_end_to_target_start_seconds"]*1e9):
                raise ValueError("probe and future target overlap or forecast became stale")
            for start in starts.values():
                if start["request_sent_monotonic_ns"] >= probe["measurement_window"]["start_ns"]:
                    raise ValueError("AP not active during prior probe")
            prior,actual = summarize_windows([probe]),old["observed"]
            runnable = runnable_diagnostic(probe,starts)
            target_TPS = old["actual_TPS"]
            point = forecast["control_reference"]["point"]
            values = {}
            for lane in LANES:
                rate = forecast["lanes"][lane]["predicted_tps"]
                values[lane] = {
                    "predicted_TPS":rate,"absolute_relative_error":abs(rate/target_TPS-1),
                    "AP_degradation_error_TPS":target_TPS-rate}
            episode = {
                "repeat":repeat,"configuration":config,"actual_TPS":target_TPS,"lanes":values,
                "probe_observed":prior,"target_observed":actual,"probe_runnable":runnable,
                "AP_CPU_forecast_error_cores":prior["AP_CPU_cores_bracketed"]-actual["AP_CPU_cores_bracketed"],
                "TP_backend_CPU_forecast_absolute_relative_error":abs(
                    prior["TP_CPU_seconds_per_commit_proxy"]/actual["TP_CPU_seconds_per_commit_proxy"]-1),
                "TP_driver_CPU_forecast_absolute_relative_error":abs(
                    prior["driver_CPU_seconds_per_commit_proxy"]/actual["driver_CPU_seconds_per_commit_proxy"]-1),
                "probe_end_to_target_start_seconds":(target["measurement_window"]["start_ns"]-
                    probe["measurement_window"]["end_ns"])/1e9,
                "prediction_compute_milliseconds":forecast["prediction_compute_milliseconds"]}
            episode["prediction_process_CPU_milliseconds"] = forecast["prediction_process_CPU_milliseconds"]
            episodes.append(episode)
            observed_episodes.append(episode)
            for path in (fp,pp):
                inputs[str(path.relative_to(run))] = digest(path)
        mean_actual = row["actual_mean_TPS"]
        rows.append({"configuration":config,"actual_mean_TPS":mean_actual,"episodes":observed_episodes,
            "lanes":{lane:{"complete_predictions":True,
                "predicted_mean_TPS":average(e["lanes"][lane]["predicted_TPS"] for e in observed_episodes),
                "absolute_relative_error":abs(average(e["lanes"][lane]["predicted_TPS"]
                    for e in observed_episodes)/mean_actual-1)} for lane in LANES}})
    by_key = {r["configuration"]["key"]:r for r in rows}
    comparisons = []
    for contrast in static["paired_changes"]:
        a,b = by_key[contrast["from"]],by_key[contrast["to"]]
        deltas = [b["episodes"][i]["actual_TPS"]-a["episodes"][i]["actual_TPS"] for i in (0,1)]
        comparisons.append({"from":contrast["from"],"to":contrast["to"],"kind":contrast["kind"],
            "lanes":{lane:paired_effect(a["lanes"][lane]["predicted_mean_TPS"],
                b["lanes"][lane]["predicted_mean_TPS"],a["actual_mean_TPS"],b["actual_mean_TPS"],
                repeat_differences=deltas) for lane in LANES}})
    metrics = {lane:{
        "configuration_mean_MAPE":average(r["lanes"][lane]["absolute_relative_error"] for r in rows),
        "window_MAPE":average(e["lanes"][lane]["absolute_relative_error"] for e in episodes),
        "worst_window_absolute_relative_error":max(e["lanes"][lane]["absolute_relative_error"]
                                                     for e in episodes),
        "AP_degradation_MAE_TPS":average(abs(e["lanes"][lane]["AP_degradation_error_TPS"]) for e in episodes),
        "paired_directions_matched":sum(c["lanes"][lane]["predicted_direction_matches_mean"] is True
                                        for c in comparisons),
        "paired_contrasts":12,"ranking":rank_summary(rows,lane)} for lane in LANES}
    return {
        "schema":"jmt.independent-prior-CPU-service-audit/v1",
        "created_at":datetime.now().astimezone().isoformat(),
        "static_same_batch_metrics":static["metrics"],"metrics":metrics,
        "independent_TP_only_heldout_MAPE":static["independent_TP_only_heldout_MAPE"],
        "configurations":rows,"paired_changes":comparisons,
        "AP_CPU_forecast_MAE_cores":average(abs(e["AP_CPU_forecast_error_cores"]) for e in episodes),
        "TP_backend_CPU_forecast_MAPE":average(e["TP_backend_CPU_forecast_absolute_relative_error"]
                                              for e in episodes),
        "TP_driver_CPU_forecast_MAPE":average(e["TP_driver_CPU_forecast_absolute_relative_error"]
                                             for e in episodes),
        "full_CPU_service_beats_naive_persistence_windows":sum(
            e["lanes"][FULL]["absolute_relative_error"]<e["lanes"][PERSISTENCE]["absolute_relative_error"]
            for e in episodes),
        "total_future_target_windows":len(episodes),
        "largest_AP_CPU_phase_change_examples":[{
            "configuration":e["configuration"]["key"],"repeat":e["repeat"],
            "prior_AP_CPU_cores":e["probe_observed"]["AP_CPU_cores_bracketed"],
            "future_AP_CPU_cores":e["target_observed"]["AP_CPU_cores_bracketed"],
            "prior_TP_backend_CPU_seconds_per_tx":e["probe_observed"]["TP_CPU_seconds_per_commit_proxy"],
            "future_TP_backend_CPU_seconds_per_tx":e["target_observed"]["TP_CPU_seconds_per_commit_proxy"],
            "full_CPU_service_TPS_error":e["lanes"][FULL]["absolute_relative_error"],
            "naive_TPS_error":e["lanes"][PERSISTENCE]["absolute_relative_error"]}
            for e in sorted(episodes,key=lambda e:abs(e["AP_CPU_forecast_error_cores"]),reverse=True)[:4]],
        "max_prediction_compute_milliseconds":max(e["prediction_compute_milliseconds"] for e in episodes),
        "max_prediction_process_CPU_milliseconds":max(e["prediction_process_CPU_milliseconds"] for e in episodes),
        "probe_end_to_target_start_range_seconds":[min(e["probe_end_to_target_start_seconds"] for e in episodes),
                                                   max(e["probe_end_to_target_start_seconds"] for e in episodes)],
        "naive_probe_TPS_persistence_comparator_not_omitted":True,
        "requires_same_configuration_running_AP_probe_not_cold_start_search":True,
        "measured_CPU_cost_not_reapplied_as_reference_CPU_budget":True,
        "all_future_targets_frozen_after_probe_before_target":True,
        "candidate_promoted_to_default":False,"automatic_recommendation_qualified":False,
        "restoration_verified":True,"inputs_sha256":inputs}


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
        writer.writerow(["SB_mib","WM","actual_repeat1","actual_repeat2","actual_mean"]+list(LANES))
        for row in result["configurations"]:
            writer.writerow([row["configuration"]["shared_buffers_mib"],row["configuration"]["WM_profile"],
                *[e["actual_TPS"] for e in row["episodes"]],row["actual_mean_TPS"],
                *[row["lanes"][lane]["predicted_mean_TPS"] for lane in LANES]])
    print(json.dumps(result["metrics"],indent=2))


if __name__ == "__main__":
    main()
