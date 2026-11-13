#!/usr/bin/env python3
"""Post-development same-WM spill replay, explicitly NOT an independent test.

Actual outcomes are loaded only here, after forecasts/allocation are frozen.
Aborted queries never supply a complete IO label. No refitting or DB changes.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.innovation_2_spill.labels import actual_label


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(predicted, observed):
    return {"predicted_bytes": predicted, "observed_bytes": observed,
            "relative_error_pct": 100 * (predicted - observed) / observed if observed else None,
            "absolute_error_bytes": abs(predicted - observed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allocation", type=Path, required=True)
    parser.add_argument("--observations", type=Path, action="append", required=True)
    parser.add_argument("--previous-forecasts", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    forecast_path = args.allocation / "QUERY_FORECASTS.json"
    report_path = args.allocation / "FIVE_STAGE_RESULTS.json"
    forecasts, allocation = read(forecast_path), read(report_path)
    previous = read(args.previous_forecasts) if args.previous_forecasts else {}
    sources = [forecast_path, report_path] + ([args.previous_forecasts] if args.previous_forecasts else [])
    rows = []
    for directory in args.observations:
        for path in sorted(directory.glob("q*_wm*/MEASUREMENT.json")):
            measurement = read(path)
            key = path.parent.name
            sources.append(path)
            if measurement.get("statement_completed") is not True:
                rows.append({"arm": key, "execution_completed": False,
                             "failure": measurement.get("error"),
                             "incomplete_execution_not_scored_as_zero": True})
                continue
            forecast = forecasts.get(key)
            frozen_path = path.parent / "FROZEN_FORECAST.json"
            sources.append(frozen_path)
            if (forecast is None or forecast["plan_structure_sha256"]
                    != read(frozen_path)["plan_structure_sha256"]):
                rows.append({"arm": key, "execution_completed": True,
                             "not_comparable_missing_same_WM_and_plan_shape": True})
                continue
            label = actual_label(measurement["actual_plan"])
            row = {
                "arm": key, "execution_completed": True,
                "same_WM_and_plan_shape": True, "independent_new_validation": False,
                "read": compare(forecast["logical_temp_read_bytes_estimate"], label["read_bytes"]),
                "write": compare(forecast["logical_temp_write_bytes_estimate"], label["write_bytes"]),
            }
            if key in previous:
                old = previous[key].get("forecast", previous[key])
                row["previous_write"] = compare(old["logical_temp_write_bytes_estimate"],
                                                label["write_bytes"])
            rows.append(row)
    baseline = []
    for stage in allocation["known_cohort"]["stages"]:
        qrows = [forecasts["q%d_wm512" % q] for q in stage["AP_queries"]]
        old_bytes = sum(f["logical_temp_read_bytes_estimate"]
                        + f["logical_temp_write_bytes_estimate"] for f in qrows)
        selected = stage["selection"]["logical_temp_read_plus_write_bytes"]
        baseline.append({
            "stage": stage["stage"], "baseline": "same SQLs, modeled WM=512 MiB each",
            "baseline_logical_temp_read_plus_write_bytes": old_bytes,
            "selected_logical_temp_read_plus_write_bytes": selected,
            "modeled_reduction_pct": 100 * (old_bytes - selected) / old_bytes if old_bytes else None,
            "is_observed_improvement": False,
            "baseline_contains_failed_or_unexecuted_queries": True,
        })
    result = {
        "schema": "jmt.innovation2-development-replay-audit/v1",
        "created_at": datetime.now().astimezone().isoformat(), "rows": rows,
        "modeled_baseline_comparison": baseline,
        "uses_observations_only_after_prediction_freeze": True,
        "independent_new_holdout_or_five_stage_mixed_validation": False,
        "new_selected_WM_executions_measured": False,
        "automatic_recommendation_qualified": False,
        "source_artifacts": [{"path": str(p.resolve()), "sha256": sha(p)} for p in sources],
    }
    with (args.out / "REPLAY_VALIDATION.json").open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"rows": rows, "modeled_baseline": baseline}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
