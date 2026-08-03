#!/usr/bin/env bash
# Execute the feasible, stateful PPT controls on stock openGauss.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$ROOT/results/ppt_state_machine_probe_20260731_sb4096}"
SB_MB="${SB_MB:-4096}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data
ORIGINAL_SHA=d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c
CALIBRATION="$ROOT/results/input/tp_cpu_calibration.json"

as_omm() {
  su - omm -c "export GAUSSHOME=/opt/openGauss; export LD_LIBRARY_PATH=/opt/openGauss/lib:/opt/openGauss/lib/postgresql; $*"
}

restore() {
  [[ -n "${CONTROLLER_PID:-}" ]] && kill "$CONTROLLER_PID" 2>/dev/null || true
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=8192MB'" || true
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_ppt_probe_restore.log" || true
}

[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || { echo "unexpected gaussdb"; exit 1; }
[[ -s "$CALIBRATION" ]] || { echo "missing TP calibration"; exit 1; }
trap restore EXIT

mkdir -p "$OUT_DIR"
as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${SB_MB}MB'"
as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
sync; echo 3 > /proc/sys/vm/drop_caches
as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_ppt_probe.log"

python3 "$ROOT/bin/ppt_stage_action_controller.py" \
  --events-file "$OUT_DIR/events.jsonl" \
  --state-file "$OUT_DIR/control_state.json" \
  --audit-file "$OUT_DIR/controller_actions.jsonl" \
  > "$OUT_DIR/controller.log" 2>&1 &
CONTROLLER_PID=$!

python3 "$ROOT/bin/continuous_five_stage_workload.py" run \
  --out-dir "$OUT_DIR" \
  --phase-seconds 30 \
  --ap-arrival-intervals 25,1,1,1,1 \
  --ap-query-cycle 18,21,18,21,9,3,5,7,13 \
  --ap-work-mem 'q3=512;q5=512;q7=512;q9=512;q13=512;q18=1024;q21=1024' \
  --ap-max-running 8 \
  --ap-dynamic-budget-mb 3500 \
  --control-state-file "$OUT_DIR/control_state.json" \
  --tp-calibration-file "$CALIBRATION" \
  --block-trace \
  > "$OUT_DIR/runner_console.log" 2>&1

wait "$CONTROLLER_PID"
unset CONTROLLER_PID
python3 "$ROOT/bin/audit_ppt_stage_contract.py" --run-dir "$OUT_DIR" --s2-min-dynamic-delta-mb 2000 \
  > "$OUT_DIR/contract_audit_console.log"
