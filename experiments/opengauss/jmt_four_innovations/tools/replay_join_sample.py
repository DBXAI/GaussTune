#!/usr/bin/env python3
"""Freeze fresh sample-corrected forecasts on saved blind plans; no target fitting."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql, extract_json
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.group_synopsis import data_watermark_sql, watermark
from jmt.innovation_2_spill.join_sample import JoinSampleProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--plans-dir", type=Path, required=True)
    parser.add_argument("--sql", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    bundle = json.loads(args.bundle.read_text())
    relations = {tuple(r[k] for k in ("schema", "relation"))
                 for e in bundle["entries"] for r in e["scope"]["relations"]}
    profiles = [{"schema": s, "relation": r, "column": "unused"} for s, r in sorted(relations)]
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=20)
    current = watermark(client.json("h5_tpch", data_watermark_sql(profiles)))
    provider = JoinSampleProvider(bundle, current["sha256"])
    catalog = collect_catalog(client, "h5_tpch")
    sql = args.sql.read_text()
    base, corrected = ForecastSession(sql, catalog), ForecastSession(
        sql, catalog, cardinality_provider=provider)
    rows = []
    for path in sorted(args.plans_dir.glob("*_EXPLAIN_RAW.json")):
        # The CLI accepts a single query family's plan directory or prefix.
        if not path.name.startswith(args.sql.stem + "_"):
            continue
        wm = int(path.name.split("_wm", 1)[1].split("_", 1)[0])
        document = extract_json(json.loads(path.read_text())["stdout"])
        before, after = base.forecast(document, wm), corrected.forecast(document, wm)
        row = {"work_mem_mib": wm,
               "baseline_logical_temp_write_bytes": before["logical_temp_write_bytes_estimate"],
               "sample_corrected_logical_temp_write_bytes": after["logical_temp_write_bytes_estimate"],
               "baseline_structural_peak_mib": before["structural_peak_upper_mib"],
               "sample_corrected_structural_peak_mib": after["structural_peak_upper_mib"],
               "cardinality_effects": after["experimental_cardinality_effects"],
               "forecast_is_not_independent_accuracy_score": True}
        with (args.out / (path.name.replace("_EXPLAIN_RAW", "_SAMPLED_FORECAST"))).open("x") as stream:
            json.dump(after, stream, ensure_ascii=False, indent=2, allow_nan=False)
        rows.append(row)
    with (args.out / "SUMMARY.json").open("x") as stream:
        json.dump({"scope": "fresh preexecution sample evidence, old blind plans",
                   "uses_target_Actual_Rows": False, "rows": rows,
                   "automatic_recommendation_qualified": False},
                  stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
