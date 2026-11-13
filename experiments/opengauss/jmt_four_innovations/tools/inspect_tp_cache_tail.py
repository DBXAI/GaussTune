#!/usr/bin/env python3
"""Inspect larger virtual SBs without rewriting a frozen boundary/recommendation.

Replays the same trace, verifies overlapping capacities against the frozen
curve, and reports remaining SB misses as well as hit-ratio threshold sensitivity.
No database access, parameter application or new physical-IO claim.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.innovation_1_cache.cache_boundary import file_sha, load_boundary, derive_boundary
from replay_tp_cache_boundary import accelerate, compile_accelerator
from collect_current_tp_matrix import dump


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    policy = json.loads((args.run / "POLICY.json").read_text())
    collection = json.loads((args.run / "COLLECTION.json").read_text())
    frozen_path = args.run / "replay_v3/TP_CACHE_BOUNDARY.json"
    frozen = load_boundary(frozen_path, machine_fingerprint=collection["machine_fingerprint"],
                           profile=policy["TP_profile"])
    args.out.mkdir(parents=True, exist_ok=False)
    replay_policy = json.loads((args.run / "replay_v3/REPLAY_POLICY.json").read_text())
    grid = [1536, 2048, 3072, 4096, 5120, 8192]
    dump(args.out / "TAIL_POLICY.json", {
        "created_at": datetime.now().astimezone().isoformat(),
        "virtual_SB_mib": grid, "same_source_trace_and_OS_assumption": True,
        "original_boundary_and_recommendations_will_not_be_rewritten": True,
        "new_capacity_is_replay_only_not_real_DB_measurement": True,
    })
    binary = args.out / "tp_mrc_replay"
    cpp = compile_accelerator(binary)
    events = args.run / "replay_v3/tp.events"
    windows = []
    for name in ("TP128_TRACE_ON", "S5_TRACE_ON"):
        w = json.loads((args.run / (name + ".WINDOW.json")).read_text())["measurement_window"]
        windows.append((w["start_ns"], w["end_ns"]))
    started = time.perf_counter()
    raw = accelerate(binary.resolve(), events.resolve(), policy["sample_ppm"], 8192,
                     replay_policy["OS_cache_mib"], int(collection["settings"]["pagewriters"]),
                     windows, grid)
    elapsed = time.perf_counter() - started
    dump(args.out / "REPLAY_COUNTS.json", raw)
    topologies = {}
    for index, name in enumerate(("TP128", "S5_128_plus_16")):
        baseline = {r["shared_buffers_mib"]: r for r in frozen["topologies"][name]["curve"]}
        points = []
        for entry in raw["rows"]:
            if entry.get("replay_error"):
                raise ValueError("tail replay failed: " + entry["replay_error"])
            counts = entry["windows"][index]
            n = counts["accesses"]
            if (not n or counts["anomalies"]
                    or sum(counts[k] for k in ("sb_hits", "os_hits", "disk")) != n):
                raise ValueError("invalid tail counts")
            row = {
                "shared_buffers_mib": entry["shared_buffers_mib"],
                "p_sb": counts["sb_hits"] / n, "p_os": counts["os_hits"] / n,
                "p_disk": counts["disk"] / n, "sampled_accesses": n,
                "sampled_SB_misses": n - counts["sb_hits"],
                "SB_misses_per_10000_logical_accesses": (n - counts["sb_hits"]) / n * 10000,
                "not_physical_disk_requests": True,
            }
            if row["shared_buffers_mib"] in baseline:
                old = baseline[row["shared_buffers_mib"]]
                if any(row[key] != old[key] for key in ("p_sb", "p_os", "p_disk", "sampled_accesses")):
                    raise ValueError("same-trace overlap differs from frozen replay")
            points.append(row)
        merged = dict(baseline)
        merged.update({r["shared_buffers_mib"]: r for r in points})
        sensitivity = {
            str(fraction): derive_boundary(list(merged.values()), fraction)["B_high_mib"]
            for fraction in (.99, .995, .999, .9999, .99999)}
        topologies[name] = {
            "points": points, "same_trace_overlap_exact_match": True,
            "relative_hit_fraction_sensitivity_B_high_mib": sensitivity,
            "sensitivity_is_not_a_new_accepted_policy": True,
        }
    dump(args.out / "CACHE_TAIL_COMPARISON.json", {
        "created_at": datetime.now().astimezone().isoformat(),
        "topologies": topologies, "replay_seconds": elapsed,
        "no_DB_mutation": True, "no_new_real_capacity_measurements": True,
        "physical_IO_qualified": False,
        "source_artifacts": [
            {"path": str(p.resolve()), "sha256": file_sha(p)}
            for p in (frozen_path, events, cpp)],
    })
    for name, topology in topologies.items():
        print(name)
        for row in topology["points"]:
            print("%4d MiB  P_SB=%.8f%%  SB misses/10000=%.6f  sampled_misses=%d" % (
                row["shared_buffers_mib"], row["p_sb"] * 100,
                row["SB_misses_per_10000_logical_accesses"], row["sampled_SB_misses"]))
        print("Sensitivity:", topology["relative_hit_fraction_sensitivity_B_high_mib"])
    print("replay_seconds=%.3f" % elapsed)


if __name__ == "__main__":
    main()
