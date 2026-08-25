"""Stable target-machine fingerprint and PPT hardware-contract checks."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from pathlib import Path
from typing import Dict

from .provenance import sha256


def _meminfo() -> Dict[str, int]:
    result = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        fields = value.split()
        result[key] = int(fields[0]) * 1024
    return result


def _physical_cores() -> int:
    cores = set()
    for cpu in Path("/sys/devices/system/cpu").glob("cpu[0-9]*"):
        topology = cpu / "topology"
        try:
            cores.add((
                (topology / "physical_package_id").read_text().strip(),
                (topology / "core_id").read_text().strip(),
            ))
        except OSError:
            continue
    return len(cores)


def collect_machine(device: Path, gaussdb: Path, source_commit: str) -> Dict[str, object]:
    block = Path("/sys/block") / device.name
    if not device.is_block_device() or not block.is_dir():
        raise ValueError("device must be a whole block device: %s" % device)
    memory = _meminfo()
    document: Dict[str, object] = {
        "schema": "huawei7.machine/v1",
        "kernel_release": platform.release(),
        "architecture": platform.machine(),
        "logical_cpus": os.cpu_count(),
        "physical_cores": _physical_cores(),
        "memory_bytes": memory["MemTotal"],
        "swap_bytes": memory["SwapTotal"],
        "device": str(device),
        "device_model": (block / "device/model").read_text().strip(),
        "device_serial": (block / "device/serial").read_text().strip(),
        "device_bytes": int((block / "size").read_text()) * 512,
        "gaussdb_sha256": sha256(gaussdb),
        "source_commit": source_commit,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    document["machine_fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return document


def validate_ppt_hardware(document: Dict[str, object]) -> None:
    failures = []
    if not str(document["kernel_release"]).startswith("5.4."):
        failures.append("Linux kernel is not 5.4.x")
    if int(document["logical_cpus"]) != 16 or int(document["physical_cores"]) != 8:
        failures.append("CPU topology is not 8 cores / 16 threads")
    memory_gib = int(document["memory_bytes"]) / 1024 ** 3
    if not 29 <= memory_gib <= 31:
        failures.append("RAM is not the PPT 30 GiB class")
    if int(document["swap_bytes"]) != 0:
        failures.append("swap is not disabled")
    if "elastic block" not in str(document["device_model"]).lower():
        failures.append("target device is not reported as Elastic Block Storage")
    if failures:
        raise RuntimeError("PPT hardware contract failed: " + "; ".join(failures))
