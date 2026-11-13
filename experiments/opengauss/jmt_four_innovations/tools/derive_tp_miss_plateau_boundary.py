#!/usr/bin/env python3
"""Apply the user-requested SB-miss plateau rule to a verified frozen replay.

Does not recollect, refit or overwrite old traces/curves/recommendations. The
policy is explicitly adopted AFTER the trace inspection, not a predeclared test.
"""
import argparse
import copy
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.innovation_1_cache.cache_boundary import (
    SCHEMA, load_boundary, derive_miss_plateau_boundary, file_sha,
    DEFAULT_MAX_MISS_REDUCTION_PER_10000_PER_GIB, MISS_PLATEAU_CRITERION,
)
from collect_current_tp_matrix import dump


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-boundary", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-miss-reduction-per-10000-per-gib", type=float,
                        default=DEFAULT_MAX_MISS_REDUCTION_PER_10000_PER_GIB)
    args = parser.parse_args()
    header = json.loads(args.source_boundary.read_text())
    original = load_boundary(args.source_boundary,
                             machine_fingerprint=header["machine_fingerprint"],
                             profile=header["TP_profile"])
    started = time.perf_counter()
    document = copy.deepcopy(original)
    document["schema"] = SCHEMA
    document["created_at"] = datetime.now().astimezone().isoformat()
    document["source_replay_created_at"] = original["created_at"]
    document["existing_verified_trace_replay_reused"] = True
    document["boundary_policy_changed_after_trace_inspection_at_user_request"] = True
    document["not_an_independent_test_of_threshold_selection"] = True
    document["new_database_measurements_collected"] = False
    for name, topology in document["topologies"].items():
        topology["historical_boundary_for_audit_only"] = topology["boundary"]
        topology["boundary"] = derive_miss_plateau_boundary(
            topology["curve"], args.max_miss_reduction_per_10000_per_gib)
    document["boundary_derivation_seconds"] = time.perf_counter() - started
    args.out.mkdir(parents=True, exist_ok=False)
    policy_path = args.out / "BOUNDARY_POLICY.json"
    dump(policy_path, {
        "created_at": document["created_at"],
        "criterion": MISS_PLATEAU_CRITERION,
        "max_miss_reduction_per_10000_per_gib": args.max_miss_reduction_per_10000_per_gib,
        "endpoint_policy": "right endpoint; 3->4 GiB plateau selects 4, not 3",
        "not_a_fixed_4096_mib_constant": True,
        "rule_changed_after_trace_inspection_at_user_request": True,
        "no_change_to_recorded_hit_counts": True,
    })
    # Resolve inherited relative bindings against their ORIGINAL document.
    for binding in document["source_artifacts"]:
        path = Path(binding["path"])
        if not path.is_absolute():
            binding["path"] = str((args.source_boundary.parent / path).resolve())
    for path in (args.source_boundary, policy_path):
        document["source_artifacts"].append({
            "path": str(path.resolve()), "sha256": file_sha(path)})
    result_path = args.out / "TP_CACHE_BOUNDARY.json"
    dump(result_path, document)
    load_boundary(result_path, machine_fingerprint=header["machine_fingerprint"],
                  profile=header["TP_profile"], require_miss_plateau=True)
    print(json.dumps({
        "B_high_by_topology_mib": {
            name: row["boundary"]["B_high_mib"] for name, row in document["topologies"].items()},
        "boundary_derivation_seconds": document["boundary_derivation_seconds"]}, indent=2))


if __name__ == "__main__":
    main()
