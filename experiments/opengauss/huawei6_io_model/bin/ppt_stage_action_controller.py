#!/usr/bin/env python3
"""Publish the executable, non-SB portion of the PPT five-stage policy.

The continuous workload polls a control-state JSON before launching each AP
statement.  This controller changes only controls stock openGauss can honor at
that boundary: future-session work_mem and AP admission.  It never claims to
resize shared_buffers or shrink an already running operator.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


STATES = {
    "stage1_memory_rich": ("stage1", 8, False, "S1: raise grants while AP pressure is low"),
    "stage2_reach_limit": ("stage2", 16, False, "S2: SB decrease is restart-emulated; keep AP grants high"),
    "stage3_protect_tp": ("stage3", 18, False, "S3: lower grants for newly admitted AP"),
    "stage4_backpressure": ("stage4", 4, True, "S4: queue every new AP request"),
    "stage5_tp_surge": ("stage5", 8, True, "S5: protect TP; active grants remain graceful debt"),
    "natural_drain": ("stage5", 8, False, "Drain: release queued AP naturally; do not cancel"),
}


def parse_grants(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in value.split(";"):
        query, separator, memory = item.strip().partition("=")
        if not separator or not query.lower().startswith("q"):
            raise ValueError(f"invalid grant assignment: {item!r}")
        result[str(int(query[1:]))] = int(memory)
    if not result or any(memory <= 0 for memory in result.values()):
        raise ValueError("grants must contain positive work_mem values")
    return result


def state_for(
    stage: str,
    grants: dict[str, dict[str, int]],
    admission_caps: dict[str, int] | None = None,
    keep_queue_on_drain: bool = False,
) -> dict[str, object]:
    tier, admitted, block_new, reason = STATES[stage]
    if admission_caps is not None:
        admitted = admission_caps.get(stage, admitted)
    if stage == "natural_drain" and keep_queue_on_drain:
        block_new = True
    # Keep state_for usable by the earlier three-tier callers and tests.  The
    # command-line path constructs the richer stage-specific map above.
    legacy_tiers = {
        "stage1": "s1_rich",
        "stage2": "high",
        "stage3": "low",
        "stage4": "low",
        "stage5": "low",
    }
    profile = tier if tier in grants else legacy_tiers[tier]
    return {
        "admitted_ap_clients": admitted,
        "block_new_ap": block_new,
        "work_mem_mb": grants[profile],
        "source": "ppt_stage_action_controller",
        "stage": stage,
        "reason": reason,
        "stock_opengauss_limit": (
            "shared_buffers transition requires stage restart; running AP work_mem is unchanged"
            if stage in {"stage2_reach_limit", "stage5_tp_surge"}
            else "future AP sessions only"
        ),
    }


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_audit(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def publish(
    stage: str,
    state_path: Path,
    audit_path: Path,
    grants: dict[str, dict[str, int]],
    admission_caps: dict[str, int] | None = None,
    keep_queue_on_drain: bool = False,
) -> None:
    state = state_for(stage, grants, admission_caps, keep_queue_on_drain)
    atomic_json(state_path, state)
    append_audit(audit_path, {"event": "control_publish", "wall_time": time.strftime("%F %T"), **state})


def run(args: argparse.Namespace) -> int:
    # Stage-specific profiles preserve plan-aware middle points such as
    # Q5@996MB.  Older high/low arguments remain fallbacks for callers that
    # have not supplied a distinct stage profile.
    grants = {
        "stage1": parse_grants(args.stage1_work_mem or args.s1_rich_work_mem),
        "stage2": parse_grants(args.stage2_work_mem or args.high_work_mem),
        "stage3": parse_grants(args.stage3_work_mem or args.low_work_mem),
        "stage4": parse_grants(args.stage4_work_mem or args.low_work_mem),
        "stage5": parse_grants(args.stage5_work_mem or args.low_work_mem),
    }
    known_queries = set(grants["stage2"])
    if any(set(value) != known_queries for value in grants.values()):
        raise ValueError("all grant tiers must cover the same query IDs")

    admission_caps = {
        "stage1_memory_rich": args.stage1_ap_cap,
        "stage2_reach_limit": args.stage2_ap_cap,
        "stage3_protect_tp": args.stage3_ap_cap,
        "stage4_backpressure": args.stage4_ap_cap,
        "stage5_tp_surge": args.stage5_ap_cap,
        "natural_drain": args.stage5_ap_cap,
    }
    if any(value < 0 for value in admission_caps.values()):
        raise ValueError("admission caps must be non-negative")
    publish("stage1_memory_rich", args.state_file, args.audit_file, grants, admission_caps,
            args.keep_queue_on_drain)
    offset = 0
    current = "stage1_memory_rich"
    while True:
        try:
            lines = args.events_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        for line_index, raw in enumerate(lines[offset:], start=offset):
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                # The workload appends JSONL while this process polls it.  A
                # read can catch precisely the final, not-yet-terminated row;
                # retain its offset and retry it on the next poll instead of
                # taking the controller down during S4 admission protection.
                if line_index == len(lines) - 1:
                    break
                raise
            offset += 1
            if event.get("event") == "phase_enter" and event.get("stage") in STATES:
                stage = str(event["stage"])
                if stage != current:
                    publish(stage, args.state_file, args.audit_file, grants, admission_caps,
                            args.keep_queue_on_drain)
                    current = stage
            elif event.get("event") == "tp_injection_stop":
                publish("natural_drain", args.state_file, args.audit_file, grants, admission_caps,
                        args.keep_queue_on_drain)
                current = "natural_drain"
            elif event.get("event") == "workload_complete":
                return 0
        time.sleep(args.poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-file", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--audit-file", required=True, type=Path)
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    parser.add_argument("--s1-rich-work-mem", default="q3=512;q5=512;q7=512;q9=512;q13=512;q18=1024;q21=1024")
    parser.add_argument("--high-work-mem", default="q3=1150;q5=1024;q7=1083;q9=1174;q13=1024;q18=4096;q21=2968")
    parser.add_argument("--low-work-mem", default="q3=256;q5=256;q7=256;q9=256;q13=256;q18=512;q21=512")
    parser.add_argument("--stage1-work-mem", default="", help="optional per-query S1 override")
    parser.add_argument("--stage2-work-mem", default="", help="optional per-query S2 override")
    parser.add_argument("--stage3-work-mem", default="", help="optional per-query S3 override")
    parser.add_argument("--stage4-work-mem", default="", help="optional per-query S4 override")
    parser.add_argument("--stage5-work-mem", default="", help="optional per-query S5 override")
    parser.add_argument("--stage1-ap-cap", type=int, default=8)
    parser.add_argument("--stage2-ap-cap", type=int, default=16)
    parser.add_argument("--stage3-ap-cap", type=int, default=18)
    parser.add_argument("--stage4-ap-cap", type=int, default=4)
    parser.add_argument("--stage5-ap-cap", type=int, default=8)
    parser.add_argument(
        "--keep-queue-on-drain",
        action="store_true",
        help="retain unstarted backpressure requests after injection stops",
    )
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("poll interval must be positive")
    try:
        return run(args)
    except (ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
