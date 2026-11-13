#!/usr/bin/env python3
"""Replay existing stepwise diagnostics on completed NEW AP observations."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import extract_json
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_2_spill.diagnostics import diagnose_ap
from jmt.innovation_2_spill.labels import actual_label


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--sql-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    catalog = json.loads((args.collection / "CATALOG.json").read_text())
    rows = []
    for path in sorted(args.collection.glob("q*_wm*/MEASUREMENT.json")):
        observed = json.loads(path.read_text())
        if not observed.get("statement_completed"):
            rows.append({"arm": path.parent.name, "status": "execution_failed_NOT_zero"})
            continue
        q, wm = [int(x) for x in path.parent.name.replace("q", "").replace("_wm", ",").split(",")]
        sql = (args.sql_dir / ("q%d.sql" % q)).read_text()
        blind = extract_json(json.loads((path.parent / "EXPLAIN_RAW.json").read_text())["stdout"])
        forecast = json.loads((path.parent / "FROZEN_FORECAST.json").read_text())
        if (forecast.get("preexecution_key_group_sample_evidence")
                or any((effect.get("source") == "single_anchor_direct_join_sample_N")
                       for effect in forecast.get("experimental_cardinality_effects", [])
                       if isinstance(effect, dict))):
            rows.append({
                "arm": path.parent.name,
                "status": "diagnostic_unavailable",
                "reason": "Frozen prediction used direct sample statistics. This plain-session "
                          "diagnostic cannot replay those bindings; do not compare an "
                          "unsampled oracle chain against a sampled frozen forecast.",
                "frozen_forecast_and_execution_evidence_preserved": True,
            })
            continue
        try:
            label = actual_label(observed["actual_plan"])
            diagnostic = diagnose_ap(ForecastSession(sql, catalog), blind,
                                     observed["actual_plan"], wm, forecast, label)
            with (args.out / (path.parent.name + "_POSTEXECUTION.json")).open("x") as stream:
                json.dump(diagnostic, stream, ensure_ascii=False, indent=2, allow_nan=False)
            rows.append({
                "arm": path.parent.name, "status": "diagnosed",
                "wall_seconds": observed["wall_seconds"], "cpu_seconds": observed["cpu_seconds"],
                "whole_query_logical_spill_label": label,
                "frozen_spill": diagnostic["cumulative_failure_replacement"][0]["spill"],
                "after_exact_cardinality_diagnostic": diagnostic["all_eligible_rows_oracle"]["spill"],
                "not_deployable_or_fitted_correction": True,
            })
        except (ValueError, KeyError, TypeError) as error:
            rows.append({"arm": path.parent.name, "status": "diagnostic_unavailable",
                         "reason": type(error).__name__ + ": " + str(error)})
    with (args.out / "SUMMARY.json").open("x") as stream:
        json.dump({"schema": "jmt.current-AP-postexecution-audit/v1", "rows": rows,
                   "target_truth_never_replaces_normal_forecast": True,
                   "automatic_recommendation_qualified": False},
                  stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
