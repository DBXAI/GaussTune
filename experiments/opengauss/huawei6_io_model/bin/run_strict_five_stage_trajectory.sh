#!/usr/bin/env bash
# Stock-openGauss strict S1--S5 trajectory.  shared_buffers is fixed for this
# continuous run; the report does not claim an online SB resize.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:?usage: $0 <out_dir>}"
CALIBRATION="$ROOT/results/input/tp_cpu_calibration.json"
mkdir -p "$OUT"

python3 "$ROOT/bin/ppt_stage_action_controller.py" \
  --events-file "$OUT/events.jsonl" --state-file "$OUT/control_state.json" --audit-file "$OUT/controller_actions.jsonl" \
  --stage1-work-mem 'q3=1150' --stage2-work-mem 'q3=1150' \
  --stage3-work-mem 'q3=256' --stage4-work-mem 'q3=256' --stage5-work-mem 'q3=256' \
  --stage1-ap-cap 1 --stage2-ap-cap 2 --stage3-ap-cap 4 --stage4-ap-cap 4 --stage5-ap-cap 4 \
  --keep-queue-on-drain > "$OUT/controller.log" 2>&1 &
CONTROLLER_PID=$!
cleanup() { kill "$CONTROLLER_PID" 2>/dev/null || true; }
trap cleanup EXIT

python3 "$ROOT/bin/continuous_five_stage_workload.py" run \
  --out-dir "$OUT" --phase-seconds 120 \
  --ap-arrival-intervals '120,60,30,15,15' --ap-query-cycle 3 --ap-work-mem 'q3=256' \
  --ap-max-running 4 --ap-dynamic-budget-mb 5000 --finish-after-running-drain \
  --control-state-file "$OUT/control_state.json" --tpch-database h5_tpch --tpch-scale 85 \
  --tp-calibration-file "$CALIBRATION" \
  --tp-low-threads 8 --tp-low-rate 700 \
  --tp-saturated-threads 128 --tp-saturated-rate 4000 \
  --tp-high-threads 160 --tp-high-rate 4600 \
  --tp-low-warmup-seconds 20 --block-trace > "$OUT/runner_console.log" 2>&1

wait "$CONTROLLER_PID" || true
python3 "$ROOT/bin/evaluate_strict_five_stage_contract.py" \
  --run-dir "$OUT" --out "$OUT/strict_contract.json" > "$OUT/strict_contract_console.log"
