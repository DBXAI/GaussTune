#!/usr/bin/env python3
"""Apply and verify the openGauss runtime shared-buffer target GUC."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable


_MEMORY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?b)?\s*$", re.I)


def parse_memory_mb(value: str) -> float:
    """Parse an openGauss memory setting into MiB."""
    match = _MEMORY_RE.match(value)
    if match is None:
        raise ValueError(f"cannot parse memory setting: {value!r}")
    amount = float(match.group(1))
    unit = (match.group(2) or "blocks").lower()
    factors = {
        "blocks": 8.0 / 1024.0,
        "kb": 1.0 / 1024.0,
        "mb": 1.0,
        "gb": 1024.0,
        "tb": 1024.0 * 1024.0,
    }
    return amount * factors[unit]


class GucSharedBuffersRuntime:
    """Set `shared_buffers_target` and retain explicit verification evidence.

    The current kernel commits growth synchronously when its resize worker
    consumes SIGHUP. Shrink commits are asynchronous and are therefore not
    used by the TP-SLO driver until a status view is available.
    """

    def __init__(
        self,
        data_dir: Path,
        gausshome: Path,
        setting_reader: Callable[[str], str],
        timeout_seconds: float = 30.0,
        settle_seconds: float = 1.0,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.data_dir = data_dir
        self.gausshome = gausshome
        self.setting_reader = setting_reader
        self.timeout_seconds = timeout_seconds
        self.settle_seconds = settle_seconds
        self.command_runner = command_runner

    def read_mb(self, name: str) -> float:
        return parse_memory_mb(self.setting_reader(name).strip())

    def status(self) -> dict[str, float]:
        return {
            "startup_max_mb": self.read_mb("shared_buffers"),
            "target_mb": self.read_mb("shared_buffers_target"),
        }

    def _reload(self, target_mb: int) -> None:
        command = (
            f"export GAUSSHOME={shlex.quote(str(self.gausshome))}; "
            f"export PATH=$GAUSSHOME/bin:$PATH; "
            f"export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; "
            f"gs_guc reload -D {shlex.quote(str(self.data_dir))} "
            f"-c {shlex.quote(f'shared_buffers_target = {target_mb}MB')}"
        )
        completed = self.command_runner(
            ["su", "-", "omm", "-c", command],
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "shared_buffers_target reload failed: "
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
            )

    def apply_target(self, target_mb: int) -> dict[str, object]:
        before = self.status()
        startup_max_mb = before["startup_max_mb"]
        previous_target_mb = before["target_mb"]
        if target_mb <= 0 or target_mb > startup_max_mb + 1e-9:
            raise ValueError(
                f"runtime SB target {target_mb}MB is outside startup maximum "
                f"{startup_max_mb:g}MB"
            )
        if target_mb < previous_target_mb - 1e-9:
            raise RuntimeError(
                "live shrink is disabled in the TP-SLO driver because the current "
                "kernel does not expose an active/retiring status view"
            )
        changed = abs(target_mb - previous_target_mb) > 1e-9
        started = time.monotonic()
        if changed:
            self._reload(target_mb)
            deadline = started + self.timeout_seconds
            while True:
                observed_target_mb = self.read_mb("shared_buffers_target")
                if abs(observed_target_mb - target_mb) <= 1e-9:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"shared_buffers_target remained {observed_target_mb:g}MB; "
                        f"requested {target_mb}MB"
                    )
                time.sleep(0.1)
            time.sleep(self.settle_seconds)
        else:
            observed_target_mb = previous_target_mb
        return {
            "sb_runtime_requested_mb": target_mb,
            "sb_runtime_previous_target_mb": round(previous_target_mb, 3),
            "sb_runtime_observed_target_mb": round(observed_target_mb, 3),
            "sb_runtime_startup_max_mb": round(startup_max_mb, 3),
            "sb_runtime_changed": changed,
            "sb_runtime_applied": True,
            "sb_runtime_verification": (
                "target_guc_observed_after_synchronous_grow_settle"
                if changed else "target_already_applied"
            ),
            "sb_runtime_apply_seconds": round(time.monotonic() - started, 3),
        }
