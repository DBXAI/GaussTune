#!/usr/bin/env python3
"""Bind independent TP training/holdout to a subsequent, frozen mixed matrix."""
import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from freeze_current_TP_points import checked_point, POINTS
from audit_configuration_TPS_matrix import (
    audit as audit_matrix, INITIAL, INITIAL_BACKEND, INITIAL_DRIVER, LANES, FRESH_LANES,
    FRESH_BACKEND, FRESH_DRIVER)


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(run):
    path = run / "FRESH_TP_POINTS.json"
    model, policy = read(path), read(run / "PRE_REGISTERED_POLICY.json")
    inputs = [path]
    for relative, expected in model["input_sha256"].items():
        p = run / relative
        if sha(p) != expected:
            raise ValueError("TP calibration/holdout raw input changed: " + relative)
        inputs.append(p)
    for relative, expected in policy["source_sha256"].items():
        p = run / "TP_collection_source" / relative
        if sha(p) != expected:
            raise ValueError("TP collector source snapshot binding changed")
        inputs.append(p)
    if (model["boot_id"] != policy["boot_id"] or model["training_repeat"] != 1
            or model["holdout_repeat"] != 2 or model["mixed_TPS_labels_used"]):
        raise ValueError("TP collection identity/train/holdout policy mismatch")
    collection_policy = read(run / "TP_calibration/COLLECTION_POLICY.json")
    if collection_policy["script_sha256"] != policy["source_sha256"][
            "tools/collect_current_tp_matrix.py"]:
        raise ValueError("TP collection script differs from preregistered snapshot")
    for sb in POINTS:
        train, test = [checked_point(read(
            run / "TP_calibration" / ("sb%d_r%02d" % (sb, r)) / "WINDOW.json"), sb, r)
            for r in (1, 2)]
        holdout = next(h for h in model["holdout"] if h["SB_mib"] == sb)
        if (train != model["points"]["TP128:" + str(sb)]
                or test != holdout["observed"]
                or holdout["predicted_TPS_from_repeat1"] != train["baseline_tps"]
                or holdout["absolute_relative_error"]
                != abs(train["baseline_tps"] / test["baseline_tps"] - 1)):
            raise ValueError("TP labels do not replay with preregistered train-only rule")
    mixed_run = run / "mixed"
    frozen_path = mixed_run / "frozen/FROZEN_MATRIX.json"
    frozen = read(frozen_path)
    if (frozen["fresh_TP_points"] != model or frozen["fresh_TP_points_sha256"] != sha(path)
            or frozen["boot_id"] != model["boot_id"]
            or frozen["postmaster_start_time"] != model["postmaster_start_time"]
            or frozen["machine_fingerprint"] != model["machine_fingerprint"]):
        raise ValueError("mixed prediction not bound to independent TP calibration")
    mixed = audit_matrix(mixed_run)
    calibration = read(run / "TP_calibration/MATRIX.json")
    restore = read(run / "TP_calibration/SB_RESTORE.json")
    if (not calibration["complete"] or not calibration["restoration_verified"]
            or not restore["config_restored_byte_exact"]
            or not restore["server_status_matches_original"]):
        raise ValueError("TP calibration not complete/restored")
    model_heldout_mape = statistics.mean(h["absolute_relative_error"] for h in model["holdout"])
    if model_heldout_mape != model["heldout_TP_only_MAPE"]:
        raise ValueError("TP holdout MAPE differs")
    inputs.append(frozen_path)
    level_degradation = {}
    episodes = [e for row in mixed["configurations"] for e in row["episodes"]]
    for lane in LANES + FRESH_LANES:
        usable = [e for e in episodes if e["lanes"][lane]["predicted_TPS"] is not None]
        if not usable:
            level_degradation[lane] = None
            continue
        diagnostics = [e["lanes"][lane]["diagnostic_not_refit"] for e in usable]
        level_degradation[lane] = {
            "numeric_windows": len(usable),
            "internal_TP_level_mean_bias_TPS": statistics.mean(
                d["baseline_bias_TPS"] for d in diagnostics),
            "internal_TP_level_MAE_TPS": statistics.mean(
                abs(d["baseline_bias_TPS"]) for d in diagnostics),
            "AP_degradation_mean_error_TPS": statistics.mean(
                d["AP_degradation_error_TPS"] for d in diagnostics),
            "AP_degradation_MAE_TPS": statistics.mean(
                abs(d["AP_degradation_error_TPS"]) for d in diagnostics),
            "mixed_TPS_MAE": statistics.mean(
                abs(e["lanes"][lane]["predicted_TPS"] - e["actual_TPS"]) for e in usable),
            "retention_fraction_MAE_percentage_points": statistics.mean(
                abs(d["retention_fraction_error_percentage_points"]) for d in diagnostics),
            "adjacent_controls_and_target_labels_for_diagnosis_only_not_refit": True}
    competition = []
    for row in mixed["configurations"]:
        sb = row["configuration"]["shared_buffers_mib"]
        base_cpu = model["points"]["TP128:" + str(sb)]["backend_cpu_seconds_per_tx"]
        observed_cpu = statistics.mean(
            e["observed"]["TP_CPU_seconds_per_commit_proxy"] for e in row["episodes"])
        competition.append({
            "configuration": row["configuration"]["key"],
            "calibrated_TP_only_backend_CPU_ms_per_commit": 1000 * base_cpu,
            "observed_mixed_backend_CPU_ms_per_commit_bracketed": 1000 * observed_cpu,
            "relative_increase_vs_independent_TP_only": observed_cpu / base_cpu - 1,
            "observed_AP_CPU_cores_by_repeat_bracketed": [
                e["observed"]["AP_CPU_cores_bracketed"] for e in row["episodes"]],
            "predicted_AP_CPU_cores_by_repeat_backend_lane": [
                e["lanes"][FRESH_BACKEND]["predicted_AP_CPU_cores"] for e in row["episodes"]],
            "diagnostic_not_query_coefficients_or_online_target_refit": True})
    result = {
        "schema": "jmt.fresh-TP-baseline-independent-effect/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "training_TP_only_points": model["points"],
        "heldout_TP_only": model["holdout"], "heldout_TP_only_MAPE": model_heldout_mape,
        "mixed_configuration_mean_MAPE": mixed["configuration_mean_MAPE"],
        "mixed_episode_MAPE": mixed["episode_MAPE"],
        "numeric_episode_coverage": mixed["numeric_episode_coverage"],
        "mixed_paired_change_summary": mixed["paired_change_summary"],
        "mixed_ranking": mixed["ranking"], "mixed_configurations": mixed["configurations"],
        "mixed_paired_changes": mixed["paired_changes"],
        "level_and_AP_degradation_diagnostic": level_degradation,
        "observed_competition_CPU_diagnostic": competition,
        "matched_legacy_and_fresh_forecasts_on_same_new_targets": True,
        "previous_mixed_targets_used_for_fit": False,
        "current_mixed_targets_or_TP_only_controls_used_for_fit": False,
        "fresh_TP_training_data_only_repeat1_exact_physical_SB": True,
        "historical_relative_AP_capacity_penalty_retained_unvalidated": True,
        "TP_physical_IO_proxy_recalibrated": False,
        "AP_costs_or_spill_or_phase_model_refitted": False,
        "backend_only_and_colocated_driver_ablations_preregistered_not_cherry_picked": True,
        "actual_AP_age_range_seconds": mixed["actual_AP_age_at_scored_start_range_seconds"],
        "strict_AP_age_or_phase_matched_causal_effect_verified": False,
        "original_full_SQL_EXPLAIN_ANALYZE_overhead_independently_qualified": False,
        "full_AP_lifecycle_S5_topology_or_global_optimum_verified": False,
        "restoration_verified": True, "candidate_promoted_to_default": False,
        "automatic_recommendation_qualified": False,
        "inputs_sha256": {str(p.relative_to(run)): sha(p) for p in inputs},
        "mixed_inputs_sha256": mixed["inputs_sha256"]}
    return result, mixed


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--mixed-audit-out", type=Path)
    p.add_argument("--csv", type=Path)
    args = p.parse_args()
    result, mixed = audit(args.run)
    for path, value in ((args.out, result), (args.mixed_audit_out, mixed)):
        if path is not None:
            with path.open("x") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
    if args.csv is not None:
        with args.csv.open("x", newline="") as stream:
            fields = ["SB_GiB", "WM_profile", "actual_r1_TPS", "actual_r2_TPS", "actual_mean_TPS",
                      "old_initial_TPS", "fresh_backend_initial_TPS", "fresh_with_driver_initial_TPS",
                      "old_error_pct", "fresh_backend_error_pct", "fresh_with_driver_error_pct",
                      "fresh_backend_conditional_TPS", "fresh_with_driver_conditional_TPS"]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in result["mixed_configurations"]:
                lanes = row["lanes"]
                writer.writerow({
                    "SB_GiB": row["configuration"]["shared_buffers_mib"] / 1024,
                    "WM_profile": row["configuration"]["WM_profile"],
                    "actual_r1_TPS": row["episodes"][0]["actual_TPS"],
                    "actual_r2_TPS": row["episodes"][1]["actual_TPS"],
                    "actual_mean_TPS": row["actual_mean_TPS"],
                    **{name: lanes[lane]["predicted_mean_TPS"] for name, lane in (
                        ("old_initial_TPS", INITIAL), ("fresh_backend_initial_TPS", INITIAL_BACKEND),
                        ("fresh_with_driver_initial_TPS", INITIAL_DRIVER),
                        ("fresh_backend_conditional_TPS", FRESH_BACKEND),
                        ("fresh_with_driver_conditional_TPS", FRESH_DRIVER))},
                    **{name: (100 * lanes[lane]["absolute_relative_error"]
                              if lanes[lane]["complete_predictions"] else None)
                       for name, lane in (("old_error_pct", INITIAL),
                                          ("fresh_backend_error_pct", INITIAL_BACKEND),
                                          ("fresh_with_driver_error_pct", INITIAL_DRIVER))}})
    print(json.dumps({k: result[k] for k in (
        "heldout_TP_only_MAPE", "mixed_configuration_mean_MAPE",
        "mixed_episode_MAPE", "numeric_episode_coverage")}))


if __name__ == "__main__":
    main()
