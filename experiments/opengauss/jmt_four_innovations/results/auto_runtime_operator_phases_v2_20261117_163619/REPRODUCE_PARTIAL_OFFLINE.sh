#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$HERE/source_snapshot/src:$ROOT/vendor"
"${PYTHON_BIN:-${JMT_PYTHON:-python3}}" - "$HERE" <<'PYCODE'
import json,hashlib,sys
from pathlib import Path
from jmt.innovation_4_performance.operator_phases import build_phases
from jmt.innovation_4_performance.boundary_observer import pair_events
out=Path(sys.argv[1]);p=json.loads((out/'PREREGISTERED.json').read_text());cat=json.loads((out/'CATALOG.json').read_text())
for rel,sha in p['source_sha256'].items():
 assert hashlib.sha256((out/'source_snapshot'/rel).read_bytes()).hexdigest()==sha
count=0
for observation in sorted(out.glob('*/OBSERVATION.json')):
 o=json.loads(observation.read_text());pred=json.loads((observation.parent/'PREDICTION.json').read_text())
 assert p['frozen_ns']<pred['frozen_ns']<o['started_ns']
 assert pair_events(o['native_events'])==o['native_pairs']
 g=build_phases(pred['forecast'],pred['blind_plan'],cat)
 for key in ('compute_process_cpu_ns',):
  g.pop(key,None);pred['program'].pop(key,None)
 assert g==pred['program'];count+=1
assert count==24
assert not (out/'customer_project_full_wm4/OBSERVATION.json').exists()
assert json.loads((out/'PARTIAL_STATUS.json').read_text())['missing_raw_for_failed_record']
print('24 partial calibration records verified; failed target raw remains missing, no holdout/model acceptance claimed')
PYCODE
