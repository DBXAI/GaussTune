"""Bounded Linux process-tree CPU accounting, including runuser children.

Summing the wrapper's /proc/<pid>/task alone misses Sysbench worker CPU.
Thread identities include start ticks; membership changes fail delta checks
instead of subtracting unrelated/reused PID counters.
"""
from pathlib import Path


def _identity(path):
    stat = (path / "stat").read_text()
    # comm may contain spaces/parentheses. Fields after the final ')' start at 3.
    fields = stat[stat.rfind(")") + 1:].split()
    return int(fields[19])


def snapshot_process_trees(roots, proc_root=Path("/proc")):
    if isinstance(roots, int):
        roots = [roots]
    roots = sorted(set(int(pid) for pid in roots))
    if not roots or min(roots) <= 0:
        raise ValueError("positive driver roots required")
    pending, visited, counters, processes = list(roots), set(), {}, []
    while pending:
        pid = pending.pop()
        if pid in visited:
            continue
        if len(visited) >= 64:
            raise ValueError("driver process tree exceeds bounded 64-process policy")
        visited.add(pid)
        base = proc_root / str(pid)
        birth = _identity(base)
        tasks = sorted((base / "task").iterdir(), key=lambda p: int(p.name))
        if not tasks or len(tasks) > 2048:
            raise ValueError("driver thread membership outside bounded policy")
        for task in tasks:
            tid = int(task.name)
            thread_birth = _identity(task)
            key = "%d:%d:%d:%d" % (pid, birth, tid, thread_birth)
            counters[key] = int((task / "schedstat").read_text().split()[0])
            pending.extend(int(child) for child in (task / "children").read_text().split())
        if _identity(base) != birth:
            raise ValueError("driver PID reused during snapshot")
        processes.append({"pid": pid, "start_ticks": birth,
                          "thread_count": len(tasks), "comm": (base / "comm").read_text().strip()})
    return {
        "root_pids": roots, "processes": sorted(processes, key=lambda r: r["pid"]),
        "thread_cpu_ns": counters, "total_cpu_ns": sum(counters.values()),
        "scope": "wrapper_and_all_descendant_processes_all_threads",
        "same_kernel_commit_window": False,
    }


def process_tree_cpu_delta(before, after):
    if before["root_pids"] != after["root_pids"]:
        raise ValueError("driver roots changed")
    first, last = before["thread_cpu_ns"], after["thread_cpu_ns"]
    if first.keys() != last.keys():
        raise ValueError("driver process/thread identity membership changed")
    delta = [last[key] - value for key, value in first.items()]
    if not delta or min(delta) < 0:
        raise ValueError("driver CPU counter decreased or empty")
    return sum(delta) / 1e9
