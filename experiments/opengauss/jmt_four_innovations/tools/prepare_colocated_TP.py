#!/usr/bin/env python3
"""Freeze fresh exact-SB calibration; repeat 2 is evaluation-only."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.innovation_4_performance.colocated_tp import summarize_point


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    model = {
        "schema": "jmt.exact-SB-colocated-TP-points/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "training_repeat": 1, "holdout_repeat": 2, "points": {}, "holdout": [],
        "mixed_TPS_labels_used": False, "no_stage_or_query_ID_coefficients": True,
        "source_windows": {}, "automatic_recommendation_qualified": False}
    fingerprints = set()
    for folder, topology, points in (
            ("TP128_calibration", "TP128", (5120, 5376)),
            ("TP128_plus16_calibration", "S5_128_plus_16", (5376,))):
        matrix = json.loads((a.run / folder / "MATRIX.json").read_text())
        if not matrix["complete"] or not matrix["restoration_verified"]:
            raise ValueError("calibration must be complete and restored")
        fingerprints.add(matrix["machine_fingerprint"])
        for sb in points:
            observations = []
            for repeat in (1, 2):
                path = a.run / folder / ("sb%d_r%02d" % (sb, repeat)) / "WINDOW.json"
                window = json.loads(path.read_text())
                row = summarize_point(window, topology=topology)
                row["role"] = "calibration" if repeat == 1 else "heldout_TP_only"
                observations.append(row)
                model["source_windows"][str(path.relative_to(a.run))] = hashlib.sha256(
                    path.read_bytes()).hexdigest()
            train, test = observations
            model["points"][topology + ":" + str(sb)] = train
            model["holdout"].append({
                "topology": topology, "SB_mib": sb, "observed": test,
                "predicted_TPS_from_repeat1": train["baseline_tps"],
                "absolute_relative_error": abs(train["baseline_tps"] / test["baseline_tps"] - 1)})
    if len(fingerprints) != 1:
        raise ValueError("calibration machines differ")
    model["machine_fingerprint"] = fingerprints.pop()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("x") as stream:
        json.dump(model, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"points": model["points"], "holdout_MAPE": sum(
        r["absolute_relative_error"] for r in model["holdout"]) / len(model["holdout"])}))


if __name__ == "__main__":
    main()
