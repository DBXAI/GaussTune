"""Optional OFFLINE acceptance observer: sparse native function boundaries only.

Never installed by startup/forecast. No ExecProcNode/ExecAgg/tuple-insert probe.
Symbols are resolved from the actual binary, not fixed addresses or struct
offsets. Inlined hash-aggregate fill functions are explicitly not observable.
"""
from pathlib import Path
import subprocess

SCHEMA = "jmt.sparse-native-boundary-observer/v1"
SYMBOLS = {
    "ExecutorRun": ("standard_ExecutorRun", "QueryDesc"),
    "Hash build": ("MultiExecHash", "HashState"),
    "Sort": ("tuplesort_performsort", "Tuplesortstate"),
}


def resolve_symbols(binary):
    result = subprocess.run(["nm", "--defined-only", str(binary)], capture_output=True,
                            text=True, timeout=15, check=False)
    if result.returncode or len(result.stdout) > 64 * 1024**2:
        raise ValueError("native boundary symbols unavailable/stripped or nm budget")
    names = [fields[-1] for line in result.stdout.splitlines()
             for fields in [line.split()] if len(fields) == 3 and fields[1] in ("t", "T", "w", "W")]
    found = {}
    for kind, fragments in SYMBOLS.items():
        matches = [name for name in names
                   if all(fragment in name for fragment in fragments)
                   and not name.startswith("_GLOBAL")]
        if len(matches) != 1:
            raise ValueError("native boundary symbol ambiguous/unavailable: " + kind)
        found[kind] = matches[0]
    return found


PROGRAM = r"""
#include <uapi/linux/ptrace.h>
#include <linux/sched.h>
struct event {
    u64 mono_ns; u64 scheduled_cpu_ns; u64 pointer;
    u32 tid; u32 kind; u32 exit;
};
BPF_PERCPU_ARRAY(count, u64, 1);
BPF_PERCPU_ARRAY(events, struct event, 512);
static inline int record(struct pt_regs *ctx, u32 kind, u32 exit) {
    u64 identity = bpf_get_current_pid_tgid();
    if ((u32)(identity >> 32) != GAUSSDB_PID) return 0;
    u32 zero = 0;
    u64 *n = count.lookup(&zero);
    if (!n) return 0;
    u32 index = (*n)++;
    if (index >= 512) return 0;
    struct event out = {};
    out.mono_ns = bpf_ktime_get_ns();
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
    bpf_probe_read(&out.scheduled_cpu_ns, sizeof(out.scheduled_cpu_ns),
                   &task->se.sum_exec_runtime);
    out.pointer = exit ? 0 : PT_REGS_PARM1(ctx);
    out.tid = (u32)identity; out.kind = kind; out.exit = exit;
    events.update(&index, &out);
    return 0;
}
int run_in(struct pt_regs *ctx) { return record(ctx, 0, 0); }
int run_out(struct pt_regs *ctx) { return record(ctx, 0, 1); }
int hash_in(struct pt_regs *ctx) { return record(ctx, 1, 0); }
int hash_out(struct pt_regs *ctx) { return record(ctx, 1, 1); }
int sort_in(struct pt_regs *ctx) { return record(ctx, 2, 0); }
int sort_out(struct pt_regs *ctx) { return record(ctx, 2, 1); }
"""


class BoundaryObserver:
    def __init__(self, binary, pid):
        from bcc import BPF
        self.binary, self.pid = str(Path(binary).resolve(strict=True)), int(pid)
        if self.pid <= 0:
            raise ValueError("invalid DB PID")
        self.symbols = resolve_symbols(self.binary)
        self.bpf = BPF(text=PROGRAM.replace("GAUSSDB_PID", str(self.pid)))
        self.attached = []
        try:
            for kind, name in (("ExecutorRun", "run"), ("Hash build", "hash"), ("Sort", "sort")):
                symbol = self.symbols[kind]
                self.bpf.attach_uprobe(name=self.binary, sym=symbol,
                                       fn_name=name + "_in", pid=self.pid)
                self.attached.append((False, symbol))
                self.bpf.attach_uretprobe(name=self.binary, sym=symbol,
                                          fn_name=name + "_out", pid=self.pid)
                self.attached.append((True, symbol))
        except BaseException:
            self.close()
            raise

    def reset(self):
        table = self.bpf["count"]
        table[table.Key(0)] = table.Leaf()

    def snapshot(self, *, allow_partial=False):
        counts = [int(n) for n in self.bpf["count"][self.bpf["count"].Key(0)]]
        if max(counts, default=0) >= 512 and not allow_partial:
            raise ValueError("sparse observer event overflow: incomplete evidence")
        counts = [min(n, 512) for n in counts]
        table = self.bpf["events"]
        events = []
        for i in range(max(counts, default=0)):
            for cpu, e in enumerate(table[table.Key(i)]):
                if i < counts[cpu]:
                    events.append({name: int(getattr(e, name)) for name in (
                        "mono_ns", "scheduled_cpu_ns", "pointer", "tid", "kind", "exit")})
        return sorted(events, key=lambda r: r["mono_ns"])

    def close(self):
        for returned, symbol in reversed(self.attached):
            method = self.bpf.detach_uretprobe if returned else self.bpf.detach_uprobe
            method(name=self.binary, sym=symbol, pid=self.pid)
        self.attached = []


def pair_events(events):
    """Per-thread/kind stacks; reject unpaired/missing events, never interpolate."""
    stacks, pairs = {}, []
    for event in sorted(events, key=lambda e: e["mono_ns"]):
        key = event["tid"], event["kind"]
        if not event["exit"]:
            stacks.setdefault(key, []).append(event)
        else:
            if not stacks.get(key):
                raise ValueError("unpaired native return boundary")
            start = stacks[key].pop()
            wall = event["mono_ns"] - start["mono_ns"]
            cpu = event["scheduled_cpu_ns"] - start["scheduled_cpu_ns"]
            if wall < 0 or cpu < 0:
                raise ValueError("native boundary clock moved backwards")
            pairs.append({
                "tid": key[0], "kind": ("ExecutorRun", "Hash build", "Sort")[key[1]],
                "pointer": start["pointer"], "start_ns": start["mono_ns"],
                "end_ns": event["mono_ns"], "wall_seconds": wall / 1e9,
                "scheduled_CPU_seconds_approx": cpu / 1e9,
                "CPU_not_exact_boundary_runtime_current_timeslice_not_flushed": True})
    if any(stacks.values()):
        raise ValueError("unpaired native entry boundary")
    return sorted(pairs, key=lambda e: e["end_ns"])
