#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PYTHON="${PYTHON_BIN:-${JMT_PYTHON:-python3}}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$HERE/source_snapshot/src:$ROOT/vendor:$ROOT/tools"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
exec "$PYTHON" "$HERE/source_snapshot/tools/validate_operator_phases.py" --replay "$HERE"
