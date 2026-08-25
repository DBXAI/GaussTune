#!/usr/bin/env python3
"""Capture only openGauss Buffer Manager ACCESS->RETURN pairs.

This is the low-overhead companion to ``opengauss_buffer_trace_bcc.py``.
The full probe also records PIN/REF/UNPIN/DIRTY/FLUSH state transitions, which
are useful for cache-replay evidence but create substantially more perf-buffer
traffic than is needed by the resource-only buffered TP request surface.

The collector using this probe needs only:

* the database-issued TP/AP buffer access count; and
* the ACCESS->RETURN wait for each access.

No TPS or throughput value is emitted by this process.  The binary stream
keeps the same fixed event layout and trailer as the full probe so the
existing loss checker and normalizer can consume it without a second parser.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import signal
import struct
import sys
from pathlib import Path

import lz4.frame
from bcc import BPF, PerfSWConfig, PerfType


MAGIC = b"H7BUFV3\0"
HEADER = struct.Struct("<8sIIII")
GAUSSDB = "/opt/openGauss/bin/gaussdb"


class Event(ctypes.Structure):
    _fields_ = [
        ("start_ns", ctypes.c_uint64),
        ("end_ns", ctypes.c_uint64),
        ("strategy_id", ctypes.c_uint64),
        ("tid", ctypes.c_uint32),
        ("kind", ctypes.c_uint32),
        ("spc_node", ctypes.c_uint32),
        ("db_node", ctypes.c_uint32),
        ("rel_node", ctypes.c_uint32),
        ("block_num", ctypes.c_uint32),
        ("bucket_node", ctypes.c_int32),
        ("fork_num", ctypes.c_int32),
        ("buffer_id", ctypes.c_int32),
        ("access_mode", ctypes.c_int32),
        ("strategy_type", ctypes.c_int32),
        ("ring_pages", ctypes.c_int32),
        ("observed_hit", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


ACCESS_RETURN = 1
STATS = 255
SAMPLE_RATE = 512


BPF_SOURCE = r"""
#include <uapi/linux/ptrace.h>

#define TARGET_DB __TARGET_DB__
#define BD_OFF_spcNode       0
#define BD_OFF_dbNode        4
#define BD_OFF_relNode        8
#define BD_OFF_bucketNode   12
#define BD_OFF_forkNum      16
#define BD_OFF_blockNum     20

#define K_ACCESS_RETURN 1
#define SAMPLE_RATE 512

struct read_entry_t {
    u64 start_ns;
    u64 strategy_id;
    u64 hit_ptr;
    u32 spc_node;
    u32 db_node;
    u32 rel_node;
    u32 block_num;
    s32 bucket_node;
    s32 fork_num;
    s32 access_mode;
    s32 strategy_type;
    s32 ring_pages;
};

struct event_t {
    u64 start_ns;
    u64 end_ns;
    u64 strategy_id;
    u32 tid;
    u32 kind;
    u32 spc_node;
    u32 db_node;
    u32 rel_node;
    u32 block_num;
    s32 bucket_node;
    s32 fork_num;
    s32 buffer_id;
    s32 access_mode;
    s32 strategy_type;
    s32 ring_pages;
    s32 observed_hit;
    u32 reserved;
};

BPF_HASH(read_entries, u32, struct read_entry_t, 65536);
/* Access-only capture has a much smaller kernel-side event path than the
 * complete cache-state probe.  A larger batch materially reduces the number
 * of perf_submit calls at TPCC rate without changing the on-disk records. */
#define BATCH_SIZE 128
struct batch_t {
    u32 count;
    u32 reserved;
    struct event_t rows[BATCH_SIZE];
};
BPF_PERCPU_ARRAY(batch_state, struct batch_t, 1);
BPF_PERF_OUTPUT(batch_output);
BPF_ARRAY(diagnostics, u64, 4);

static __always_inline void diag_inc(u32 key)
{
    u64 *value = diagnostics.lookup(&key);
    if (value)
        __sync_fetch_and_add(value, 1);
}

static __always_inline int read_u32(u32 *out, const void *address)
{
    /* Linux 5.4 uses the legacy helper for uprobe user-memory reads. */
    return bpf_probe_read(out, sizeof(*out), address);
}

static __always_inline int read_s32(s32 *out, const void *address)
{
    return bpf_probe_read(out, sizeof(*out), address);
}

static __always_inline void submit_event(
    struct pt_regs *ctx, struct event_t *event)
{
    u32 zero = 0;
    struct batch_t *batch = batch_state.lookup(&zero);
    if (!batch) {
        diag_inc(1);
        return;
    }
    u32 index = batch->count;
    if (index >= BATCH_SIZE) {
        diag_inc(1);
        batch->count = 0;
        return;
    }
    __builtin_memcpy(&batch->rows[index], event, sizeof(*event));
    index++;
    batch->count = index;
    if (index == BATCH_SIZE) {
        if (batch_output.perf_submit(ctx, batch, sizeof(*batch)) < 0)
            diag_inc(1);
        batch->count = 0;
    }
}

int flush_batch(struct bpf_perf_event_data *ctx)
{
    u32 zero = 0;
    struct batch_t *batch = batch_state.lookup(&zero);
    if (!batch || batch->count == 0)
        return 0;
    if (batch_output.perf_submit(ctx, batch, sizeof(*batch)) < 0)
        diag_inc(1);
    batch->count = 0;
    return 0;
}

int trace_read_entry(struct pt_regs *ctx)
{
    /* Sample ACCESS->RETURN pairs to keep probe overhead bounded.  The
     * exact TP request denominator comes from pg_stat_database; this stream
     * is used for the unbiased latency estimate. */
    if ((bpf_get_prandom_u32() & (SAMPLE_RATE - 1)) != 0)
        return 0;
    u32 tid = (u32)bpf_get_current_pid_tgid();
    u64 smgr = PT_REGS_PARM1(ctx);
    u32 db_node = 0;
    if (!smgr || read_u32(&db_node, (void *)(smgr + 4)) || db_node != TARGET_DB)
        return 0;

    struct read_entry_t entry = {};
    entry.start_ns = bpf_ktime_get_ns();
    entry.db_node = db_node;
    read_u32(&entry.spc_node, (void *)(smgr + BD_OFF_spcNode));
    read_u32(&entry.rel_node, (void *)(smgr + BD_OFF_relNode));
    read_s32(&entry.bucket_node, (void *)(smgr + BD_OFF_bucketNode));
    entry.bucket_node = (s16)entry.bucket_node;
    entry.fork_num = (s32)PT_REGS_PARM3(ctx);
    entry.block_num = (u32)PT_REGS_PARM4(ctx);
    entry.access_mode = (s32)PT_REGS_PARM5(ctx);
    entry.strategy_id = PT_REGS_PARM6(ctx);
    entry.strategy_type = -1;
    if (entry.strategy_id) {
        read_s32(&entry.strategy_type, (void *)entry.strategy_id);
        read_s32(&entry.ring_pages, (void *)(entry.strategy_id + 4));
    }
    u64 stack_pointer = PT_REGS_SP(ctx);
    bpf_probe_read(
        &entry.hit_ptr, sizeof(entry.hit_ptr), (void *)(stack_pointer + 8));
    if (read_entries.update(&tid, &entry) < 0)
        diag_inc(0);
    return 0;
}

int trace_read_return(struct pt_regs *ctx)
{
    u32 tid = (u32)bpf_get_current_pid_tgid();
    struct read_entry_t *entry = read_entries.lookup(&tid);
    if (!entry)
        return 0;

    struct event_t event = {};
    event.start_ns = entry->start_ns;
    event.end_ns = bpf_ktime_get_ns();
    event.strategy_id = entry->strategy_id;
    event.tid = tid;
    event.kind = K_ACCESS_RETURN;
    event.spc_node = entry->spc_node;
    event.db_node = entry->db_node;
    event.rel_node = entry->rel_node;
    event.block_num = entry->block_num;
    event.bucket_node = entry->bucket_node;
    event.fork_num = entry->fork_num;
    event.buffer_id = (s32)PT_REGS_RC(ctx);
    event.access_mode = entry->access_mode;
    event.strategy_type = entry->strategy_type;
    event.ring_pages = entry->ring_pages;
    event.observed_hit = -1;
    if (entry->hit_ptr) {
        u8 hit = 0;
        if (!bpf_probe_read(&hit, sizeof(hit), (void *)entry->hit_ptr))
            event.observed_hit = hit ? 1 : 0;
    }
    if (event.observed_hit < 0)
        event.observed_hit = 0;
    submit_event(ctx, &event);
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
    parser.add_argument("--perf-pages", type=int, default=8192)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("openGauss uprobes require root")
    if args.target_db_node <= 0 or not args.gaussdb.is_file():
        parser.error("target dbNode and gaussdb binary must exist")
    if args.perf_pages <= 0 or args.perf_pages & (args.perf_pages - 1):
        parser.error("--perf-pages must be a positive power of two")

    source = BPF_SOURCE.replace("__TARGET_DB__", str(args.target_db_node))
    bpf = BPF(text=source)
    for function, symbol, is_return in SYMBOLS:
        attach = bpf.attach_uretprobe if is_return else bpf.attach_uprobe
        attach(name=str(args.gaussdb), sym=symbol, fn_name=function, pid=-1)
    bpf.attach_perf_event(
        ev_type=PerfType.SOFTWARE,
        ev_config=PerfSWConfig.CPU_CLOCK,
        fn_name="flush_batch",
        sample_freq=100,
        pid=-1,
        cpu=-1,
    )

    output = sys.stdout.buffer
    compressor = lz4.frame.LZ4FrameCompressor(
        block_size=lz4.frame.BLOCKSIZE_MAX4MB,
        block_linked=True,
        compression_level=0,
        auto_flush=False,
    )
    output.write(compressor.begin())
    output.write(
        compressor.compress(
            HEADER.pack(MAGIC, 1, ctypes.sizeof(Event), args.target_db_node, 0)
        )
    )
    output.flush()
    lost = 0

    def emit(_cpu: int, data: int, size: int) -> None:
        count = ctypes.c_uint32.from_address(data).value
        expected = 8 + count * ctypes.sizeof(Event)
        if count <= 0 or count > 128 or size < expected:
            raise RuntimeError(
                "unexpected BPF batch count/size %d/%d" % (count, size)
            )
        output.write(
            compressor.compress(
                ctypes.string_at(data + 8, count * ctypes.sizeof(Event))
            )
        )

    def lost_events(count: int) -> None:
        nonlocal lost
        lost += count

    bpf["batch_output"].open_perf_buffer(
        emit, page_cnt=args.perf_pages, lost_cb=lost_events
    )
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not stopping:
        bpf.perf_buffer_poll(timeout=100)
    for _ in range(5):
        bpf.perf_buffer_poll(timeout=20)
    for _ in range(3):
        bpf.perf_buffer_poll(timeout=0)

    diagnostics = [
        int(bpf["diagnostics"][ctypes.c_int(index)].value)
        for index in range(4)
    ]
    trailer = Event()
    trailer.kind = STATS
    trailer.start_ns = lost
    trailer.end_ns = diagnostics[0]
    trailer.strategy_id = diagnostics[1]
    trailer.tid = diagnostics[2]
    trailer.reserved = SAMPLE_RATE
    output.write(compressor.compress(bytes(trailer)))
    output.write(compressor.flush())
    output.flush()
    print(
        "binary_buffer_access_probe records_flushed lost=%d "
        "map_update_failures=%d submit_failures=%d"
        % (lost, diagnostics[0], diagnostics[1]),
        file=sys.stderr,
    )
    return 0 if lost == 0 and diagnostics[0] == 0 and diagnostics[1] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
