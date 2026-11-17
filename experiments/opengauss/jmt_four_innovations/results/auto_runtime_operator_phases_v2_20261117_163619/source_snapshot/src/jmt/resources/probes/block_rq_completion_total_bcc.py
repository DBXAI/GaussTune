#!/usr/bin/env python3
"""Exact low-overhead cumulative block completion totals.

The hot path only increments two per-CPU arrays.  User space prints cumulative
snapshots once per second; consumers difference adjacent snapshots, so no
completion is lost in a read-and-clear race.
"""

from __future__ import annotations

import signal
import sys
import time

from bcc import BPF


PROGRAM = r"""
BPF_PERCPU_ARRAY(completion_count, u64, 2);
BPF_PERCPU_ARRAY(completion_bytes, u64, 2);

TRACEPOINT_PROBE(block, block_rq_complete)
{
    if (args->dev != TARGET_DEVICE)
        return 0;
    u32 direction;
    if (args->rwbs[0] == 'R')
        direction = 0;
    else if (args->rwbs[0] == 'W')
        direction = 1;
    else
        return 0;
    u64 *count = completion_count.lookup(&direction);
    u64 *bytes = completion_bytes.lookup(&direction);
    if (count)
        (*count)++;
    if (bytes)
        (*bytes) += (u64)args->nr_sector * 512;
    return 0;
}
"""


def _total(table, key: int) -> int:
    return int(table.sum(table.Key(key)).value)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: block_rq_completion_total_bcc.py RAW_DEVICE_NUMBER")
    target = int(sys.argv[1], 0)
    if target < 0 or target > 0xFFFFFFFF:
        raise ValueError("raw device number is outside dev_t tracepoint range")
    bpf = BPF(text=PROGRAM.replace("TARGET_DEVICE", str(target)))
    counts = bpf.get_table("completion_count")
    byte_counts = bpf.get_table("completion_bytes")
    stopped = False

    def stop(_signal, _frame) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(
        "# HUAWEI7_BLOCK_COMPLETION_CUMULATIVE_V2 target_dev=%d" % target,
        flush=True,
    )
    deadline = time.monotonic_ns() + 1_000_000_000
    while not stopped:
        remaining = deadline - time.monotonic_ns()
        if remaining > 0:
            time.sleep(min(remaining / 1e9, .05))
            continue
        print("WINDOW,%d" % deadline)
        for direction in (0, 1):
            print("@count[0, %d]: %d" % (direction, _total(counts, direction)))
            print("@bytes[0, %d]: %d" % (
                direction, _total(byte_counts, direction),
            ))
        sys.stdout.flush()
        deadline += 1_000_000_000
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
