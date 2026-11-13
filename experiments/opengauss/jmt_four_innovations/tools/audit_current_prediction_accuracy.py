#!/usr/bin/env python3
"""Audit immutable five-stage preview against completed same-WM observations.

No DB, re-fitting, mixed-TPS fabrication or overwriting of recorded labels.
Post-repair replay is separately marked development evidence.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.innovation_2_spill.labels import actual_label, nodes
from jmt.innovation_2_spill.observations import cardinalities


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def comparison(predicted, actual, comparable=True):
    error = predicted - actual
    return {
        "prediction": predicted, "observed": actual,
        "signed_relative_error_pct": 100 * error / actual if actual else None,
        "absolute_relative_error_pct": 100 * abs(error) / actual if actual else None,
        "same_measurement_scope": comparable,
        "zero_observation_absolute_error": abs(error) if actual == 0 else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recommendation", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--development-replay", type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    report_path = args.recommendation / "FIVE_STAGE_RECOMMENDATIONS.json"
    report = read(report_path)
    options = read(args.recommendation / "QUERY_OPTIONS.json")
    trained_keys = {row["key"] for row in read(args.recommendation / "COST_CALIBRATION_ROWS.json")}
    sources = [report_path, args.recommendation / "QUERY_OPTIONS.json",
               args.recommendation / "PREVIEW_COST_MODEL.json"]
    rows = []
    collections = (
        args.run / "full_ap_v1", args.run / "q9_sampled_wm2048_v1",
        args.run / "q18_key_sampled_wm4096_v1")
    for collection in collections:
        for path in sorted(collection.glob("q*_wm*/MEASUREMENT.json")):
            q_text, wm_text = path.parent.name[1:].split("_wm")
            q, wm = int(q_text), int(wm_text)
            observed = read(path)
            if observed.get("statement_completed") is not True:
                rows.append({
                    "arm": path.parent.name, "execution_completed": False,
                    "error": observed.get("error"), "partial_wall_seconds_NOT_full_label": observed["wall_seconds"],
                    "not_scored_as_zero_or_complete_query": True,
                })
                continue
            key = path.parent.name
            if key not in options:
                rows.append({"arm": key, "execution_completed": True,
                             "frozen_recommendation_has_no_same_WM_option": True})
                continue
            point = options[key]
            prediction, cost = point["forecast"], point["cost_preview"]
            frozen = read(path.parent / "FROZEN_FORECAST.json")
            if prediction["plan_structure_sha256"] != frozen["plan_structure_sha256"]:
                raise ValueError("same query/WM but different plans; cannot silently compare")
            label = actual_label(observed["actual_plan"])
            block = observed["block_issue"]
            whole = {key: block["snapshot_after"]["device_totals"][key]
                     - block["snapshot_before"]["device_totals"][key]
                     for key in ("read_requests", "write_requests", "read_bytes", "write_bytes")}
            trained = key in trained_keys
            row = {
                "arm": key, "query_id": q, "work_mem_mib": wm, "execution_completed": True,
                "cost_role": "in_sample_training_residual" if trained else
                             "prospective_new_observation_not_in_frozen_cost_model",
                "cost_in_sample_not_independent_accuracy": trained,
                "plan_shape_matches_recorded_query_execution": True,
                "CPU_seconds": comparison(cost["cpu_seconds_estimate"], observed["cpu_seconds"]),
                "AP_wall_seconds": comparison(cost["isolated_wall_seconds_estimate"], observed["wall_seconds"]),
                "logical_temp_read_bytes": comparison(prediction["logical_temp_read_bytes_estimate"],
                                                       label["read_bytes"]),
                "logical_temp_write_bytes": comparison(prediction["logical_temp_write_bytes_estimate"],
                                                        label["write_bytes"]),
                "foreground_read_requests": comparison(cost["physical_IO_proxy"]["read_requests"],
                                                         block["read_requests"]),
                "foreground_read_bytes": comparison(cost["physical_IO_proxy"]["read_bytes"], block["read_bytes"]),
                "write_requests_proxy_versus_whole_device_NOT_owned_truth": comparison(
                    cost["physical_IO_proxy"]["write_requests"], whole["write_requests"], False),
                "write_bytes_proxy_versus_whole_device_NOT_owned_truth": comparison(
                    cost["physical_IO_proxy"]["write_bytes"], whole["write_bytes"], False),
                "forecast_cardinality_comparisons": [],
                "mixed_TP_TPS_not_measured_by_this_isolated_AP_execution": True,
            }
            predicted_nodes = {o["path"]: o for o in prediction["operators"]}
            for value in cardinalities(observed["actual_plan"][0]["Plan"]):
                if value["comparable"]:
                    item = {
                        "path": value["path"], "node_type": value["node_type"],
                        "rows_per_call": comparison(
                            predicted_nodes[value["path"]]["output_rows_per_call"],
                            value["actual_rows_per_loop"]),
                        "calls": comparison(predicted_nodes[value["path"]]["calls"], value["actual_loops"]),
                    }
                    if value["pre_having_groups"] is not None:
                        evidence = predicted_nodes[value["path"]]["details"].get("pre_having_group_evidence")
                        if evidence:
                            item["pre_HAVING_groups"] = comparison(
                                evidence["estimated_pre_having_groups"], value["pre_having_groups"])
                    row["forecast_cardinality_comparisons"].append(item)
            development = (args.development_replay / ("q%d_REPAIRED_KERNEL_FORECAST.json" % q)
                           if args.development_replay else None)
            if development and development.exists():
                changed = read(development)
                if changed["work_mem_mib"] == wm:
                    row["post_repair_development_replay_NOT_independent"] = {
                        "logical_temp_write_bytes": comparison(
                            changed["logical_temp_write_bytes_estimate"], label["write_bytes"]),
                        "uses_actual_N_G_as_predictive_input": False,
                        "independent_new_execution_after_repair_completed": False,
                    }
                    sources.append(development)
            rows.append(row)
            sources += [path, path.parent / "FROZEN_FORECAST.json"]
    tp = read(args.run / "CURRENT_TP_MODEL_v1.json")
    surge = read(args.recommendation / "S5_CURRENT_TP_MODEL.json")
    baselines = {
        "TP128_only_holdout": [{
            "shared_buffers_mib": r["shared_buffers_mib"],
            "TPS": comparison(r["sustainable_tps"], r["heldout_TPS"]),
        } for r in tp["rows"]],
        "TP128_plus_16_only_holdout": [{
            "shared_buffers_mib": r["shared_buffers_mib"],
            "TPS": comparison(r["sustainable_tps"], r["heldout_TPS"]),
        } for r in surge["rows"]],
        "these_are_NOT_mixed_five_stage_TPS_validation": True,
    }
    result = {
        "schema": "jmt.frozen-recommendation-accuracy-audit/v1",
        "audited_at": datetime.now().astimezone().isoformat(),
        "recommendation_created_at": report["created_at"],
        "frozen_recommendation": str(report_path.resolve()), "rows": rows,
        "baseline_holdouts": baselines,
        "final_mixed_stage_TPS": [
            {"stage": s["stage"], "frozen_predicted_TPS": s["result"]["selected"]["predicted_tps"],
             "same_configuration_full_mixed_observed_TPS": None,
             "relative_error_pct": None,
             "status": "unverifiable_no_corresponding_mixed_measurement"}
            for s in report["stages"]],
        "new_holdouts_are_not_refitted_into_frozen_model": True,
        "all_errors_use_observed_value_as_denominator": True,
        "automatic_recommendation_qualified": False,
        "source_artifacts": [{"path": str(p.resolve()), "sha256": sha(p)}
                             for p in sorted(set(sources))],
    }
    (args.out / "ACCURACY_AUDIT.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps([{
        "arm": r["arm"], "cost_role": r.get("cost_role"),
        "CPU_error_pct": r.get("CPU_seconds", {}).get("signed_relative_error_pct"),
        "wall_error_pct": r.get("AP_wall_seconds", {}).get("signed_relative_error_pct"),
        "spill_write_error_pct": r.get("logical_temp_write_bytes", {}).get("signed_relative_error_pct"),
        "completed": r["execution_completed"],
    } for r in rows], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
