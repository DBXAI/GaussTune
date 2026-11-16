"""Offline-only, same-kernel-clock TP commit and device-completion counters.

All events are filtered in BPF by one preprogrammed [start_ns, end_ns).
No one-second driver report is scaled to a shorter device interval.
Only explicit, successful read-only transaction commits on enrolled backend
threads are eligible; this is NOT generic attempted-transaction throughput.
"""
import os
from pathlib import Path
import time

PROGRAM = r"""
#include <uapi/linux/ptrace.h>
struct timing { u64 start_ns; u64 end_ns; };
BPF_ARRAY(window, struct timing, 1);
BPF_HASH(backends, u32, u8, 2048);
BPF_PERCPU_ARRAY(totals, u64, 6);
static inline int inside_window(void) {
    u32 zero = 0;
    struct timing *w = window.lookup(&zero);
    u64 now = bpf_ktime_get_ns();
    return w && w->start_ns && now >= w->start_ns && now < w->end_ns;
}
int committed(struct pt_regs *ctx) {
    u64 identity = bpf_get_current_pid_tgid();
    if ((u32)(identity >> 32) != GAUSSDB_PID) return 0;
    u32 tid = (u32)identity;
    if (!backends.lookup(&tid) || !inside_window()) return 0;
    u32 key = 0;
    u64 *n = totals.lookup(&key);
    if (n) (*n)++;
    return 0;
}
TRACEPOINT_PROBE(block, block_rq_complete) {
    if (args->dev != TARGET_DEVICE || !inside_window()) return 0;
    u32 count_key, bytes_key;
    if (args->rwbs[0] == 'R') { count_key = 1; bytes_key = 3; }
    else if (args->rwbs[0] == 'W') { count_key = 2; bytes_key = 4; }
    else return 0;
    if (args->error) {
        u32 errors = 5;
        u64 *e = totals.lookup(&errors);
        if (e) (*e)++;
        return 0;
    }
    u64 *n = totals.lookup(&count_key);
    u64 *b = totals.lookup(&bytes_key);
    if (n) (*n)++;
    if (b) (*b) += (u64)args->nr_sector * 512;
    return 0;
}
"""
FIELDS = ("committed_transactions", "device_read_completion_events",
          "device_write_completion_events", "device_read_bytes",
          "device_write_bytes", "device_error_completion_events")


def validate_window(start_ns, end_ns, now_ns):
    if any(isinstance(x, bool) or not isinstance(x, int)
           for x in (start_ns, end_ns, now_ns)):
        raise ValueError("window endpoints must be integer monotonic nanoseconds")
    if not now_ns < start_ns < end_ns:
        raise ValueError("window must be preprogrammed in the future")
    if not 0 < (end_ns - start_ns) / 1e9 <= 120:
        raise ValueError("measurement duration must be in (0,120] seconds")


class TPWindowObserver:
    """One-shot BPF window; create a new observer for each independent arm."""
    def __init__(self, binary, gaussdb_pid, device):
        from bcc import BPF
        self.binary = str(Path(binary).resolve())
        self.pid = int(gaussdb_pid)
        self.device = str(Path(device).resolve())
        if self.pid <= 0 or not Path(self.binary).is_file():
            raise ValueError("invalid gaussdb binary or PID")
        dev = Path(device)
        if not dev.is_block_device():
            raise ValueError("a real block device is required")
        trace = Path("/sys/kernel/tracing/events/block/block_rq_complete/format")
        if not all(f in trace.read_text() for f in ("rwbs", "nr_sector", "error", "dev")):
            raise ValueError("unsupported completion tracepoint")
        raw_device = os.major(dev.stat().st_rdev) << 20 | os.minor(dev.stat().st_rdev)
        text = PROGRAM.replace("GAUSSDB_PID", str(self.pid)).replace(
            "TARGET_DEVICE", str(raw_device))
        self.bpf = BPF(text=text)
        self.symbol = "_ZL17CommitTransactionb"
        try:
            self.bpf.attach_uretprobe(name=self.binary, sym=self.symbol,
                                     fn_name="committed", pid=self.pid)
        except BaseException:
            self.bpf.detach_tracepoint(tp="block:block_rq_complete")
            raise
        self.interval = None
        self.enrolled = set()

    def enroll(self, tids):
        if self.interval is not None:
            raise ValueError("backend set must be frozen before the window")
        if not tids or len(tids) > 2048:
            raise ValueError("expected 1..2048 backend threads")
        table = self.bpf["backends"]
        for tid in tids:
            if isinstance(tid, bool) or not isinstance(tid, int) or tid <= 0:
                raise ValueError("invalid Linux thread ID")
            if not Path("/proc/%d/task/%d" % (self.pid, tid)).is_dir():
                raise ValueError("backend is not a thread of the selected postmaster")
            table[table.Key(tid)] = table.Leaf(1)
            self.enrolled.add(tid)

    def arm(self, start_ns, end_ns):
        if self.interval is not None or not self.enrolled:
            raise ValueError("observer needs a frozen backend set and a single window")
        validate_window(start_ns, end_ns, time.monotonic_ns())
        table = self.bpf["window"]
        value = table.Leaf()
        value.start_ns, value.end_ns = start_ns, end_ns
        table[table.Key(0)] = value
        self.interval = (start_ns, end_ns)

    def result(self):
        if self.interval is None or time.monotonic_ns() < self.interval[1]:
            raise ValueError("measurement window has not ended")
        table = self.bpf["totals"]
        values = {field: int(table.sum(table.Key(i)).value)
                  for i, field in enumerate(FIELDS)}
        start, end = self.interval
        seconds = (end - start) / 1e9
        return {
            "schema": "jmt.synchronized-tp-window/v1",
            "measurement_window": {"clock": "monotonic_ns", "start_ns": start,
                                   "end_ns": end, "interval": "[start,end)"},
            "scored_seconds": seconds, "backend_linux_tids": sorted(self.enrolled),
            "gaussdb_pid": self.pid, "commit_symbol": self.symbol,
            "device": self.device, **values,
            "observed_committed_TPS": values["committed_transactions"] / seconds,
            "transaction_count_rescaled": False,
            "physical_TP_attribution_qualified": False,
            "probe_overhead_qualified": False,
            "automatic_recommendation_qualified": False,
            "qualification": "Same-clock counters only. Device completions are whole-device, "
            "not automatically TP-owned requests; background attribution, partial-completion "
            "semantics and overhead still require independent qualification.",
        }

    def close(self):
        if getattr(self, "bpf", None) is None:
            return
        # Installed BCC cleanup is not idempotent; only detach here.
        self.bpf.detach_uretprobe(name=self.binary, sym=self.symbol, pid=self.pid)
        self.bpf.detach_tracepoint(tp="block:block_rq_complete")
        self.bpf = None
