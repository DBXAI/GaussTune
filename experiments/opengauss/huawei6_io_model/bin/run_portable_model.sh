#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 /absolute/path/to/machine-config.json [modelctl-action]" >&2
  exit 2
fi

CONFIG="$(readlink -f "$1")"
ACTION="${2:-run-all}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "$CONFIG" ]]; then
  echo "Configuration file does not exist: $CONFIG" >&2
  exit 2
fi

WORKSPACE="$(python3 - "$CONFIG" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
print(os.path.abspath(os.path.expandvars(config["workspace"])))
PY
)"

mkdir -p "$WORKSPACE"
LOCK="$WORKSPACE/modelctl.lock"
LOG="$WORKSPACE/modelctl.log"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "Another Huawei6 model run owns $LOCK" >&2
  exit 1
fi

{
  printf '[%s] starting Huawei6 portable model action=%s\n' "$(date --iso-8601=seconds)" "$ACTION"
  printf '[%s] config=%s workspace=%s\n' "$(date --iso-8601=seconds)" "$CONFIG" "$WORKSPACE"
  python3 "$ROOT/bin/huawei6_modelctl.py" --config "$CONFIG" "$ACTION"
  printf '[%s] Huawei6 portable model action=%s completed\n' "$(date --iso-8601=seconds)" "$ACTION"
} 2>&1 | tee -a "$LOG"
