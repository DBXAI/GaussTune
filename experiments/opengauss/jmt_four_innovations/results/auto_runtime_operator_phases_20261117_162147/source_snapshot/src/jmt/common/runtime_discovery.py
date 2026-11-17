"""Read-only local startup discovery; facts are NOT performance calibration.

No GUC writes, benchmark, cache drop, restart, sampling of user tables, or
privilege escalation is performed. The optional root -> local database owner
switch is only the existing peer-auth gsql adapter. Remote hardware is unknown.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import time

from .db import Gsql
from .provenance import sha256

SCHEMA = "jmt.runtime-discovery/v1"


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def cpu_list(text):
    """Bounded Linux cpulist parser, usable on sparse CPU numbering."""
    result = set()
    if not text:
        return result
    for part in text.strip().split(","):
        values = part.split("-")
        if len(values) > 2 or any(not s.isdigit() for s in values):
            raise ValueError("invalid CPU list")
        lo, hi = int(values[0]), int(values[-1])
        if not 0 <= lo <= hi < 1048576 or hi - lo > 65536:
            raise ValueError("CPU list budget")
        result.update(range(lo, hi + 1))
        if len(result) > 65536:
            raise ValueError("CPU list budget")
    return result


def mount_rows(text):
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)
    result = []
    for line in text.splitlines():
        before, after = line.split(" - ", 1)
        left, right = before.split(), after.split()
        result.append({
            "major_minor": left[2], "root": unescape(left[3]),
            "mountpoint": unescape(left[4]), "fstype": right[0],
            "source": unescape(right[1]), "super_options": right[2].split(","),
        })
    return result


def _inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def cgroup_directories(cgroup_text, mounts):
    """Resolve v1/v2 mounts, including non-root mount roots; no guessed paths."""
    result = []
    for line in cgroup_text.splitlines():
        _, names, group = line.split(":", 2)
        controllers = set(names.split(",")) if names else set()
        for mount in mounts:
            version = 2 if mount["fstype"] == "cgroup2" and not names else 1
            if not (version == 2 or mount["fstype"] == "cgroup"
                    and controllers.intersection(mount["super_options"])):
                continue
            root = mount["root"]
            if not _inside(group, root):
                continue
            relative = group[len(root):].lstrip("/")
            directory = Path(mount["mountpoint"]) / relative
            result.append((version, controllers, directory, Path(mount["mountpoint"])))
    return result


def cgroup_limits(cgroup_text, mounts, reader=read):
    quotas, memory_limits, cpus = [], [], []
    checked, unavailable = 0, []
    for version, names, directory, mount in cgroup_directories(cgroup_text, mounts):
        current = directory
        for _ in range(128):
            checked += 1
            if version == 2:
                cpu = reader(current / "cpu.max")
                mem = reader(current / "memory.max")
                cpuset = reader(current / "cpuset.cpus.effective")
                if cpu and cpu.split()[0] != "max":
                    quota, period = map(int, cpu.split())
                    if quota > 0 and period > 0:
                        quotas.append(quota / period)
                if mem and mem != "max":
                    memory_limits.append(int(mem))
            else:
                cpuset = reader(current / "cpuset.cpus") if "cpuset" in names else None
                if "cpu" in names:
                    quota = reader(current / "cpu.cfs_quota_us")
                    period = reader(current / "cpu.cfs_period_us")
                    if quota and period and int(quota) > 0 and int(period) > 0:
                        quotas.append(int(quota) / int(period))
                    if reader is read and (quota is None or period is None):
                        unavailable.append(str(current) + ": unreadable CPU quota")
                mem = reader(current / "memory.limit_in_bytes") if "memory" in names else None
                if mem and 0 < int(mem) < (1 << 60):
                    memory_limits.append(int(mem))
                if reader is read and "memory" in names and mem is None:
                    unavailable.append(str(current) + ": unreadable memory limit")
            if cpuset:
                cpus.append(cpu_list(cpuset))
            if current == directory and not current.exists() and reader is read:
                unavailable.append(str(current))
            if current == mount:
                break
            current = current.parent
        else:
            raise ValueError("cgroup ancestor budget")
    return {
        "cpu_quota_cores": min(quotas) if quotas else None,
        "memory_limit_bytes": min(memory_limits) if memory_limits else None,
        "cpuset_cpus": sorted(set.intersection(*cpus)) if cpus else None,
        "ancestor_directories_checked": checked, "unavailable_paths": unavailable,
        "unmapped_membership": bool(cgroup_text.strip()) and checked == 0,
    }


def resource_domain(pid):
    memory = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        key, value = line.split(":", 1)
        if key in ("MemTotal", "MemAvailable", "SwapTotal"):
            memory[key] = int(value.split()[0]) * 1024
    affinity = sorted(os.sched_getaffinity(pid))
    mounts = mount_rows(Path("/proc/self/mountinfo").read_text())
    limits = cgroup_limits(read("/proc/%d/cgroup" % pid) or "", mounts)
    usable = set(affinity)
    if limits["cpuset_cpus"] is not None:
        usable.intersection_update(limits["cpuset_cpus"])
    effective_cpu = min(len(usable), limits["cpu_quota_cores"] or math.inf)
    effective_memory = min(
        memory["MemTotal"], limits["memory_limit_bytes"]
        if limits["memory_limit_bytes"] is not None else math.inf)
    physical = set()
    for cpu in usable:
        base = Path("/sys/devices/system/cpu/cpu%d/topology" % cpu)
        pair = (read(base / "physical_package_id"), read(base / "core_id"))
        if None not in pair:
            physical.add(pair)
    return {
        "host_logical_cpus": os.cpu_count(), "database_affinity_cpus": affinity,
        "effective_logical_cpu_capacity": effective_cpu,
        "effective_physical_cores": len(physical) or None,
        "host_memory_bytes": memory["MemTotal"],
        "effective_memory_limit_bytes": int(effective_memory),
        "host_mem_available_bytes_snapshot": memory.get("MemAvailable"),
        "host_swap_bytes": memory.get("SwapTotal"),
        "cgroups": limits,
        "resource_constraints_verified": not limits["unmapped_membership"] and not limits["unavailable_paths"],
        "capacity_is_not_guaranteed_exclusive_CPU_or_free_AP_budget": True,
    }


def local_instances():
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            exe = (entry / "exe").resolve(strict=True)
            if exe.name != "gaussdb":
                continue
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
            data = None
            for i, arg in enumerate(argv[:-1]):
                if arg in ("-D", "--pgdata"):
                    data = argv[i + 1]
                elif arg.startswith("--pgdata="):
                    data = arg.split("=", 1)[1]
            if not data:
                continue
            directory = Path(data)
            if not directory.is_absolute():
                directory = (entry / "cwd").resolve(strict=True) / directory
            directory = directory.resolve()
            pid_file = read(directory / "postmaster.pid")
            lines = pid_file.splitlines() if pid_file else []
            # Only the real postmaster; never a gaussdb worker or stale pid file.
            if len(lines) < 5 or int(lines[0]) != int(entry.name):
                continue
            result.append({
                "pid": int(entry.name), "gaussdb": str(exe),
                "data_directory": str(directory),
                "owner": pwd.getpwuid(entry.stat().st_uid).pw_name,
                "port": int(lines[3]), "socket_directory": lines[4],
                "postmaster_start_epoch": int(lines[2]),
            })
        except (OSError, ValueError, KeyError):
            continue
    return sorted(result, key=lambda r: r["pid"])


def filesystem_profile(path):
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    dev = "%d:%d" % (os.major(stat.st_dev), os.minor(stat.st_dev))
    rows = mount_rows(Path("/proc/self/mountinfo").read_text())
    matches = [r for r in rows if r["major_minor"] == dev and _inside(str(path), r["mountpoint"])]
    mount = max(matches, key=lambda r: len(r["mountpoint"])) if matches else None
    block = Path("/sys/dev/block") / dev
    leaves, visited = [], set()
    def walk(node):
        node = node.resolve()
        if str(node) in visited:
            return
        visited.add(str(node))
        if len(visited) > 64:
            raise ValueError("block device graph budget")
        slaves = list((node / "slaves").glob("*"))
        if slaves:
            for slave in slaves:
                walk(slave)
        elif (node / "partition").exists():
            walk(node.parent)
        else:
            leaves.append({
                "name": node.name, "major_minor": read(node / "dev"),
                "size_bytes": int(read(node / "size") or 0) * 512,
                "rotational": read(node / "queue/rotational"),
            })
    if block.exists():
        walk(block)
    free = os.statvfs(path)
    return {
        "path": str(path), "major_minor": dev, "mount": mount,
        "leaf_devices": sorted(leaves, key=lambda r: r["name"]),
        "free_bytes_snapshot": free.f_bavail * free.f_frsize,
        "physical_device_known": bool(leaves),
        "bandwidth_IOPS_latency_are_not_inferred_from_device_size": True,
    }


RUNTIME_SQL = """SELECT row_to_json(t) FROM (
 SELECT current_database() AS database,version() AS version,
 pg_postmaster_start_time()::text AS postmaster_start_time,
 (SELECT json_agg(s) FROM (
  SELECT name,setting,unit,min_val,max_val,context,source FROM pg_settings
  WHERE name IN ('data_directory','port','block_size','shared_buffers',
   'shared_buffers_target','work_mem','query_mem','query_max_mem',
   'max_process_memory','cstore_buffers','enable_memory_limit',
   'disable_memory_protect','query_dop','enable_vector_engine','temp_tablespaces')
  ORDER BY name)s) AS settings) t"""


def discover(database, *, gsql=None, host=None, port=None, user=None,
             run_as_user=None, pid=None, data_directory=None, timeout_s=30):
    """Return (profile, client). Explicit connection options win over env/defaults.

    Local hardware is bound only through the data directory returned by the DB.
    A remote target never inherits this client's hardware or a local instance.
    """
    started = time.monotonic_ns()
    host = host if host is not None else os.environ.get("PGHOST")
    port = port if port is not None else os.environ.get("PGPORT")
    user = user if user is not None else os.environ.get("PGUSER")
    instances = local_instances() if not host or host.startswith("/") else []
    if pid is not None:
        instances = [r for r in instances if r["pid"] == int(pid)]
    if data_directory is not None:
        instances = [r for r in instances
                     if r["data_directory"] == str(Path(data_directory).resolve())]
    if port is not None:
        instances = [r for r in instances if r["port"] == int(port)]
    if host and host.startswith("/"):
        instances = [r for r in instances if r["socket_directory"] == host]
    if len(instances) > 1:
        raise ValueError("multiple local instances: pass --pid/--data-directory/--port")
    instance = instances[0] if instances else None
    if (pid is not None or data_directory is not None) and instance is None:
        raise ValueError("explicit local instance not found/accessible")
    home = os.environ.get("GAUSSHOME")
    binary = (gsql or (str(Path(instance["gaussdb"]).with_name("gsql")) if instance else None)
              or shutil.which("gsql") or (str(Path(home) / "bin/gsql") if home else None))
    if binary is None:
        raise ValueError("gsql not found: pass --gsql or set GAUSSHOME/PATH")
    resolved_host = host or (instance["socket_directory"] if instance else "/tmp")
    resolved_port = int(port or (instance["port"] if instance else 5432))
    if run_as_user is None and instance and os.geteuid() == 0:
        run_as_user = instance["owner"]
    client = Gsql(binary, resolved_host, resolved_port, user, run_as_user, timeout_s)
    # No forced row-executor/work_mem values contaminate the discovered facts.
    raw = client.execute(database, RUNTIME_SQL, wm_mib=None, row_executor=False)
    if raw["exit_code"]:
        raise RuntimeError("startup database discovery failed: " + raw["stderr"][-1000:])
    from .db import extract_json
    db = extract_json(raw["stdout"])
    settings = {r["name"]: r for r in db["settings"]}
    observed_data = settings.get("data_directory", {}).get("setting")
    if instance and (not observed_data
                     or Path(observed_data).resolve() != Path(instance["data_directory"])):
        raise ValueError("connected database does not match selected local instance")
    local = instance is not None
    resources = resource_domain(instance["pid"]) if local else None
    filesystems = [filesystem_profile(instance["data_directory"])] if local else []
    # Include relocated tablespaces and pg_xlog/pg_wal; readlink/stat only.
    if local:
        data = Path(instance["data_directory"])
        for candidate in sorted((data / "pg_tblspc").glob("*")) + [
                data / "pg_xlog", data / "pg_wal"]:
            if candidate.exists():
                item = filesystem_profile(candidate)
                if item["path"] not in {f["path"] for f in filesystems}:
                    filesystems.append(item)
    native = None
    native_error = None
    try:
        native_raw = client.execute(database,
            "SELECT coalesce(json_agg(t),'[]'::json) FROM "
            "(SELECT * FROM pg_total_memory_detail)t", wm_mib=None, row_executor=False)
        if native_raw["exit_code"]:
            raise RuntimeError("native view unavailable")
        native = extract_json(native_raw["stdout"])
    except (RuntimeError, ValueError) as exc:
        native_error = type(exc).__name__ + ": native memory view unavailable"
    identity = None
    if local:
        resource_static = {k: resources[k] for k in (
            "effective_logical_cpu_capacity", "database_affinity_cpus",
            "effective_memory_limit_bytes", "host_swap_bytes")}
        machine_id = read("/etc/machine-id")
        identity = {
            "host_id_hash": digest(machine_id) if machine_id else None,
            "architecture": platform.machine(), "kernel": platform.release(),
            "resource_domain": resource_static,
            "gaussdb_sha256": sha256(Path(instance["gaussdb"])),
            "storage_topology": [
                {"major_minor": f["major_minor"], "devices": f["leaf_devices"]}
                for f in filesystems],
        }
    result = {
        "schema": SCHEMA, "collected_at": datetime.now(timezone.utc).isoformat(),
        "started_monotonic_ns": started, "finished_monotonic_ns": time.monotonic_ns(),
        "connection": {"gsql": client.binary, "host": resolved_host, "port": resolved_port,
                       "user": user, "run_as_user": run_as_user, "database": database},
        "local_instance": instance, "local_server_hardware_verified": local,
        "database": db, "native_memory": native, "native_memory_error": native_error,
        "resources": resources, "filesystems": filesystems,
        "machine_identity": identity,
        "calibration_machine_key": digest(identity) if identity else None,
        "boot_id": read("/proc/sys/kernel/random/boot_id") if local else None,
        "performance_calibration": {
            "status": "required_not_obtainable_from_settings",
            "missing": ["CPU_seconds_per_operator_work_unit", "physical_IO_conversion_and_rates",
                        "fresh_TP_baseline_for_requested_workload_and_SB"],
            "legacy_host_calibration_automatically_reused": False,
            "implicit_benchmark_or_global_configuration_change": False},
        "automatic_recommendation_qualified": False,
    }
    from .database_memory_domain import derive_work_mem_domain
    result["work_mem_SET_domain"] = derive_work_mem_domain(
        [r for r in db["settings"] if r["name"] in (
            "work_mem", "query_mem", "query_max_mem", "max_process_memory",
            "enable_memory_limit", "disable_memory_protect")])
    return result, client


def calibration_binding(profile, *, workload_sha256, kind, lifetime_seconds):
    if not profile.get("local_server_hardware_verified") or not profile.get("calibration_machine_key"):
        raise ValueError("verified local server profile required for calibration binding")
    if profile.get("resources", {}).get("resource_constraints_verified") is False:
        raise ValueError("unverified cgroup resource domain cannot bind a calibration")
    if (not isinstance(lifetime_seconds, (float, int)) or isinstance(lifetime_seconds, bool)
            or not math.isfinite(lifetime_seconds) or not 0 < lifetime_seconds <= 86400 * 30):
        raise ValueError("bounded positive calibration lifetime required")
    return {
        "schema": "jmt.runtime-calibration-binding/v1", "kind": kind,
        "machine_key": profile["calibration_machine_key"],
        "boot_id": profile["boot_id"],
        "postmaster_start_time": profile["database"]["postmaster_start_time"],
        "workload_sha256": workload_sha256, "created_at": profile["collected_at"],
        "lifetime_seconds": lifetime_seconds,
    }


def validate_binding(binding, profile, *, workload_sha256, kind, now=None):
    expected = calibration_binding(
        profile, workload_sha256=workload_sha256, kind=kind,
        lifetime_seconds=binding.get("lifetime_seconds"))
    for key in ("schema", "kind", "machine_key", "boot_id",
                "postmaster_start_time", "workload_sha256"):
        if binding.get(key) != expected[key]:
            raise ValueError("calibration binding mismatch: " + key)
    created = datetime.fromisoformat(binding["created_at"])
    if created.tzinfo is None:
        raise ValueError("calibration timestamp requires timezone")
    age = ((now or datetime.now(timezone.utc)) - created).total_seconds()
    if not 0 <= age <= binding["lifetime_seconds"]:
        raise ValueError("future/stale calibration; recollect on current server")
    return True
