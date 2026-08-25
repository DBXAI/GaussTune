#!/usr/bin/env python3
"""Run one AP query repeatedly for a bounded resource-measurement window."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.stage_execution import ap_gsql_command


CURRENT_CHILD = None


def _terminate_child(_signum, _frame) -> None:
    global CURRENT_CHILD
    if CURRENT_CHILD is not None and CURRENT_CHILD.poll() is None:
        try:
            os.killpg(CURRENT_CHILD.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    raise SystemExit(143)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--work-mem", type=int, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--application-name", required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    if args.work_mem <= 0 or args.duration_seconds <= 0:
        parser.error("work_mem and duration must be positive")
    config = json.loads(args.runtime_config.read_text(encoding="utf-8"))
    query_files = config.get("ap_query_files", {})
    if args.query not in query_files:
        raise ValueError("runtime config lacks AP query %s" % args.query)
    deadline = time.monotonic() + args.duration_seconds
    count = 0
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("w", encoding="utf-8") as handle:
        while time.monotonic() < deadline:
            command = ap_gsql_command(
                config,
                query_file=Path(str(query_files[args.query])),
                work_mem_mb=args.work_mem,
                application_name=args.application_name,
            )
            global CURRENT_CHILD
            CURRENT_CHILD = subprocess.Popen(
                list(command),
                stdout=subprocess.DEVNULL,
                stderr=handle,
                text=True,
                env=dict(os.environ),
                start_new_session=True,
            )
            status = CURRENT_CHILD.wait()
            CURRENT_CHILD = None
            if status != 0:
                raise RuntimeError(
                    "AP query %s failed with status %d" % (args.query, status)
                )
            count += 1
    print(json.dumps({
        "query": args.query,
        "work_mem_mb": args.work_mem,
        "executions": count,
        "duration_seconds": args.duration_seconds,
        "valid": count > 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _terminate_child)
    signal.signal(signal.SIGINT, _terminate_child)
    raise SystemExit(main())
