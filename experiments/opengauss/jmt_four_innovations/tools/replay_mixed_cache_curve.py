#!/usr/bin/env python3
"""Build a conditional TP miss curve from one ORDERED, labeled TP+AP trace.

Pure offline profile replay. No database/trace collection, no cache-cost fit,
no query-specific SB offset and no automatic mixed-performance qualification.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jmt.innovation_1_cache.mixed_replay import replay_mixed_curve
from jmt.innovation_1_cache.schema import read_trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True,
                        help="bind stage, TP topology, AP plans/WM/concurrency/phase profile")
    parser.add_argument("--variants", type=Path, required=True,
                        help="list of shared_buffer_pages/os_cache_pages candidates")
    parser.add_argument("--sample-ppm", type=int, default=1000000)
    parser.add_argument("--startup-pages", type=int)
    parser.add_argument("--buffer-policy", choices=("clock", "candidate_fifo"), default="clock")
    parser.add_argument("--candidate-lists", type=int, default=1)
    parser.add_argument("--session-threads", type=Path,
                        help="JSON list from all native session trace headers, including no-ACCESS sampled sessions")
    parser.add_argument("--max-events", type=int, default=1000000)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    result = replay_mixed_curve(
        read_trace(args.trace), json.loads(args.variants.read_text()),
        context=json.loads(args.context.read_text()), sample_ppm=args.sample_ppm,
        startup_pages=args.startup_pages, buffer_policy=args.buffer_policy,
        candidate_lists=args.candidate_lists, max_events=args.max_events,
        session_threads=(json.loads(args.session_threads.read_text())
                         if args.session_threads else ()))
    result["created_at"] = datetime.now().astimezone().isoformat()
    result["offline_compute_seconds"] = time.perf_counter() - started
    result["source_artifacts"] = [
        {"path": str(p.resolve()), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in ((args.trace, args.context, args.variants)
                  + ((args.session_threads,) if args.session_threads else ()))]
    result["model_code_sha256"] = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (
            Path(__file__).resolve(),
            ROOT / "src/jmt/innovation_1_cache/mixed_replay.py",
            ROOT / "src/jmt/innovation_1_cache/cache_replay.py",
            ROOT / "src/jmt/innovation_1_cache/schema.py")}
    with (args.out / "MIXED_TP_CACHE_CURVE.json").open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({
        "curve_points": len(result["curve"]), "events": result["events_replayed"],
        "compute_seconds": result["offline_compute_seconds"],
        "TP_paths_count_TP_only_AP_still_evicts": True,
        "mixed_hard_bound_or_performance_qualified": False,
    }))


if __name__ == "__main__":
    main()
