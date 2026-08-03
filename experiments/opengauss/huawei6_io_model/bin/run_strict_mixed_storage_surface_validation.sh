#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FILE_DIR="$ROOT/results/storage_latency_strict_20260802/files"
OUT="${OUT_DIR:-$ROOT/results/mixed_storage_surface_strict_20260802}"
TRAIN="$OUT/train"
HOLDOUT="$OUT/holdout"
FROZEN="$OUT/frozen/frozen_surface.json"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data

as_omm() {
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; $*"
}

restore_database() {
  if ! pgrep -x gaussdb >/dev/null; then
    as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_mixed_surface_restore.log" || true
  fi
}

trap restore_database EXIT
if pgrep -x gaussdb >/dev/null; then
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast"
fi
sync
echo 3 > /proc/sys/vm/drop_caches
python3 "$ROOT/bin/run_mixed_storage_surface.py" \
  --file-dir "$FILE_DIR" --out-dir "$TRAIN" --split train --seconds 15 --repeats 2
python3 "$ROOT/bin/mixed_storage_surface_formula.py" freeze \
  --training-csv "$TRAIN/mixed_storage_surface.csv" --out "$FROZEN"
python3 "$ROOT/bin/run_mixed_storage_surface.py" \
  --file-dir "$FILE_DIR" --out-dir "$HOLDOUT" --split holdout --seconds 15 --repeats 2
python3 "$ROOT/bin/mixed_storage_surface_formula.py" evaluate \
  --frozen "$FROZEN" --holdout-csv "$HOLDOUT/mixed_storage_surface.csv" --out-dir "$HOLDOUT/evaluation"
