#!/usr/bin/env bash
# Rebuild the equal-TPS recommendation and recompute the 1.94% validation
# from committed, password-free replay inputs and raw TPS logs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPRO="$ROOT/repro"
OUT="${1:-$REPRO/work/offline}"
mkdir -p "$OUT/prediction"

python3 "$ROOT/bin/huawei6_observation_driven_joint_controller.py" \
  --observations "$REPRO/inputs/observations_equal_tps.json" \
  --query-replay "$REPRO/inputs/query_plan_spill_predictions.csv" \
  --query-anchors "$REPRO/inputs/query_anchor_features.csv" \
  --cache-replay "$REPRO/inputs/joint_bidirectional_candidates.csv" \
  --machine-params "$REPRO/inputs/bpf_queue_tps_summary.json" \
  --io-materialization-params "$REPRO/inputs/io_latency_tps_summary.json" \
  --tp-miss-calibration "$REPRO/inputs/tp_miss_scale_calibration.json" \
  --tp-capacity "$REPRO/inputs/tp_high_capacity.json" \
  --out-dir "$OUT/prediction" >"$OUT/controller_console.log"

python3 "$ROOT/bin/validate_huawei6_observation_driven_run.py" \
  --recommendations "$OUT/prediction/observation_driven_recommendations_blinded.csv" \
  --run-root "$REPRO/reference/validation" \
  --out "$OUT/validation_report.json" >"$OUT/validation_console.log"

python3 - "$OUT" "$REPRO/reference" <<'PY'
import csv
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
reference = Path(sys.argv[2])
columns = ("inferred_action", "recommended_sb_mb", "recommended_work_mem", "block_new_ap", "formula_tps")

def rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))

actual = rows(out / "prediction/observation_driven_recommendations_blinded.csv")
expected = rows(reference / "prediction/observation_driven_recommendations_blinded.csv")
assert len(actual) == len(expected) == 5
for got, want in zip(actual, expected):
    assert {key: got[key] for key in columns} == {key: want[key] for key in columns}

report = json.loads((out / "validation_report.json").read_text(encoding="utf-8"))
checks = report["checks"]
assert checks["all_recommended_configurations_applied"]
assert checks["all_ap_naturally_completed"]
assert checks["protected_tp_variation_s1_s5_within_5_percent"]

for stage in report["stages"]:
    print(f"{stage['stage']}: {stage['inferred_action']}, SB={stage['recommended_sb_mb']}MB, TP={stage['protected_tp_tps']:.2f}")
print(f"S1-S5 protected TPS variation: {checks['protected_tp_variation_s1_s5_percent']:.6f}%")
PY
