"""Optional offline BCC block ISSUE counters; not imported by online inference.

Foreground ownership is the actual issuing Linux thread. Background writeback
is separately visible in device totals, NEVER relabelled as query-owned IO.
This observer does not infer physical IO from /proc write_bytes or temp bytes.
"""
import os
from pathlib import Path
import time

PROGRAM = r"""
BPF_ARRAY(watched_tid, u32, 1);
BPF_PERCPU_ARRAY(device_totals, u64, 4);
BPF_PERCPU_ARRAY(foreground_totals, u64, 4);
TRACEPOINT_PROBE(block, block_rq_issue) {
    if (args->dev != TARGET_DEVICE) return 0;
    u32 d;
    if (args->rwbs[0] == 'R') d = 0;
    else if (args->rwbs[0] == 'W') d = 1;
    else return 0;
    u32 bytes_index = d + 2;
    u64 *n = device_totals.lookup(&d);
    u64 *b = device_totals.lookup(&bytes_index);
    if (n) (*n)++;
    if (b) (*b) += (u64)args->nr_sector * 512;
    u32 zero = 0;
    u32 *target = watched_tid.lookup(&zero);
    u64 task = bpf_get_current_pid_tgid();
    if (!target || (u32)task != *target || (u32)(task >> 32) != GAUSSDB_PID) return 0;
    n = foreground_totals.lookup(&d);
    b = foreground_totals.lookup(&bytes_index);
    if (n) (*n)++;
    if (b) (*b) += (u64)args->nr_sector * 512;
    return 0;
}
"""
FIELDS = ("read_requests", "write_requests", "read_bytes", "write_bytes")


class BlockIssueObserver:
    def __init__(self, device, gaussdb_pid):
        from bcc import BPF
        device = Path(device)
        if not device.is_block_device():
            raise ValueError("observer requires a real block device")
        fmt = Path("/sys/kernel/tracing/events/block/block_rq_issue/format").read_text()
        if not all(field in fmt for field in ("rwbs", "nr_sector", "dev")):
            raise ValueError("unsupported block issue tracepoint")
        stat = device.stat()
        raw_device = os.major(stat.st_rdev) << 20 | os.minor(stat.st_rdev)
        text = PROGRAM.replace("TARGET_DEVICE", str(raw_device)).replace("GAUSSDB_PID", str(int(gaussdb_pid)))
        self.bpf = BPF(text=text)
        self.device = str(device)

    def watch(self, tid):
        table = self.bpf["watched_tid"]
        table[table.Key(0)] = table.Leaf(int(tid))

    def snapshot(self):
        result = {"monotonic_ns": time.monotonic_ns()}
        for name in ("device_totals", "foreground_totals"):
            table = self.bpf[name]
            result[name] = {key: int(table.sum(table.Key(i)).value)
                            for i, key in enumerate(FIELDS)}
        return result

    def finish(self, before):
        after = self.snapshot()
        result = {"coverage": "foreground_issued", "device": self.device,
                  "snapshot_before": before, "snapshot_after": after,
                  "query_lifecycle_complete": False,
                  "qualification": "Exact issuer-thread block ISSUE count; unowned/background writeback excluded."}
        for field in FIELDS:
            value = after["foreground_totals"][field] - before["foreground_totals"][field]
            if value < 0:
                raise ValueError("cumulative block counter moved backwards")
            result[field] = value
        return result

    def close(self):
        self.watch(0)
        # This installed BCC registers cleanup at atexit and is not idempotent.
        # Detach the hook now; let its own atexit close program/map FDs once.
        self.bpf.detach_tracepoint(tp="block:block_rq_issue")
