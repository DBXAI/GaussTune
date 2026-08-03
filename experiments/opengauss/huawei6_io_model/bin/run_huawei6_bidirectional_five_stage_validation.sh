#!/usr/bin/env bash
# Apply only the frozen, TPS-free recommendation, then validate afterwards.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECS="${1:-$ROOT/repro/reference/prediction/observation_driven_recommendations_blinded.csv}"
OUT="${2:-$ROOT/results/huawei6_five_stage_equal_tps_run}"
SECONDS_PER_STAGE="${SECONDS_PER_STAGE:-120}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
DATA_DIR="${OPENGAUSS_DATA_DIR:-/opt/openGauss/data}"
ORIGINAL_SHA="${OPENGAUSS_GAUSSDB_SHA256:-d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c}"
TPCH_SCALE="${TPCH_SCALE:-85}"
TPCH_DATABASE="${TPCH_DATABASE:-h5_tpch}"

as_omm() { su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; $*"; }

restart_with_sb() {
  local sb_mb="$1"
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${sb_mb}MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  sync
  echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_bidirectional_validation.log"
  local shown
  shown="$(as_omm "$GAUSSHOME/bin/gsql -d postgres -Atc 'show shared_buffers;'" | tr -d '[:space:]')"
  [[ "$shown" == "${sb_mb}MB" || "$shown" == "$((sb_mb / 1024))GB" ]]
}

field() {
  local stage="$1" column="$2"
  python3 - "$RECS" "$stage" "$column" <<'PY'
import csv, re, sys
with open(sys.argv[1], newline='') as fh:
    rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit('empty recommendation file')
    requested = sys.argv[3]
    aliases = {
        'joint_sb_mb': 'recommended_sb_mb',
        'joint_work_mem_assignments': 'recommended_work_mem',
    }
    column = requested if requested in rows[0] else aliases.get(requested, requested)
    target = sys.argv[2]
    observation = re.match(r'S(\d+)', target)
    observation_index = observation.group(1) if observation else target
    for index, row in enumerate(rows, start=1):
        matches_stage = row.get('stage') == target
        matches_observation = row.get('observation_index') == observation_index
        if matches_stage or matches_observation:
            print(row[column])
            break
    else:
        raise SystemExit(f'missing {target}')
PY
}

run_stage() {
  local short="$1" decision="$2" count="$3" queries="$4" threads="$5" rate="$6" interval="$7" protected_threads="$8" protected_rate="$9"
  local stage_out="$OUT/$short"
  [[ -s "$stage_out/stage_summary.json" ]] && return 0
  local sb grants
  sb="$(field "$decision" joint_sb_mb)"
  grants="$(field "$decision" joint_work_mem_assignments)"
  mkdir -p "$stage_out"
  restart_with_sb "$sb"
  python3 "$ROOT/bin/run_restart_stage_episode.py" \
    --out-dir "$stage_out" --stage "$short" --expected-sb-mb "$sb" --seconds "$SECONDS_PER_STAGE" \
    --ap-count "$count" --ap-work-mem-by-query "$grants" --ap-query-ids "$queries" --queue-interval-seconds "$interval" \
    --tp-threads "$threads" --tp-rate "$rate" --protected-threads "$protected_threads" --protected-rate "$protected_rate" \
    --tpch-scale "$TPCH_SCALE" --tpch-database "$TPCH_DATABASE" > "$stage_out/runner_console.log" 2>&1
}

[[ -s "$RECS" ]] || { echo "missing frozen recommendation: $RECS" >&2; exit 1; }
[[ -n "${HUAWEI6_TP_PASSWORD:-}" ]] || { echo "set HUAWEI6_TP_PASSWORD" >&2; exit 1; }
[[ -n "${HUAWEI6_AP_PASSWORD:-}" ]] || { echo "set HUAWEI6_AP_PASSWORD" >&2; exit 1; }
[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || { echo "refusing modified gaussdb" >&2; exit 1; }
mkdir -p "$OUT"
trap 'restart_with_sb 8192 || true' EXIT

# S1/S2 carry the same protected TP demand as S3/S4.  "Unsaturated" here
# means memory headroom remains; it must not be implemented as a lower TP
# offered load, otherwise stage-to-stage TPS stability is untestable.
run_stage S1 S1_memory_rich 1 18 128 4000 0 0 0
run_stage S2 S2_yield_sb_for_ap 2 18,21 128 4000 0 0 0
run_stage S3 S3_protect_tp 4 9,13,18,21 128 4000 0 0 0
run_stage S4 S4_backpressure 4 9,13,18,21 128 4000 15 0 0
run_stage S5 S5_tp_surge 2 18,21 144 4300 15 128 4000

if head -1 "$RECS" | grep -q 'observation_index'; then
  python3 "$ROOT/bin/validate_huawei6_observation_driven_run.py" \
    --recommendations "$RECS" --run-root "$OUT" --out "$OUT/validation_report.json"
else
  python3 "$ROOT/bin/validate_huawei6_bidirectional_decisions.py" \
    --recommendations "$RECS" --run-root "$OUT" --out "$OUT/validation_report.json"
fi
