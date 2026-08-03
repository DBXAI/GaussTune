#!/usr/bin/env bash
# Independent Huawei6 validation of all PPT five-stage candidate actions.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/five_stage_io_validation_20260731}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data
CALIBRATION="$ROOT/results/input/tp_cpu_calibration.json"
PARAMS="$ROOT/results/cache_state_matrix_20260731/model/cache_state_queue_tps_summary.json"
ORIGINAL_SHA=d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c
HIGH="q3=1150;q5=1024;q7=1083;q9=1174;q13=1024;q18=4096;q21=2968"
LOW="q3=256;q5=256;q7=256;q9=256;q13=256;q18=512;q21=512"
mkdir -p "$OUT_ROOT"
as_omm() { su - omm -c "export GAUSSHOME=/opt/openGauss; export LD_LIBRARY_PATH=/opt/openGauss/lib:/opt/openGauss/lib/postgresql; $*"; }
restart_sb() {
  local sb="$1"
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${sb}MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  sync; echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_five_stage_io.log"
}
restore() {
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=8192MB'" || true
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_five_stage_restore.log" || true
}
run_baseline() {
  local sb="$1"
  local name="baseline_sb${sb}_tp_only"
  local out="$OUT_ROOT/$name"
  [[ -s "$out/block_trace_attribution.csv" ]] && return
  mkdir -p "$out"; restart_sb "$sb"
  python3 "$ROOT/bin/continuous_five_stage_workload.py" run --out-dir "$out" --phase-seconds 30 \
    --ap-arrival-intervals 1000,1000,1000,1000,1000 --ap-query-cycle 18 --ap-work-mem 'q18=512' \
    --ap-max-running 0 --allow-no-ap --tp-calibration-file "$CALIBRATION" --block-trace > "$out/runner_console.log" 2>&1
}
run_candidate() {
  local name="$1"
  local sb="$2"
  local cap="$3"
  local grants="$4"
  local out="$OUT_ROOT/$name"
  [[ -s "$out/block_trace_attribution.csv" ]] && return
  mkdir -p "$out"; restart_sb "$sb"
  printf '{"label":"%s","shared_buffers_mb":%s,"ap_max_running":%s,"work_mem_profile":"%s"}\n' "$name" "$sb" "$cap" "$grants" > "$out/profile.json"
  python3 "$ROOT/bin/continuous_five_stage_workload.py" run --out-dir "$out" --phase-seconds 30 \
    --runtime-gated --stage-gate-timeout-seconds 120 --memory-high-watermark 0.95 \
    --memory-sustain-seconds 5 --queue-sustain-seconds 10 --ap-dynamic-budget-mb 5000 \
    --ap-arrival-intervals 25,2,2,1.5,1.5 --ap-query-cycle 18,21,9,3,5,7,13 \
    --ap-work-mem "$grants" --ap-max-running "$cap" --tp-calibration-file "$CALIBRATION" --block-trace \
    > "$out/runner_console.log" 2>&1
}
[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || { echo "unexpected gaussdb"; exit 1; }
[[ -s "$CALIBRATION" && -s "$PARAMS" ]] || { echo "missing calibration input"; exit 1; }
trap restore EXIT
run_baseline 4096
run_baseline 8192
run_candidate sb8192_high_cap8 8192 8 "$HIGH"
run_candidate sb4096_high_cap8 4096 8 "$HIGH"
run_candidate sb4096_low_cap8 4096 8 "$LOW"
run_candidate sb8192_low_cap8 8192 8 "$LOW"
run_candidate sb8192_low_cap4 8192 4 "$LOW"
run_candidate sb4096_low_cap4 4096 4 "$LOW"
python3 "$ROOT/bin/five_stage_io_recommendation.py" --root "$OUT_ROOT" --params "$PARAMS" --out-dir "$OUT_ROOT/model"
