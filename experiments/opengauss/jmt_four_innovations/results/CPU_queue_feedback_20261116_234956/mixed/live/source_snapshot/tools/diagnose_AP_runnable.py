#!/usr/bin/env python3
"""Read-only /proc queue-vs-blocked diagnostic; never a TPS/model fit input.

Keeps its own interval; does not relabel this as an exact commit/device window.
No SQL or BPF changes, no process priority/affinity changes.
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"), str(ROOT/"vendor"), str(ROOT/"tools")]
from collect_current_tp_matrix import dump, digest


def sample(pid, tid):
    begun = time.monotonic_ns()
    task = Path("/proc/%d/task/%d" % (pid, tid))
    fields = (task/"stat").read_text().rsplit(")", 1)[1].split()
    sched = list(map(int, (task/"schedstat").read_text().split()))
    io = {k: int(v) for k,v in (s.split(":") for s in (task/"io").read_text().splitlines())}
    return {
        "identity": [pid, tid, int(fields[19])], "nice": int(fields[16]),
        "CPU_ns": sched[0], "runqueue_wait_ns": sched[1], "timeslices": sched[2],
        "io_submissions_not_completions": io,
        "sample_ns": (begun+time.monotonic_ns())//2,
        "read_span_ns": time.monotonic_ns()-begun}


def summarize(before, after):
    if before["identity"] != after["identity"] or before["nice"] != after["nice"]:
        raise ValueError("thread identity or priority changed")
    duration = (after["sample_ns"]-before["sample_ns"])/1e9
    cpu = (after["CPU_ns"]-before["CPU_ns"])/1e9
    queue = (after["runqueue_wait_ns"]-before["runqueue_wait_ns"])/1e9
    if min(cpu, queue) < 0 or duration <= 0:
        raise ValueError("invalid /proc queue interval")
    return {
        "CPU_cores": cpu/duration, "runqueue_wait_fraction": queue/duration,
        "CPU_plus_runqueue_fraction": (cpu+queue)/duration,
        "other_or_blocked_fraction_approx": 1-(cpu+queue)/duration,
        "nice": before["nice"], "duration_seconds": duration,
        "not_exact_disjoint_kernel_state_accounting": True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live-matrix", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seconds", type=float, default=4)
    args = p.parse_args()
    if not .5 <= args.seconds <= 10:
        raise ValueError("bounded diagnostic duration required")
    pid = int(Path("/opt/openGauss/data/postmaster.pid").read_text().splitlines()[0])
    deadline = time.monotonic()+90
    case = None
    while time.monotonic() < deadline:
        for candidate in sorted(args.live_matrix.glob("SB*"), key=lambda p: p.stat().st_mtime, reverse=True):
            starts = list(candidate.glob("q*.START.json"))
            if len(starts) == 4 and not list(candidate.glob("q*.STOP.json")):
                case = candidate
                break
        if case is not None:
            break
        time.sleep(.2)
    if case is None:
        raise ValueError("no complete active AP cohort found within bound")
    identities = {p.name.split(".")[0]: json.loads(p.read_text())["identity"]
                  for p in case.glob("q*.START.json")}
    before = {q: sample(pid, row["linux_tid"]) for q,row in identities.items()}
    time.sleep(args.seconds)
    after = {q: sample(pid, row["linux_tid"]) for q,row in identities.items()}
    dump(args.out, {
        "schema": "jmt.own-interval-AP-runnable-diagnostic/v1", "case": case.name,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "source_sha256": digest(Path(__file__)), "before": before, "after": after,
        "queries": {q: summarize(before[q], after[q]) for q in identities},
        "AP_stop_started_during_diagnostic": bool(list(case.glob("q*.STOP.json"))),
        "used_for_fitting_or_forecasting_any_target_TPS": False,
        "exact_commit_window_IO_ownership_or_physical_blocking_cause_qualified": False})


if __name__ == "__main__":
    main()
