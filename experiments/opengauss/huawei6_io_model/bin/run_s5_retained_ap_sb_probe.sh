#!/usr/bin/env bash
# Compare restart-time S5 shared_buffers choices while constrained AP remains active.
#
# Stock openGauss cannot change shared_buffers online.  Each invocation is one
# static-SB execution; together, SB=4GB and SB=8GB are the S5 restart-emulated
# counterfactual.  AP starts in S3, S4 blocks all new arrivals, and the same
# four already-admitted Q3 sessions remain active when S5 adds the TP surge.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SB_MB="${1:?usage: $0 <shared_buffers_mb> <out_dir>}"
OUT="${2:?usage: $0 <shared_buffers_mb> <out_dir>}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data
CALIBRATION="$ROOT/results/input/tp_cpu_calibration.json"
ORIGINAL_SHA=d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c
STATE="$OUT/control_state.json"
EVENTS="$OUT/events.jsonl"
AUDIT="$OUT/controller_actions.jsonl"

case "$SB_MB" in
  4096|8192) ;;
  *) echo "S5 probe supports only 4096 or 8192MB, got $SB_MB" >&2; exit 2 ;;
esac

as_omm() {
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; $*"
}

restart_with_sb() {
  local sb_mb="$1"
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${sb_mb}MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  sync
  echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_s5_retained_sb.log"
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
printf '{"scenario":"S5 retained constrained AP; restart-emulated SB counterfactual","shared_buffers_mb":%s,"ap_query":"Q3","ap_work_mem_mb":512,"s3_admitted_ap":4,"s4_blocks_new_ap":true,"s5_tp_threads":128,"s5_tp_rate":4000}\n' "$SB_MB" > "$OUT/profile.json"

trap restore EXIT
restart_with_sb "$SB_MB"

# Q3@512MB is a covered complex AP plan with predicted 10GiB temporary I/O.
# The same grant is deliberately retained from S3 through S5: it represents
# the post-graceful-reduction AP state. Existing sessions are never cancelled.
python3 "$ROOT/bin/ppt_stage_action_controller.py" \
  --events-file "$EVENTS" --state-file "$STATE" --audit-file "$AUDIT" \
  --stage1-work-mem 'q3=512' --stage2-work-mem 'q3=512' \
  --stage3-work-mem 'q3=512' --stage4-work-mem 'q3=512' --stage5-work-mem 'q3=512' \
  --stage1-ap-cap 0 --stage2-ap-cap 0 --stage3-ap-cap 4 --stage4-ap-cap 4 --stage5-ap-cap 4 \
  --keep-queue-on-drain \
  > "$OUT/controller.log" 2>&1 &
CONTROLLER_PID=$!

python3 "$ROOT/bin/continuous_five_stage_workload.py" run \
  --out-dir "$OUT" \
  --phase-seconds 45 \
  --ap-arrival-intervals 1000,1000,1,1,1 \
  --ap-query-cycle 3 \
  --ap-work-mem 'q3=512' \
  --ap-max-running 4 \
  --ap-dynamic-budget-mb 3500 \
  --finish-after-running-drain \
  --control-state-file "$STATE" \
  --tpch-database h5_tpch --tpch-scale 85 \
  --tp-calibration-file "$CALIBRATION" \
  --block-trace \
  > "$OUT/runner_console.log" 2>&1

wait "$CONTROLLER_PID"
unset CONTROLLER_PID
python3 "$ROOT/bin/audit_ppt_stage_contract.py" --run-dir "$OUT" --s2-min-dynamic-delta-mb 1 \
  > "$OUT/contract_audit_console.log"
