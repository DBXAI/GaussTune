#!/usr/bin/env python3
"""Combine disjoint, validated same-machine TP calibration domains.

Preserves all raw windows, exact durations, repeats1/2 train and repeat3 holdout.
No interpolation, new TP measurement, ratio rescaling or duplicate-SB selection.
Different original measurement times/durations remain explicit in source rows.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from build_current_tp_model import build
from recommend_current_five_stages import surge_baselines, TOPOLOGY
from collect_current_tp_matrix import digest, dump
from collect_current_tp_surge import GATED_LUA


def combine(paths, out, kind):
    loaded = [(p, json.loads(p.read_text())) for p in paths]
    if len(loaded) < 2:
        raise ValueError("at least two original matrices required")
    machine = loaded[0][1]["machine_fingerprint"]
    seen, rows, bindings = set(), [], []
    for path, matrix in loaded:
        if matrix["machine_fingerprint"] != machine:
            raise ValueError("matrix machines differ")
        if kind == "tp128":
            build(path)
        elif kind == "surge":
            surge_baselines(path, machine)
        else:
            raise ValueError("unknown TP topology")
        sizes = {r["shared_buffers_mib"] for r in matrix["rows"]}
        if seen & sizes:
            raise ValueError("duplicate SB domain; never choose best-looking source/repeat")
        seen.update(sizes)
        rows.extend(matrix["rows"])
        bindings.append({"path": str(path.resolve()), "sha256": digest(path),
                         "original_SB_domain_mib": sorted(sizes),
                         "original_created_at": matrix.get("created_at")})
    out.mkdir(parents=True, exist_ok=False)
    dump(out / "SOURCE.json", {
        "source_artifacts": bindings, "wrapper": GATED_LUA if kind == "surge" else None,
        "no_new_TP_measurements": True,
        "no_duplicate_SB_domain_or_interpolation": True,
        "original_window_durations_and_repeat_assignments_preserved": True,
        "different_collection_times_not_claimed_as_one_synchronous_run": True,
    })
    result = {
        "schema": ("jmt.current-TP-kernel-window-matrix/v1" if kind == "tp128"
                   else "jmt.current-TP-surge-kernel-window-matrix/v1"),
        "created_at": datetime.now().astimezone().isoformat(),
        "machine_fingerprint": machine, "rows": rows,
        "complete": True, "restoration_verified": True,
        "collection_error": None, "error": None,
        "topology": TOPOLOGY if kind == "surge" else "TP128",
        "source_artifacts": bindings, "no_new_TP_measurements": True,
        "automatic_recommendation_qualified": False,
    }
    matrix_path = out / "MATRIX.json"
    dump(matrix_path, result)
    checked = build(matrix_path) if kind == "tp128" else surge_baselines(matrix_path, machine)
    dump(out / ("TP_MODEL.json" if kind == "tp128" else "VALIDATED_BASELINES.json"), checked)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("tp128", "surge"), required=True)
    parser.add_argument("--matrices", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = combine(args.matrices, args.out, args.kind)
    print(json.dumps({"complete": True, "kind": args.kind,
                      "SB_domain_mib": sorted({r["shared_buffers_mib"] for r in result["rows"]}),
                      "rows": len(result["rows"]), "no_new_TP_measurements": True}))


if __name__ == "__main__":
    main()
