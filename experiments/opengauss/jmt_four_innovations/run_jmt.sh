#!/usr/bin/env bash
# Relocatable entry point. No /root, owner, data path, disk, CPU or SB constants.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${JMT_PYTHON:-}" ]]; then
  PYTHON="$JMT_PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT/src:$ROOT/vendor${PYTHONPATH:+:$PYTHONPATH}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
if [[ "${1:-}" == "calibrate-phases" ]]; then
  shift
  exec "$PYTHON" "$ROOT/tools/validate_operator_phases.py" "$@"
fi
exec "$PYTHON" -m jmt.cli "$@"
