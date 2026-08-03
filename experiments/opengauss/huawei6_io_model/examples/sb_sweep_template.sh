#!/usr/bin/env bash
set -euo pipefail

# Template for reproducing the shared_buffers sweep.
#
# This script changes openGauss shared_buffers and restarts the database.
# Read and adapt it before running on a production system.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DATA_DIR="${OPENGAUSS_DATA_DIR:-/opt/openGauss/data}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
GSQL="${OPENGAUSS_GSQL:-/opt/openGauss/bin/gsql}"
GUCTL="${OPENGAUSS_GS_GUC:-/opt/openGauss/bin/gs_guc}"
GSCTL="${OPENGAUSS_GS_CTL:-/opt/openGauss/bin/gs_ctl}"
export LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}:${LD_LIBRARY_PATH:-}"
export HUAWEI5_TPC5_ROOT="${HUAWEI5_TPC5_ROOT:-$PACKAGE_ROOT}"
export HUAWEI4_MODEL="${HUAWEI4_MODEL:-$PACKAGE_ROOT/bin/dual_cache_warmup.py}"
export TRACE_BOTH="${TRACE_BOTH:-$PACKAGE_ROOT/bpftrace/trace_both.bt}"

OUT_ROOT="${1:-$PACKAGE_ROOT/results/sb_sweep_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT_ROOT"

SB_LIST="${SB_LIST:-128 256 512 1024 1504 2048 4096 8192 12288 16384 24576}"

set_shared_buffers() {
  local sb_mb="$1"
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GUCTL set -D $DATA_DIR -c 'shared_buffers=${sb_mb}MB'"
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GSCTL restart -D $DATA_DIR -l /tmp/huawei5_pre_model_restart.log"
  su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GSQL -d postgres -Atc 'SHOW shared_buffers;'"
}

for sb_mb in $SB_LIST; do
  echo "=== shared_buffers=${sb_mb}MB ==="
  set_shared_buffers "$sb_mb"
  "$PACKAGE_ROOT/examples/run_one_cache_eval.sh" "$OUT_ROOT/sb${sb_mb}mb"
  rm -f "$OUT_ROOT/sb${sb_mb}mb/trace_full.log"
done

echo "sweep result root: $OUT_ROOT"
