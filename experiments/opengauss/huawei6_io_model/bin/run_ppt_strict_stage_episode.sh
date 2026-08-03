#!/usr/bin/env bash
# Run one strict PPT five-stage trajectory on unmodified stock openGauss.
#
# shared_buffers is static for the trajectory.  The companion repeated runner
# executes the same frozen pressure trajectory at 4GB and 8GB, then the
# evaluator stitches score windows using the preselected stage recommendation.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPEAT="${1:?usage: $0 <repeat> <shared_buffers_mb> <out_dir>}"
SB_MB="${2:?usage: $0 <repeat> <shared_buffers_mb> <out_dir>}"
OUT="${3:?usage: $0 <repeat> <shared_buffers_mb> <out_dir>}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data
CALIBRATION="$ROOT/results/input/tp_cpu_calibration.json"
ORIGINAL_SHA=d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c
PHASE_SECONDS="${PHASE_SECONDS:-30}"

case "$SB_MB" in
  4096|8192) ;;
  *) echo "supported shared_buffers values are 4096 or 8192MB" >&2; exit 2 ;;
esac
[[ "$REPEAT" =~ ^[1-9][0-9]*$ ]] || { echo "repeat must be a positive integer" >&2; exit 2; }
[[ "$PHASE_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo "PHASE_SECONDS must be a positive integer" >&2; exit 2; }

as_omm() {
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; $*"
}

restart_with_sb() {
  local sb_mb="$1"
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${sb_mb}MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  sync
  echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_strict_stage_episode.log"
  local configured
  configured="$(as_omm "$GAUSSHOME/bin/gsql -d postgres -t -A -c 'show shared_buffers;'" | tr -d '[:space:]')"
  [[ "$configured" == "${sb_mb}MB" || "$configured" == "$((sb_mb / 1024))GB" ]] || {
    echo "shared_buffers verification failed: expected ${sb_mb}MB, got ${configured}" >&2
    return 1
  }
}

restore() {
  [[ -n "${CONTROLLER_PID:-}" ]] && kill "$CONTROLLER_PID" 2>/dev/null || true
  restart_with_sb 8192 || true
}

[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || {
  echo "refusing to run against a modified gaussdb" >&2
  exit 1
}
[[ -s "$CALIBRATION" ]] || { echo "missing TP calibration: $CALIBRATION" >&2; exit 1; }
[[ ! -e "$OUT/run_summary.json" ]] || { echo "completed result exists: $OUT" >&2; exit 1; }
mkdir -p "$OUT"

# The values below are frozen before execution.  S1 has exactly one Q3 at
# 1150MB; S2 injects high-grant complex AP until dynamic pressure is observed;
# S3 makes only future AP sessions use the restricted grant set.  S4/S5 retain
# the admission stop.  Existing AP SQL is never cancelled or hot-resized.
printf '%s\n' \
  "{\"protocol\":\"strict_ppt_static_trajectory_v1\",\"repeat\":$REPEAT,\"shared_buffers_mb\":$SB_MB,\"phase_seconds\":$PHASE_SECONDS,\"s1_s2_grants\":\"q3=1150;q5=1024;q7=1083;q9=1174;q13=1024;q18=4096;q21=2968\",\"s3_to_s5_grants\":\"q3=512;q5=996;q7=512;q9=512;q13=512;q18=512;q21=512\",\"s4_s5_new_ap\":\"blocked\",\"drain\":\"started AP finishes naturally; queued AP remains queued\"}" \
  > "$OUT/profile.json"

trap restore EXIT
restart_with_sb "$SB_MB"

python3 "$ROOT/bin/ppt_stage_action_controller.py" \
  --events-file "$OUT/events.jsonl" --state-file "$OUT/control_state.json" --audit-file "$OUT/controller_actions.jsonl" \
  --stage1-work-mem 'q3=1150;q5=1024;q7=1083;q9=1174;q13=1024;q18=4096;q21=2968' \
  --stage2-work-mem 'q3=1150;q5=1024;q7=1083;q9=1174;q13=1024;q18=4096;q21=2968' \
  --stage3-work-mem 'q3=512;q5=996;q7=512;q9=512;q13=512;q18=512;q21=512' \
  --stage4-work-mem 'q3=512;q5=996;q7=512;q9=512;q13=512;q18=512;q21=512' \
  --stage5-work-mem 'q3=512;q5=996;q7=512;q9=512;q13=512;q18=512;q21=512' \
  --stage1-ap-cap 4 --stage2-ap-cap 4 --stage3-ap-cap 4 --stage4-ap-cap 4 --stage5-ap-cap 4 \
  --keep-queue-on-drain \
  > "$OUT/controller.log" 2>&1 &
CONTROLLER_PID=$!

python3 "$ROOT/bin/continuous_five_stage_workload.py" run \
  --out-dir "$OUT" \
  --phase-seconds "$PHASE_SECONDS" \
  --ap-arrival-intervals 25,1,1,1,1 \
  --ap-query-cycle 3,18,21,9,3,5,7,13 \
  --ap-work-mem 'q3=1150;q5=1024;q7=1083;q9=1174;q13=1024;q18=4096;q21=2968' \
  --ap-max-running 4 \
  --ap-dynamic-budget-mb 3500 \
  --finish-after-running-drain \
  --control-state-file "$OUT/control_state.json" \
  --tpch-database h5_tpch_sf10 --tpch-scale 10 \
  --tp-calibration-file "$CALIBRATION" \
  > "$OUT/runner_console.log" 2>&1

wait "$CONTROLLER_PID"
unset CONTROLLER_PID
python3 "$ROOT/bin/audit_ppt_stage_contract.py" --run-dir "$OUT" --s2-min-dynamic-delta-mb 1024 --s2-min-peak-ratio 2 \
  > "$OUT/contract_audit_console.log"
