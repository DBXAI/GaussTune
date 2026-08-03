#!/usr/bin/env bash
# Same-scale, natural-completion probe for the S4 TP-SLO/backpressure decision.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/results/sf85_s4_backpressure_probe_20260801}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data
CALIBRATION="$ROOT/results/input/tp_cpu_calibration.json"
ORIGINAL_SHA=d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c
STATE="$OUT/control_state.json"
EVENTS="$OUT/events.jsonl"
AUDIT="$OUT/control_audit.jsonl"

# The controller changes grants only for future sessions.  The S5 profile is
# present for completeness; the probe's assertion is S4 block-new behavior.
S1='q1=1;q3=1150;q5=996;q7=1083;q9=1174;q13=1024;q18=4096;q21=2968'
S2="$S1"
S3='q1=1;q3=1150;q5=996;q7=1083;q9=1174;q13=1024;q18=4096;q21=2968'
S4="$S3"
S5='q1=1;q3=1150;q5=996;q7=1083;q9=1174;q13=1024;q18=512;q21=2968'

as_omm() {
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; $*"
}

restart_with_sb() {
  local sb_mb="$1"
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${sb_mb}MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  sync
  echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_sf85_s4_probe.log"
  local configured
  configured="$(as_omm "$GAUSSHOME/bin/gsql -d postgres -t -A -c 'show shared_buffers;'" | tr -d '[:space:]')"
  [[ "$configured" == "$((sb_mb / 1024))GB" || "$configured" == "${sb_mb}MB" ]] || {
    echo "shared_buffers verification failed: expected ${sb_mb}MB, got ${configured}" >&2
    return 1
  }
}

restore() {
  restart_with_sb 8192 || true
}

mkdir -p "$OUT"
[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || {
  echo "refusing to run against a modified gaussdb" >&2
  exit 1
}
[[ -s "$CALIBRATION" ]] || { echo "missing TP calibration: $CALIBRATION" >&2; exit 1; }
[[ -s "$OUT/run_summary.json" ]] && { echo "reuse completed probe: $OUT"; exit 0; }

trap restore EXIT
restart_with_sb 2048

python3 "$ROOT/bin/ppt_stage_action_controller.py" \
  --events-file "$EVENTS" --state-file "$STATE" --audit-file "$AUDIT" \
  --stage1-work-mem "$S1" --stage2-work-mem "$S2" \
  --stage3-work-mem "$S3" --stage4-work-mem "$S4" --stage5-work-mem "$S5" \
  --stage1-ap-cap 8 --stage2-ap-cap 16 --stage3-ap-cap 4 --stage4-ap-cap 4 --stage5-ap-cap 8 \
  --keep-queue-on-drain \
  > "$OUT/controller.log" 2>&1 &
CONTROLLER_PID=$!

python3 "$ROOT/bin/continuous_five_stage_workload.py" run \
  --out-dir "$OUT" \
  --phase-seconds 60 \
  --ap-arrival-intervals 1000,30,15,5,5 \
  --ap-query-cycle 9,13,18,21 \
  --ap-work-mem "$S2" \
  --ap-max-running 16 \
  --finish-after-running-drain \
  --control-state-file "$STATE" \
  --tpch-database h5_tpch --tpch-scale 85 \
  --tp-calibration-file "$CALIBRATION" \
  --block-trace \
  > "$OUT/runner_console.log" 2>&1

wait "$CONTROLLER_PID" || true
