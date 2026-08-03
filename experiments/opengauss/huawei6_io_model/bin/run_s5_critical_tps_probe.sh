#!/usr/bin/env bash
# S5 contention probe: protect the original 700-TPS TP stream while a separate
# 3300-TPS surge stream and retained spilling AP queries are active.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SB_MB="${1:?usage: $0 <shared_buffers_mb> <out_dir>}"
OUT="${2:?usage: $0 <shared_buffers_mb> <out_dir>}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data
CALIBRATION="${TP_CALIBRATION:-$ROOT/results/input/tp_cpu_calibration.json}"
ORIGINAL_SHA=d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c
AP_RUNNING="${AP_RUNNING:-6}"
AP_WORK_MEM_MB="${AP_WORK_MEM_MB:-256}"
PHASE_SECONDS="${PHASE_SECONDS:-60}"
TP_SCRIPT="${TP_SCRIPT:-/usr/share/sysbench/oltp_read_only.lua}"
TP_DATABASE="${TP_DATABASE:-h5_tpcc}"
TP_USER="${TP_USER:-h5_tpuser}"
TP_PASSWORD="${TP_PASSWORD:-${HUAWEI6_TP_PASSWORD:-}}"
: "${TP_PASSWORD:?set TP_PASSWORD or HUAWEI6_TP_PASSWORD}"
TP_LOW_THREADS="${TP_LOW_THREADS:-8}"
TP_LOW_RATE="${TP_LOW_RATE:-700}"
TP_HIGH_THREADS="${TP_HIGH_THREADS:-128}"
TP_HIGH_RATE="${TP_HIGH_RATE:-4000}"
TP_LOW_WARMUP_SECONDS="${TP_LOW_WARMUP_SECONDS:-20}"

case "$SB_MB" in 4096|8192) ;; *) echo "SB must be 4096 or 8192MB" >&2; exit 2;; esac
[[ "$AP_RUNNING" =~ ^[1-9][0-9]*$ && "$AP_WORK_MEM_MB" =~ ^[1-9][0-9]*$ ]] || exit 2

as_omm() { su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; $*"; }
restart_sb() {
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${1}MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  sync; echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_s5_critical.log"
}
restore() { [[ -n "${CONTROLLER_PID:-}" ]] && kill "$CONTROLLER_PID" 2>/dev/null || true; restart_sb 8192 || true; }
[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || exit 1
[[ -s "$CALIBRATION" && ! -e "$OUT/run_summary.json" ]] || exit 1
mkdir -p "$OUT"
(( TP_HIGH_THREADS > TP_LOW_THREADS && TP_HIGH_RATE > TP_LOW_RATE )) || { echo "TP high profile must exceed protected profile" >&2; exit 2; }
printf '%s\n' "{\"scenario\":\"S5 critical TP stability under retained spilling AP\",\"shared_buffers_mb\":$SB_MB,\"ap_query\":\"Q3\",\"ap_work_mem_mb\":$AP_WORK_MEM_MB,\"s3_admitted_ap\":$AP_RUNNING,\"protected_tp_rate\":$TP_LOW_RATE,\"surge_tp_rate\":$((TP_HIGH_RATE - TP_LOW_RATE)),\"tp_low_threads\":$TP_LOW_THREADS,\"tp_high_threads\":$TP_HIGH_THREADS,\"tp_low_warmup_seconds\":$TP_LOW_WARMUP_SECONDS,\"tp_script\":\"$TP_SCRIPT\",\"tp_database\":\"$TP_DATABASE\"}" > "$OUT/profile.json"
trap restore EXIT
restart_sb "$SB_MB"

python3 "$ROOT/bin/ppt_stage_action_controller.py" \
  --events-file "$OUT/events.jsonl" --state-file "$OUT/control_state.json" --audit-file "$OUT/controller_actions.jsonl" \
  --stage1-work-mem "q3=$AP_WORK_MEM_MB" --stage2-work-mem "q3=$AP_WORK_MEM_MB" --stage3-work-mem "q3=$AP_WORK_MEM_MB" --stage4-work-mem "q3=$AP_WORK_MEM_MB" --stage5-work-mem "q3=$AP_WORK_MEM_MB" \
  --stage1-ap-cap 0 --stage2-ap-cap 0 --stage3-ap-cap "$AP_RUNNING" --stage4-ap-cap "$AP_RUNNING" --stage5-ap-cap "$AP_RUNNING" \
  --keep-queue-on-drain > "$OUT/controller.log" 2>&1 &
CONTROLLER_PID=$!

python3 "$ROOT/bin/continuous_five_stage_workload.py" run \
  --out-dir "$OUT" --phase-seconds "$PHASE_SECONDS" \
  --ap-arrival-intervals 1000,1000,1,1,1 --ap-query-cycle 3 --ap-work-mem "q3=$AP_WORK_MEM_MB" \
  --ap-max-running "$AP_RUNNING" --ap-dynamic-budget-mb 3000 --finish-after-running-drain \
  --control-state-file "$OUT/control_state.json" --tpch-database h5_tpch --tpch-scale 85 \
  --tp-calibration-file "$CALIBRATION" --block-trace \
  --sysbench-script "$TP_SCRIPT" --tp-database "$TP_DATABASE" --tp-user "$TP_USER" --tp-password "$TP_PASSWORD" \
  --tp-low-threads "$TP_LOW_THREADS" --tp-low-rate "$TP_LOW_RATE" --tp-high-threads "$TP_HIGH_THREADS" --tp-high-rate "$TP_HIGH_RATE" --tp-low-warmup-seconds "$TP_LOW_WARMUP_SECONDS" \
  > "$OUT/runner_console.log" 2>&1
wait "$CONTROLLER_PID"; unset CONTROLLER_PID
python3 "$ROOT/bin/audit_ppt_stage_contract.py" --run-dir "$OUT" --s2-min-dynamic-delta-mb 1 > "$OUT/contract_audit_console.log"
