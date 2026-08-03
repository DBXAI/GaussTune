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

    Growth commits synchronously when the server consumes SIGHUP. Shrink is
    asynchronous and is verified against the kernel's resize-commit log.
    """

    def __init__(
        self,
        data_dir: Path,
        gausshome: Path,
        setting_reader: Callable[[str], str],
        timeout_seconds: float = 30.0,
        settle_seconds: float = 1.0,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        active_buffer_reader: Callable[[], int | None] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.gausshome = gausshome
        self.setting_reader = setting_reader
        self.timeout_seconds = timeout_seconds
        self.settle_seconds = settle_seconds
        self.command_runner = command_runner
        self.uses_external_active_buffer_reader = active_buffer_reader is not None
        self.active_buffer_reader = active_buffer_reader or self._read_active_buffers

    def read_mb(self, name: str) -> float:
        return parse_memory_mb(self.setting_reader(name).strip())

    def status(self) -> dict[str, float]:
        startup_max_mb = self.read_mb("shared_buffers")
        configured_target_mb = self.read_mb("shared_buffers_target")
        return {
            "startup_max_mb": startup_max_mb,
            "target_mb": configured_target_mb or startup_max_mb,
        }

    def _read_resize_commit(self) -> tuple[int, str] | None:
        pattern = re.compile(r"shared buffer resize committed: active buffers ([0-9]+)")
        log_dir = self.data_dir / "pg_log"
        logs = sorted(
            log_dir.glob("postgresql-*.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in logs:
            text = path.read_text(encoding="utf-8", errors="replace")
            matches = list(pattern.finditer(text))
            if matches:
                match = matches[-1]
                return int(match.group(1)), f"{path}:{match.start()}"
        return None

    def _read_active_buffers(self) -> int | None:
        commit = self._read_resize_commit()
        return commit[0] if commit else None

    def _wait_for_shrink(
        self, target_mb: int, started: float, previous_commit_token: str | None
    ) -> int:
        target_buffers = target_mb * 1024 // 8
        deadline = started + self.timeout_seconds
        while True:
            active_buffers = self.active_buffer_reader()
            commit = self._read_resize_commit()
            commit_token = commit[1] if commit else None
            fresh_commit = (
                self.uses_external_active_buffer_reader
                or commit_token != previous_commit_token
            )
            if active_buffers == target_buffers and fresh_commit:
                return active_buffers
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "shared buffer shrink did not commit before the deadline: "
                    f"target_buffers={target_buffers}, observed={active_buffers}"
                )
            time.sleep(0.1)

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
        shrinking = target_mb < previous_target_mb - 1e-9
        previous_commit = self._read_resize_commit() if shrinking else None
        previous_commit_token = previous_commit[1] if previous_commit else None
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
            active_buffers = (
                self._wait_for_shrink(target_mb, started, previous_commit_token)
                if shrinking
                else None
            )
            time.sleep(self.settle_seconds)
        else:
            observed_target_mb = previous_target_mb
            active_buffers = None
        return {
            "sb_runtime_requested_mb": target_mb,
            "sb_runtime_previous_target_mb": round(previous_target_mb, 3),
            "sb_runtime_observed_target_mb": round(observed_target_mb, 3),
            "sb_runtime_startup_max_mb": round(startup_max_mb, 3),
            "sb_runtime_changed": changed,
            "sb_runtime_applied": True,
            "sb_runtime_verification": (
                "target_guc_and_active_buffers_observed_after_shrink"
                if shrinking
                else "target_guc_observed_after_synchronous_grow_settle"
                if changed
                else "target_already_applied"
            ),
            "sb_runtime_active_buffers": active_buffers or "",
            "sb_runtime_apply_seconds": round(time.monotonic() - started, 3),
        }
