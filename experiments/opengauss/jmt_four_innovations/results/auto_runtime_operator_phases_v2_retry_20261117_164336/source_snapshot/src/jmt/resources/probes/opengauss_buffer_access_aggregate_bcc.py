#!/usr/bin/env python3
"""Aggregate openGauss Buffer Manager ACCESS->RETURN latency in BPF.

The mixed CPU/IO model needs a resource-only request latency, not a replay
trace.  Emitting one perf-buffer record per sampled request still creates
avoidable backpressure at TPCC rate.  This probe therefore keeps the sampled
ACCESS start in a BPF hash and accumulates only:

* sampled ACCESS->RETURN count;
* sampled ACCESS->RETURN latency sum; and
* probe map-update diagnostics.

The target dbNode makes the stream database-specific, so no TPS or workload
throughput label is needed.  The userspace result is a small JSON resource
measurement and contains no target TPS.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import time
from pathlib import Path

from bcc import BPF


GAUSSDB = "/opt/openGauss/bin/gaussdb"
SAMPLE_RATE = 64


BPF_SOURCE = r"""
#include <uapi/linux/ptrace.h>

#define TARGET_DB __TARGET_DB__
#define SAMPLE_RATE 64

BPF_HASH(read_entries, u32, u64, 65536);
BPF_ARRAY(stats, u64, 4);

static __always_inline void diag_inc(u32 key)
{
    u64 *value = stats.lookup(&key);
    if (value)
        __sync_fetch_and_add(value, 1);
}

static __always_inline int read_u32(u32 *out, const void *address)
{
    return bpf_probe_read(out, sizeof(*out), address);
}

int trace_read_entry(struct pt_regs *ctx)
{
    if ((bpf_get_prandom_u32() & (SAMPLE_RATE - 1)) != 0)
        return 0;
    u64 smgr = PT_REGS_PARM1(ctx);
    u32 db_node = 0;
    if (!smgr || read_u32(&db_node, (void *)(smgr + 4)) || db_node != TARGET_DB)
        return 0;
    u32 tid = (u32)bpf_get_current_pid_tgid();
    u64 start_ns = bpf_ktime_get_ns();
    if (read_entries.update(&tid, &start_ns) < 0)
        diag_inc(2);
    return 0;
}

int trace_read_return(struct pt_regs *ctx)
{
    u32 tid = (u32)bpf_get_current_pid_tgid();
    u64 *start = read_entries.lookup(&tid);
    if (!start)
        return 0;
    u64 end_ns = bpf_ktime_get_ns();
    u32 count_key = 0;
    u32 latency_key = 1;
    u64 *count = stats.lookup(&count_key);
    u64 *latency = stats.lookup(&latency_key);
    if (!count || !latency) {
        diag_inc(3);
    } else {
        __sync_fetch_and_add(count, 1);
        __sync_fetch_and_add(latency, end_ns - *start);
    }
    read_entries.delete(&tid);
    return 0;
}
"""


SYMBOLS = (
    (
        "trace_read_entry",
        "_Z17ReadBuffer_commonP16SMgrRelationDatacij14ReadBufferModeP24BufferAccessStrategyDataPbPK12XLogPhyBlock",
        False,
    ),
    (
        "trace_read_return",
        "_Z17ReadBuffer_commonP16SMgrRelationDatacij14ReadBufferModeP24BufferAccessStrategyDataPbPK12XLogPhyBlock",
        True,
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target_db_node", type=int)
    parser.add_argument("--gaussdb", type=Path, default=Path(GAUSSDB))
    parser.add_argument("--sample-rate", type=int, default=SAMPLE_RATE)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("openGauss uprobes require root")
    if args.target_db_node <= 0 or not args.gaussdb.is_file():
        parser.error("target dbNode and gaussdb binary must exist")
    if args.sample_rate != SAMPLE_RATE:
        parser.error("--sample-rate is fixed at %d" % SAMPLE_RATE)

    bpf = BPF(text=BPF_SOURCE.replace("__TARGET_DB__", str(args.target_db_node)))
    for function, symbol, is_return in SYMBOLS:
        attach = bpf.attach_uretprobe if is_return else bpf.attach_uprobe
        attach(name=str(args.gaussdb), sym=symbol, fn_name=function, pid=-1)

    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not stopping:
        time.sleep(0.1)

    def value(index: int) -> int:
        return int(bpf["stats"][ctypes.c_int(index)].value)

    print(json.dumps({
        "schema": "huawei7.buffer-access-aggregate/v1",
        "target_db_node": int(args.target_db_node),
        "sample_rate": int(args.sample_rate),
        "sample_count": value(0),
        "latency_sum_ns": value(1),
        "map_update_failures": value(2),
        "stats_map_failures": value(3),
        "valid": True,
    }, sort_keys=True), flush=True)
    return 0 if value(2) == 0 and value(3) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
