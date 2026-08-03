#!/usr/bin/env python3
"""Run the Huawei5 stages with TP-SLO-first AP admission control.

The offline replay supplies safe per-query work_mem candidates.  TP TPS is
used only as delayed runtime feedback.  AP changes happen at query/session
boundaries, so a lower grant never pretends to reclaim memory from an
operator that is still running.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import tpc5stage  # noqa: E402
from runtime_memory_controller_replay import load_targets, read_csv  # noqa: E402
from shared_buffers_runtime import GucSharedBuffersRuntime  # noqa: E402
from tp_slo_controller_replay import (  # noqa: E402
    Observation,
    TpSloController,
    TpSloPolicy,
    load_grant_profiles,
)
from tp_slo_ap_resource_controller import (  # noqa: E402
    MIB,
    ApResourceController,
    ApResourceObservation,
    ApResourcePolicy,
)
from tps_stage_eval import db_counters, wait_for_tpcc  # noqa: E402


STAGES: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("stage1_memory_rich", (1,)),
    ("stage2_reach_limit", (3,)),
    ("stage3_protect_tp", (5, 7)),
    ("stage4_backpressure", (9, 13, 18, 21)),
    ("stage5_tp_surge", (1, 3, 5, 7)),
)


@dataclass(frozen=True)
class QueryCost:
    query_id: int
    work_mem_mb: int
    dynamic_peak_mb: float
    spill_io_mb: float
    exact_candidate: bool
    trace_elapsed_seconds: float


@dataclass
class RunningQuery:
    query_id: int
    work_mem_mb: int
    dynamic_peak_mb: float
    spill_io_mb: float
    trace_elapsed_seconds: float
    started_at: float
    application_name: str
    spec: tpc5stage.ProcSpec


class ApBackendCgroup:
    """Apply cgroup-v1 limits to openGauss AP backend LWPs only."""

    def __init__(
        self,
        name: str,
        cpu_quota_cores: float,
        device: str,
        read_bps: int,
        write_bps: int,
    ) -> None:
        self.cpu_path = Path("/sys/fs/cgroup/cpu") / name
        self.blkio_path = Path("/sys/fs/cgroup/blkio") / name
        self.freezer_path = Path("/sys/fs/cgroup/freezer") / name
        self.device = device
        self.cpu_quota_cores = cpu_quota_cores
        self.read_bps = read_bps
        self.write_bps = write_bps
        self.cpu_enabled = cpu_quota_cores > 0
        self.blkio_enabled = read_bps > 0 or write_bps > 0
        self.freezer_path.mkdir(exist_ok=True)
        self.frozen = False
        if self.cpu_enabled:
            self.cpu_path.mkdir(exist_ok=True)
            period = 100_000
            (self.cpu_path / "cpu.cfs_period_us").write_text(
                f"{period}\n", encoding="ascii"
            )
            (self.cpu_path / "cpu.cfs_quota_us").write_text(
                f"{max(1, int(cpu_quota_cores * period))}\n", encoding="ascii"
            )
        if self.blkio_enabled:
            self.blkio_path.mkdir(exist_ok=True)
            if read_bps > 0:
                (self.blkio_path / "blkio.throttle.read_bps_device").write_text(
                    f"{device} {read_bps}\n", encoding="ascii"
                )
            if write_bps > 0:
                (self.blkio_path / "blkio.throttle.write_bps_device").write_text(
                    f"{device} {write_bps}\n", encoding="ascii"
                )

    def update_limits(
        self, cpu_quota_cores: float, read_bps: int, write_bps: int
    ) -> dict[str, object]:
        previous = (self.cpu_quota_cores, self.read_bps, self.write_bps)
        if self.cpu_enabled:
            period = int(
                (self.cpu_path / "cpu.cfs_period_us").read_text(encoding="ascii")
            )
            (self.cpu_path / "cpu.cfs_quota_us").write_text(
                f"{max(1, int(cpu_quota_cores * period))}\n", encoding="ascii"
            )
        if self.blkio_enabled:
            (self.blkio_path / "blkio.throttle.read_bps_device").write_text(
                f"{self.device} {read_bps}\n", encoding="ascii"
            )
            (self.blkio_path / "blkio.throttle.write_bps_device").write_text(
                f"{self.device} {write_bps}\n", encoding="ascii"
            )
        self.cpu_quota_cores = cpu_quota_cores
        self.read_bps = read_bps
        self.write_bps = write_bps
        return {
            "ap_resource_limits_changed": previous
            != (cpu_quota_cores, read_bps, write_bps),
            "ap_cpu_quota_cores_applied": cpu_quota_cores,
            "ap_read_bps_applied": read_bps,
            "ap_write_bps_applied": write_bps,
        }

    @property
    def enabled(self) -> bool:
        return self.cpu_enabled or self.blkio_enabled or self.freezer_path.exists()

    def set_frozen(self, frozen: bool, timeout_seconds: float = 2.0) -> dict[str, object]:
        """Pause or resume attached AP LWPs without cancelling their SQL."""
        requested = "FROZEN" if frozen else "THAWED"
        state_path = self.freezer_path / "freezer.state"
        state_path.write_text(f"{requested}\n", encoding="ascii")
        deadline = time.time() + timeout_seconds
        observed = state_path.read_text(encoding="ascii").strip()
        while observed != requested and time.time() < deadline:
            time.sleep(0.02)
            observed = state_path.read_text(encoding="ascii").strip()
        if observed != requested:
            raise TimeoutError(
                f"AP freezer did not reach {requested}; observed {observed}"
            )
        changed = self.frozen != frozen
        self.frozen = frozen
        return {
            "ap_freezer_changed": changed,
            "ap_frozen_applied": self.frozen,
            "ap_freezer_state": observed,
        }

    def attach_all(self) -> list[int]:
        if not self.enabled:
            return []
        output = tpc5stage.gsql_output(
            """
SELECT DISTINCT w.lwtid
FROM pg_stat_activity a
JOIN pg_thread_wait_status w ON w.sessionid = a.pid
WHERE a.application_name LIKE 'tpch_ap%'
  AND w.lwtid > 0;
"""
        )
        tids = [int(line.strip()) for line in output.splitlines() if line.strip()]
        attached: list[int] = []
        for tid in tids:
            try:
                if self.cpu_enabled:
                    (self.cpu_path / "tasks").write_text(f"{tid}\n", encoding="ascii")
                if self.blkio_enabled:
                    (self.blkio_path / "tasks").write_text(f"{tid}\n", encoding="ascii")
                (self.freezer_path / "tasks").write_text(f"{tid}\n", encoding="ascii")
                attached.append(tid)
            except OSError:
                # The query may finish between the catalog read and cgroup write.
                continue
        return attached


class ApProgressSampler:
    """Measure real AP backend CPU and physical I/O by unique app name."""

    def __init__(self, data_dir: Path) -> None:
        self.server_pid = int((data_dir / "postmaster.pid").read_text().splitlines()[0])
        self.clock_ticks = os.sysconf("SC_CLK_TCK")
        self.previous: dict[tuple[str, int], tuple[int, int, int]] = {}

    def _thread_counters(self, tid: int) -> tuple[int, int, int]:
        task = Path(f"/proc/{self.server_pid}/task/{tid}")
        stat = (task / "stat").read_text(encoding="ascii")
        fields = stat[stat.rfind(")") + 2:].split()
        cpu_ticks = int(fields[11]) + int(fields[12])
        io_values: dict[str, int] = {}
        for line in (task / "io").read_text(encoding="ascii").splitlines():
            key, value = line.split(":", 1)
            io_values[key] = int(value.strip())
        return (
            cpu_ticks,
            io_values.get("read_bytes", 0),
            io_values.get("write_bytes", 0),
        )

    def sample(self, stage: str, elapsed_seconds: float) -> list[dict[str, object]]:
        output = tpc5stage.gsql_output(
            """
SELECT a.application_name, w.lwtid, COALESCE(w.wait_status, '')
FROM pg_stat_activity a
JOIN pg_thread_wait_status w ON w.sessionid = a.pid
WHERE a.application_name LIKE 'tpch_ap%'
  AND w.lwtid > 0
ORDER BY a.application_name, w.lwtid;
"""
        )
        rows: list[dict[str, object]] = []
        current: dict[tuple[str, int], tuple[int, int, int]] = {}
        for line in output.splitlines():
            if not line.strip():
                continue
            application_name, tid_value, wait_status = line.split("|", 2)
            tid = int(tid_value)
            try:
                counters = self._thread_counters(tid)
            except (FileNotFoundError, ProcessLookupError):
                continue
            key = (application_name, tid)
            previous = self.previous.get(key, counters)
            current[key] = counters
            query_token = next(
                (part for part in application_name.split("_") if part.startswith("q")),
                "q0",
            )
            rows.append(
                {
                    "elapsed_seconds": round(elapsed_seconds, 3),
                    "stage": stage,
                    "application_name": application_name,
                    "query_id": int(query_token[1:]) if query_token[1:].isdigit() else 0,
                    "lwtid": tid,
                    "cpu_seconds_delta": round(
                        max(0, counters[0] - previous[0]) / self.clock_ticks, 6
                    ),
                    "read_mb_delta": round(
                        max(0, counters[1] - previous[1]) / 1024.0 / 1024.0, 6
                    ),
                    "write_mb_delta": round(
                        max(0, counters[2] - previous[2]) / 1024.0 / 1024.0, 6
                    ),
                    "wait_status": wait_status,
                }
            )
        self.previous = current
        return rows


def parse_assignments(value: str) -> dict[int, int]:
    result: dict[int, int] = {}
    for item in value.split(";"):
        item = item.strip()
        if not item:
            continue
        query, separator, grant = item.partition("=")
        if not separator or not query.startswith("q"):
            raise ValueError(f"invalid work_mem assignment: {item!r}")
        result[int(query[1:])] = int(float(grant))
    return result


def parse_positive_levels(value: str) -> tuple[float, ...]:
    levels = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    if not levels or any(level <= 0 for level in levels):
        raise ValueError("resource levels must be positive")
    if tuple(sorted(set(levels))) != levels:
        raise ValueError("resource levels must be unique and ascending")
    return levels


def stable_tp_baseline_ready(
    window_means: list[float],
    offered_tps: float,
    ready_ratio: float,
    required_windows: int,
) -> bool:
    if offered_tps <= 0 or required_windows <= 0:
        return False
    if len(window_means) < required_windows:
        return False
    threshold = offered_tps * ready_ratio
    return all(value >= threshold for value in window_means[-required_windows:])


def choose_tp_reference_tps(
    measured_no_ap_tps: float, fixed_offered_tps: float | None
) -> float:
    return (
        fixed_offered_tps
        if fixed_offered_tps is not None
        else measured_no_ap_tps
    )


def select_stages(value: str) -> tuple[tuple[str, tuple[int, ...]], ...]:
    requested = {item.strip() for item in value.split(",") if item.strip()}
    known = {stage for stage, _query_ids in STAGES}
    unknown = requested - known
    if unknown:
        raise ValueError(f"unknown stages: {','.join(sorted(unknown))}")
    selected = tuple(item for item in STAGES if item[0] in requested)
    if not selected:
        raise ValueError("no stages selected")
    return selected


class QueryPredictionTable:
    def __init__(self, path: Path, execution_trace_path: Path | None = None) -> None:
        self.rows: dict[int, list[dict[str, str]]] = {}
        self.trace_elapsed_seconds: dict[int, float] = {}
        for row in read_csv(path):
            self.rows.setdefault(int(row["query_id"]), []).append(row)
        if execution_trace_path is not None:
            for row in read_csv(execution_trace_path):
                self.trace_elapsed_seconds[int(row["query_id"])] = float(
                    row["elapsed_seconds"]
                )

    def lookup(self, query_id: int, work_mem_mb: int) -> QueryCost:
        candidates = self.rows.get(query_id, [])
        if not candidates:
            raise KeyError(f"no replay prediction for Q{query_id}")
        row = min(
            candidates,
            key=lambda item: abs(float(item["work_mem_mb"]) - work_mem_mb),
        )
        sampled = int(float(row["work_mem_mb"]))
        return QueryCost(
            query_id=query_id,
            work_mem_mb=work_mem_mb,
            dynamic_peak_mb=float(row["dynamic_peak_mb"]),
            spill_io_mb=float(row["spill_io_mb"]),
            exact_candidate=sampled == work_mem_mb,
            trace_elapsed_seconds=self.trace_elapsed_seconds.get(query_id, 0.0),
        )


class QueryBoundaryScheduler:
    """Translate controller output into starts at safe session boundaries."""

    def __init__(self, predictions: QueryPredictionTable) -> None:
        self.predictions = predictions
        self.stage = ""
        self.query_ids: tuple[int, ...] = ()
        self.running: dict[int, RunningQuery] = {}
        self._serial = 0
        self.stage_started_at = 0.0
        self.first_started_at: dict[int, float] = {}
        self.service_seconds: dict[int, float] = {}
        self.completion_seconds: dict[int, float] = {}

    def enter_stage(
        self, stage: str, query_ids: tuple[int, ...], now: float | None = None
    ) -> None:
        if self.running:
            raise RuntimeError("stage transition requires Query-boundary drain")
        self.stage = stage
        self.query_ids = query_ids
        self.stage_started_at = time.time() if now is None else now
        self.first_started_at = {}
        self.service_seconds = {query_id: 0.0 for query_id in query_ids}
        self.completion_seconds = {}

    def oldest_initial_wait_seconds(self, now: float) -> float:
        if not self.query_ids:
            return 0.0
        pending = [
            query_id for query_id in self.query_ids
            if query_id not in self.first_started_at
        ]
        return max(0.0, now - self.stage_started_at) if pending else 0.0

    def service_summary(self, now: float) -> dict[str, object]:
        service = dict(self.service_seconds)
        for query in self.running.values():
            service[query.query_id] = service.get(query.query_id, 0.0) + max(
                0.0, now - query.started_at
            )
        waits = {
            query_id: max(
                0.0,
                self.first_started_at.get(query_id, now) - self.stage_started_at,
            )
            for query_id in self.query_ids
        }
        return {
            "stage": self.stage,
            "requested_queries": len(self.query_ids),
            "ever_started_queries": len(self.first_started_at),
            "all_queries_started": len(self.first_started_at) == len(self.query_ids),
            "completed_queries": len(self.completion_seconds),
            "all_queries_completed": len(self.completion_seconds) == len(self.query_ids),
            "max_initial_wait_seconds": round(max(waits.values(), default=0.0), 3),
            "min_service_seconds": round(min(service.values(), default=0.0), 3),
            "total_service_seconds": round(sum(service.values()), 3),
            "query_initial_wait_seconds": ";".join(
                f"q{query_id}={waits[query_id]:.3f}" for query_id in self.query_ids
            ),
            "query_service_seconds": ";".join(
                f"q{query_id}={service[query_id]:.3f}" for query_id in self.query_ids
            ),
            "query_completion_seconds": ";".join(
                f"q{query_id}={self.completion_seconds.get(query_id, 0.0):.3f}"
                for query_id in self.query_ids
            ),
        }

    def actual_dynamic_mb(self) -> float:
        return sum(query.dynamic_peak_mb for query in self.running.values())

    def completed(self, now: float) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for serial, query in list(self.running.items()):
            code = query.spec.proc.poll()
            if code is None:
                continue
            if code != 0:
                raise RuntimeError(
                    f"Q{query.query_id} failed exit={code}; log={query.spec.log}"
                )
            rows.append(
                {
                    "event": "complete",
                    "stage": self.stage,
                    "query_id": query.query_id,
                    "work_mem_mb": query.work_mem_mb,
                    "application_name": query.application_name,
                    "elapsed_seconds": round(now - query.started_at, 3),
                }
            )
            self.service_seconds[query.query_id] = (
                self.service_seconds.get(query.query_id, 0.0)
                + max(0.0, now - query.started_at)
            )
            self.completion_seconds[query.query_id] = max(
                0.0, now - query.started_at
            )
            del self.running[serial]
        return rows

    def launch_plan(self, control: dict[str, object]) -> list[QueryCost]:
        if bool(control["block_new_ap"]):
            return []
        assignments = parse_assignments(str(control["work_mem_assignments"]))
        costs = [
            self.predictions.lookup(query_id, assignments[query_id])
            for query_id in self.query_ids
        ]
        # Keep the least interfering AP queries when only part of a stage can
        # be admitted.  This ordering comes from replayed spill and peak memory.
        costs.sort(
            key=lambda cost: (
                cost.trace_elapsed_seconds,
                cost.spill_io_mb,
                cost.dynamic_peak_mb,
                cost.query_id,
            )
        )
        running_ids = {query.query_id for query in self.running.values()}
        # A stage is a finite set of one-shot SQL executions.  A query that
        # completed must not be silently submitted again to fill an idle slot.
        available = [
            cost
            for cost in costs
            if cost.query_id not in running_ids
            and cost.query_id not in self.first_started_at
        ]
        available.sort(
            key=lambda cost: (
                cost.query_id in self.first_started_at,
                cost.trace_elapsed_seconds,
                cost.spill_io_mb,
                cost.dynamic_peak_mb,
                cost.query_id,
            )
        )
        slots = max(0, int(control["admitted_ap_clients"]) - len(self.running))
        return available[:slots]

    def register(
        self,
        cost: QueryCost,
        spec: tpc5stage.ProcSpec,
        now: float,
        application_name: str,
    ) -> None:
        self._serial += 1
        self.first_started_at.setdefault(cost.query_id, now)
        self.running[self._serial] = RunningQuery(
            query_id=cost.query_id,
            work_mem_mb=cost.work_mem_mb,
            dynamic_peak_mb=cost.dynamic_peak_mb,
            spill_io_mb=cost.spill_io_mb,
            trace_elapsed_seconds=cost.trace_elapsed_seconds,
            started_at=now,
            application_name=application_name,
            spec=spec,
        )

def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def dry_run(
    scheduler: QueryBoundaryScheduler,
    targets: dict[str, object],
    stages: tuple[tuple[str, tuple[int, ...]], ...],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for stage, query_ids in stages:
        scheduler.enter_stage(stage, query_ids)
        assignments = parse_assignments(targets[stage].work_mem_assignments)
        costs = [scheduler.predictions.lookup(qid, assignments[qid]) for qid in query_ids]
        for cost in sorted(costs, key=lambda item: item.query_id):
            rows.append(
                {
                    "stage": stage,
                    "query_id": cost.query_id,
                    "work_mem_mb": cost.work_mem_mb,
                    "predicted_dynamic_peak_mb": round(cost.dynamic_peak_mb, 3),
                    "predicted_spill_io_mb": round(cost.spill_io_mb, 3),
                    "trace_elapsed_seconds": round(cost.trace_elapsed_seconds, 3),
                    "exact_replay_candidate": cost.exact_candidate,
                }
            )
    return rows


def runtime_args(args: argparse.Namespace, total_seconds: int) -> SimpleNamespace:
    return SimpleNamespace(
        seed=20260614,
        tpcc_warehouses=args.tpcc_warehouses,
        tpch_scale=args.tpch_scale,
        ap_work_mem="1024MB",
        ap_temp_file_limit="",
        tp_low_terminals=2,
        tp_low_rate=40,
        tp_high_terminals=args.tp_terminals,
        tp_high_rate=str(args.tp_rate),
        stable_tp_high_rate="180",
        stable_workload=False,
        ap_rate="unlimited",
        ap_serial=True,
        ap_fixed_query_clients=True,
        ap_query_cycle="1,3,5,7,9,13,18,21",
        ap_s1=1,
        ap_s2=1,
        ap_s3=2,
        ap_s4=4,
        ap_s5=4,
        stage_seconds=total_seconds,
        sample_interval=1,
        stage_boundary_mode="time",
        tpch_start_timeout_seconds=60.0,
        tpch_query_timeout_seconds=0.0,
        tp_run_seconds=total_seconds,
        total_seconds=total_seconds,
    )


def make_plot(path: Path, samples: list[dict[str, object]]) -> None:
    import matplotlib.pyplot as plt

    x = [float(row["elapsed_seconds"]) for row in samples]
    retention = [100 * float(row["tp_retention_ratio"]) for row in samples]
    active = [int(row["active_ap_queries"]) for row in samples]
    fig, axes = plt.subplots(2, 1, figsize=(14, 7), sharex=True, constrained_layout=True)
    axes[0].plot(x, retention, color="#147b83", linewidth=1.8)
    axes[0].axhline(95, color="#c44742", linestyle="--", label="95% TP retention SLO")
    axes[0].set_ylabel("TP retention (%)")
    axes[0].legend()
    axes[0].grid(alpha=0.2)
    axes[1].step(x, active, where="post", color="#df8428")
    axes[1].set_ylabel("Running AP queries")
    axes[1].set_xlabel("Elapsed time (s)")
    axes[1].grid(alpha=0.2)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def make_tp_only_plot(path: Path, windows: list[dict[str, object]]) -> None:
    import matplotlib.pyplot as plt

    x = [int(row["window"]) for row in windows]
    retention = [100 * float(row["tp_retention_ratio"]) for row in windows]
    fig, axis = plt.subplots(figsize=(13, 5.5), constrained_layout=True)
    axis.axhspan(95, 105, color="#dcefe4", alpha=0.8)
    axis.plot(x, retention, color="#2878b5", linewidth=1.8)
    axis.axhline(95, color="#a92d2d", linestyle="--", label="95% SLO floor")
    axis.axhline(100, color="#4d5960", linestyle=":", label="offered TPS")
    axis.set_xlabel("15-second no-AP control window")
    axis.set_ylabel("TP retention (%)")
    axis.set_title("Long-run TP-only stability at fixed offered load")
    axis.grid(alpha=0.2)
    axis.legend()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def make_acceptance_plot(
    path: Path,
    controls: list[dict[str, object]],
    stages: tuple[tuple[str, tuple[int, ...]], ...],
    offered_rate: str,
) -> None:
    import matplotlib.pyplot as plt

    stage_names = [stage for stage, _query_ids in stages]
    stage_labels = {stage: f"S{index + 1}" for index, stage in enumerate(stage_names)}
    x = list(range(len(controls)))
    retention = [100 * float(row["tp_retention_ratio"]) for row in controls]
    admitted = [int(row["actual_running_ap_queries"]) for row in controls]

    fig, axes = plt.subplots(2, 1, figsize=(14, 8), constrained_layout=True)
    axes[0].axhspan(95, 105, color="#dcefe4", alpha=0.8, label="acceptance band (95%-105%)")
    axes[0].plot(x, retention, color="#147b83", marker="o", linewidth=1.8, label="15-second TP retention")
    axes[0].axhline(100, color="#34424a", linewidth=1, linestyle=":")
    boundaries: list[int] = []
    previous_stage = None
    for index, row in enumerate(controls):
        if row["stage"] != previous_stage:
            if index:
                boundaries.append(index - 1)
            axes[0].text(
                index,
                105.5,
                stage_labels[str(row["stage"])],
                ha="left",
                va="bottom",
                fontweight="bold",
            )
            previous_stage = row["stage"]
    for boundary in boundaries:
        axes[0].axvline(boundary + 0.5, color="#aeb7bd", linewidth=1)
    axes[0].set_ylim(94, max(107, max(retention) + 1))
    axes[0].set_ylabel("TP retention (%)")
    axes[0].set_title(
        f"Five-stage TP-SLO control windows (offered load: {offered_rate} TPS)"
    )
    axes[0].grid(axis="y", alpha=0.2)
    axes[0].legend(loc="lower left")

    ap_axis = axes[0].twinx()
    ap_axis.step(x, admitted, where="mid", color="#df8428", alpha=0.65, label="running AP queries")
    ap_axis.set_ylabel("Running AP queries")
    ap_axis.set_ylim(0, max(4.5, max(admitted) + 1))

    final_values = []
    for stage in stage_names:
        values = [
            float(row["tp_retention_ratio"])
            for row in controls
            if row["stage"] == stage
            and row.get("phase", "admission_window") == "admission_window"
        ]
        final_values.append(100 * statistics.mean(values[-min(3, len(values)):]))
    bars = axes[1].bar(
        [stage_labels[stage] for stage in stage_names],
        final_values,
        color="#3474ad",
        width=0.58,
    )
    axes[1].axhspan(95, 105, color="#dcefe4", alpha=0.8)
    axes[1].axhline(100, color="#34424a", linewidth=1, linestyle=":")
    for bar, value in zip(bars, final_values):
        axes[1].text(
            bar.get_x() + bar.get_width() / 2,
            value + 0.35,
            f"{value:.2f}%",
            ha="center",
            va="bottom",
            fontweight="bold",
        )
    gap = max(final_values) - min(final_values)
    axes[1].set_ylim(94, max(106, max(final_values) + 1.5))
    axes[1].set_ylabel("Final 45-second retention (%)")
    axes[1].set_title(f"Cross-stage max-min gap: {gap:.2f} percentage points (<5 pp)")
    axes[1].grid(axis="y", alpha=0.2)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def execute(
    args: argparse.Namespace,
    controller: TpSloController,
    scheduler: QueryBoundaryScheduler,
    stages: tuple[tuple[str, tuple[int, ...]], ...],
    sb_runtime: GucSharedBuffersRuntime | None = None,
    ap_resource_controller: ApResourceController | None = None,
) -> dict[str, object]:
    drain_budget_seconds = (
        len(stages) * args.drain_timeout_seconds
        if args.drain_timeout_seconds > 0
        else args.tp_runtime_guard_seconds
    )
    total_seconds = (
        args.tp_warmup_seconds
        + args.baseline_seconds
        + len(stages) * args.stage_seconds
        + drain_budget_seconds
        + 300
    )
    rt = runtime_args(args, total_seconds)
    paths = tpc5stage.render_configs(rt)
    samples: list[dict[str, object]] = []
    controls: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    progress_rows: list[dict[str, object]] = []
    resource_rows: list[dict[str, object]] = []
    resource_recommendations: list[dict[str, object]] = []
    ap_stage_results: list[dict[str, object]] = []
    live: list[tpc5stage.ProcSpec] = []
    started = time.time()
    ap_cgroup = ApBackendCgroup(
        args.ap_cgroup_name,
        args.ap_cpu_quota_cores,
        args.ap_io_device,
        args.ap_read_bps,
        args.ap_write_bps,
    )
    ap_progress = ApProgressSampler(args.sb_data_dir)
    resource_progress_cursor = 0

    def apply_ap_resource_control(
        *,
        stage: str,
        epoch: str,
        phase: str,
        retention: float,
        running_queries: int,
        window_seconds: float,
        external_memory_control_changed: bool = False,
    ) -> dict[str, object]:
        nonlocal resource_progress_cursor
        if ap_resource_controller is None:
            return {}
        new_progress = progress_rows[resource_progress_cursor:]
        resource_progress_cursor = len(progress_rows)
        observed_cpu_quota_cores = ap_resource_controller.cpu_quota_cores
        observed_io_mib_per_second = ap_resource_controller.io_quota_mib
        observed_ap_frozen = ap_resource_controller.ap_frozen
        decision = ap_resource_controller.step(
            ApResourceObservation(
                stage=stage,
                epoch=epoch,
                tp_retention_ratio=retention,
                running_ap_queries=running_queries,
                window_seconds=window_seconds,
                ap_cpu_seconds=sum(
                    float(row["cpu_seconds_delta"]) for row in new_progress
                ),
                ap_read_mb=sum(
                    float(row["read_mb_delta"]) for row in new_progress
                ),
                ap_write_mb=sum(
                    float(row["write_mb_delta"]) for row in new_progress
                ),
                io_wait_samples=sum(
                    "io" in str(row["wait_status"]).lower()
                    for row in new_progress
                ),
                total_wait_samples=len(new_progress),
                external_memory_control_changed=external_memory_control_changed,
            )
        )
        applied = ap_cgroup.update_limits(
            decision.cpu_quota_cores,
            decision.read_bps,
            decision.write_bps,
        )
        applied.update(ap_cgroup.set_frozen(decision.ap_frozen))
        resource_row = {
            **asdict(decision),
            "observed_cpu_quota_cores": observed_cpu_quota_cores,
            "observed_io_mib_per_second": observed_io_mib_per_second,
            "observed_ap_frozen": observed_ap_frozen,
            **applied,
            "phase": phase,
            "wall_elapsed_seconds": round(time.time() - started, 3),
        }
        resource_rows.append(resource_row)
        return {
            "ap_resource_action": decision.action,
            "ap_resource_reason": decision.reason,
            "ap_resource_cpu_quota_cores": decision.cpu_quota_cores,
            "ap_resource_read_bps": decision.read_bps,
            "ap_resource_write_bps": decision.write_bps,
            "ap_resource_cpu_utilization": decision.cpu_utilization,
            "ap_resource_io_utilization": decision.io_utilization,
            "ap_resource_io_wait_ratio": decision.io_wait_ratio,
            "ap_resource_limits_changed": applied[
                "ap_resource_limits_changed"
            ],
        }

    tpc5stage.terminate_residual_workload_backends()
    try:
        tp = tpc5stage.start(
            "tpcc_tp_slo",
            tpc5stage.benchbase_cmd(
                "tpcc", paths["tpcc_high"], create=False, load=False, execute=True,
                output_dir=args.out_dir / "benchbase",
            ),
            args.out_dir / "tpcc_tp_slo.log",
        )
        live.append(tp)
        wait_for_tpcc(args.tp_terminals)
        time.sleep(args.tp_warmup_seconds)

        try:
            fixed_offered_tps = float(args.tp_rate)
        except ValueError:
            fixed_offered_tps = None
        previous_time = time.time()
        previous_tx = db_counters()[0]
        baseline: list[float] = []
        baseline_window_means: list[float] = []
        baseline_ready = fixed_offered_tps is None
        baseline_limit = (
            max(args.baseline_seconds, args.baseline_max_seconds)
            if fixed_offered_tps is not None
            else args.baseline_seconds
        )
        baseline_started_at = time.time()
        for _ in range(baseline_limit):
            time.sleep(1)
            now = time.time()
            tx = db_counters()[0]
            baseline.append((tx - previous_tx) / max(1e-9, now - previous_time))
            previous_time, previous_tx = now, tx
            if len(baseline) % args.control_window_seconds:
                continue
            baseline_window_means.append(
                statistics.mean(baseline[-args.control_window_seconds :])
            )
            if fixed_offered_tps is not None:
                baseline_ready = stable_tp_baseline_ready(
                    baseline_window_means,
                    fixed_offered_tps,
                    args.baseline_ready_ratio,
                    args.baseline_stable_windows,
                )
                if baseline_ready and len(baseline) >= args.baseline_seconds:
                    break
        baseline_elapsed_seconds = time.time() - baseline_started_at
        if fixed_offered_tps is not None and not baseline_ready:
            raise RuntimeError(
                "TP did not reach a stable no-AP baseline: last windows="
                f"{baseline_window_means[-args.baseline_stable_windows:]} "
                f"required>={fixed_offered_tps * args.baseline_ready_ratio:.3f}"
            )
        selected_baseline_windows = (
            baseline_window_means[-args.baseline_stable_windows :]
            if fixed_offered_tps is not None
            else baseline_window_means
        )
        measured_no_ap_baseline_tps = statistics.median(
            selected_baseline_windows
        ) if selected_baseline_windows else statistics.mean(baseline)
        if measured_no_ap_baseline_tps <= 0:
            raise RuntimeError("no-AP TP baseline is zero")
        # A fixed-rate workload cannot be required to exceed its own offered
        # rate. The measured baseline is a readiness gate, not a higher SLO.
        reference_tps = choose_tp_reference_tps(
            measured_no_ap_baseline_tps, fixed_offered_tps
        )

        if args.tp_only_measure_seconds > 0:
            tp_only_windows: list[dict[str, object]] = []
            window: list[float] = []
            measure_started_at = time.time()
            deadline = measure_started_at + args.tp_only_measure_seconds
            while time.time() < deadline:
                time.sleep(1)
                now = time.time()
                tx = db_counters()[0]
                tps = (tx - previous_tx) / max(1e-9, now - previous_time)
                previous_time, previous_tx = now, tx
                window.append(tps)
                samples.append(
                    {
                        "elapsed_seconds": round(now - started, 3),
                        "stage": "tp_only",
                        "phase": "long_run_no_ap",
                        "tp_tps": round(tps, 6),
                        "tp_reference_tps": round(reference_tps, 6),
                        "tp_retention_ratio": round(tps / reference_tps, 6),
                        "active_ap_queries": 0,
                        "replay_dynamic_mb": 0.0,
                    }
                )
                if len(window) < args.control_window_seconds:
                    continue
                observed = statistics.mean(window)
                window.clear()
                tp_only_windows.append(
                    {
                        "window": len(tp_only_windows) + 1,
                        "elapsed_seconds": round(now - measure_started_at, 3),
                        "tp_tps": round(observed, 6),
                        "tp_reference_tps": round(reference_tps, 6),
                        "tp_retention_ratio": round(observed / reference_tps, 6),
                        "tp_slo_met": observed / reference_tps >= args.tp_floor_ratio,
                    }
                )
                write_rows(args.out_dir / "tp_samples.csv", samples)
                write_rows(args.out_dir / "tp_only_windows.csv", tp_only_windows)

            retention_values = [
                float(row["tp_retention_ratio"]) for row in tp_only_windows
            ]
            if not retention_values:
                raise RuntimeError("TP-only measurement produced no complete window")
            make_tp_only_plot(args.out_dir / "tp_only_long_run.png", tp_only_windows)
            final_retention = statistics.mean(retention_values[-3:])
            return {
                "mode": "real_tp_only_long_baseline",
                "tp_reference_tps": reference_tps,
                "tp_reference_mode": (
                    "fixed_offered_rate"
                    if fixed_offered_tps is not None
                    else "measured_no_ap_capacity"
                ),
                "measured_no_ap_baseline_tps": measured_no_ap_baseline_tps,
                "baseline_elapsed_seconds": round(baseline_elapsed_seconds, 3),
                "baseline_stable_window_tps": [
                    round(value, 3) for value in selected_baseline_windows
                ],
                "fixed_tp_terminals": args.tp_terminals,
                "tp_offered_rate": args.tp_rate,
                "initial_sb_mb": args.initial_sb_mb,
                "tp_only_measure_seconds": args.tp_only_measure_seconds,
                "control_windows": len(retention_values),
                "mean_retention": statistics.mean(retention_values),
                "median_retention": statistics.median(retention_values),
                "minimum_retention": min(retention_values),
                "violating_windows": sum(
                    value < args.tp_floor_ratio for value in retention_values
                ),
                "final_three_window_retention": final_retention,
                "final_three_window_slo_met": final_retention >= args.tp_floor_ratio,
                "ap_queries_started": 0,
            }

        for stage, query_ids in stages:
            stage_started_at = time.time()
            if ap_resource_controller is not None:
                start_decision = ap_resource_controller.enter_stage(stage)
                ap_cgroup.set_frozen(False)
                start_applied = ap_cgroup.update_limits(
                    start_decision.cpu_quota_cores,
                    start_decision.read_bps,
                    start_decision.write_bps,
                )
                resource_rows.append(
                    {
                        **asdict(start_decision),
                        **start_applied,
                        "phase": "stage_start",
                        "wall_elapsed_seconds": round(
                            stage_started_at - started, 3
                        ),
                    }
                )
                resource_progress_cursor = len(progress_rows)
            scheduler.enter_stage(stage, query_ids, stage_started_at)
            stage_deadline = stage_started_at + args.stage_seconds
            window: list[float] = []
            stage_reference_tps = reference_tps
            tick = 0
            while time.time() < stage_deadline:
                time.sleep(1)
                now = time.time()
                events.extend(scheduler.completed(now))
                ap_cgroup.attach_all()
                progress_rows.extend(ap_progress.sample(stage, now - started))
                tx = db_counters()[0]
                tps = (tx - previous_tx) / max(1e-9, now - previous_time)
                previous_time, previous_tx = now, tx
                window.append(tps)
                sample = {
                    "elapsed_seconds": round(now - started, 3),
                    "stage": stage,
                    "phase": "admission_window",
                    "tp_tps": round(tps, 6),
                    "tp_reference_tps": round(reference_tps, 6),
                    "tp_retention_ratio": round(tps / reference_tps, 6),
                    "active_ap_queries": len(scheduler.running),
                    "replay_dynamic_mb": round(scheduler.actual_dynamic_mb(), 3),
                }
                samples.append(sample)

                if len(window) < args.control_window_seconds:
                    continue
                tick += 1
                observed_tps = statistics.mean(window)
                window.clear()
                observation = Observation(
                    epoch=f"{stage}_tick{tick}",
                    stage=stage,
                    tp_tps=observed_tps,
                    tp_reference_tps=stage_reference_tps,
                    requested_ap_clients=len(query_ids),
                    tp_high=True,
                    observed_dynamic_mb=scheduler.actual_dynamic_mb(),
                    running_ap_clients=len(scheduler.running),
                    oldest_ap_wait_seconds=scheduler.oldest_initial_wait_seconds(now),
                )
                control = controller.step(observation)
                if sb_runtime is not None:
                    control.update(sb_runtime.apply_target(int(control["sb_mb"])))
                control.update(
                    apply_ap_resource_control(
                        stage=stage,
                        epoch=str(control["epoch"]),
                        phase="admission_window",
                        retention=float(control["tp_retention_ratio"]),
                        running_queries=len(scheduler.running),
                        window_seconds=args.control_window_seconds,
                        external_memory_control_changed=bool(
                            control.get("sb_runtime_changed", False)
                        ),
                    )
                )
                control["tp_reference_source"] = "frozen_multiwindow_no_ap_baseline"
                control["phase"] = "admission_window"
                control["wall_elapsed_seconds"] = round(now - started, 3)
                control["actual_running_ap_queries"] = len(scheduler.running)
                controls.append(control)
                for cost in scheduler.launch_plan(control):
                    application_name = (
                        f"tpch_ap_{stage}_q{cost.query_id}_{len(events) + len(live):04d}"
                    )
                    launch_args = SimpleNamespace(
                        tpch_scale=args.tpch_scale,
                        ap_work_mem=f"{cost.work_mem_mb}MB",
                        ap_application_name=application_name,
                    )
                    name = f"{stage}_q{cost.query_id}_{len(events) + len(live):04d}"
                    spec = tpc5stage.start(
                        name,
                        tpc5stage.tpch_single_query_cmd(cost.query_id, launch_args),
                        args.out_dir / "ap_logs" / f"{name}.log",
                    )
                    live.append(spec)
                    scheduler.register(cost, spec, now, application_name)
                    if ap_cgroup.enabled:
                        attach_deadline = time.time() + 2.0
                        while time.time() < attach_deadline:
                            if ap_cgroup.attach_all():
                                break
                            time.sleep(0.1)
                    events.append(
                        {
                            "event": "start",
                            "stage": stage,
                            "query_id": cost.query_id,
                            "work_mem_mb": cost.work_mem_mb,
                            "application_name": application_name,
                            "predicted_dynamic_peak_mb": round(cost.dynamic_peak_mb, 3),
                            "predicted_spill_io_mb": round(cost.spill_io_mb, 3),
                            "trace_elapsed_seconds": round(
                                cost.trace_elapsed_seconds, 3
                            ),
                            "elapsed_seconds": round(now - started, 3),
                        }
                    )
                # A long AP query can outlive the stage by many minutes.  Keep
                # evidence durable at every control decision instead of only
                # writing after all five stages finish.
                write_rows(args.out_dir / "tp_samples.csv", samples)
                write_rows(args.out_dir / "controller_actions.csv", controls)
                write_rows(args.out_dir / "ap_query_events.csv", events)
                write_rows(args.out_dir / "ap_progress.csv", progress_rows)
                write_rows(args.out_dir / "ap_resource_actions.csv", resource_rows)

            admission_closed_at = time.time()
            events.append(
                {
                    "event": "stage_admission_closed",
                    "stage": stage,
                    "elapsed_seconds": round(admission_closed_at - started, 3),
                    "running_queries": len(scheduler.running),
                    "unstarted_queries": len(
                        set(query_ids) - set(scheduler.first_started_at)
                    ),
                }
            )
            write_rows(args.out_dir / "ap_query_events.csv", events)

            # The fixed duration closes admission and TP acceptance only. Every
            # submitted SQL keeps its backend until it returns normally.
            drain_started_at = time.time()
            drain_deadline = (
                drain_started_at + args.drain_timeout_seconds
                if args.drain_timeout_seconds > 0
                else None
            )
            drain_window: list[float] = []
            drain_tick = 0
            while scheduler.running:
                time.sleep(1)
                now = time.time()
                if tp.proc.poll() is not None:
                    raise RuntimeError("TP workload ended during AP natural drain")
                completed_events = scheduler.completed(now)
                events.extend(completed_events)
                ap_cgroup.attach_all()
                progress_rows.extend(ap_progress.sample(stage, now - started))
                tx = db_counters()[0]
                tps = (tx - previous_tx) / max(1e-9, now - previous_time)
                previous_time, previous_tx = now, tx
                drain_window.append(tps)
                samples.append(
                    {
                        "elapsed_seconds": round(now - started, 3),
                        "stage": stage,
                        "phase": "natural_drain",
                        "tp_tps": round(tps, 6),
                        "tp_reference_tps": round(reference_tps, 6),
                        "tp_retention_ratio": round(tps / reference_tps, 6),
                        "active_ap_queries": len(scheduler.running),
                        "replay_dynamic_mb": round(
                            scheduler.actual_dynamic_mb(), 3
                        ),
                    }
                )
                control_due = len(drain_window) >= args.control_window_seconds
                if control_due:
                    drain_tick += 1
                    observed_tps = statistics.mean(drain_window)
                    drain_window.clear()
                    observation = Observation(
                        epoch=f"{stage}_drain_tick{drain_tick}",
                        stage=stage,
                        tp_tps=observed_tps,
                        tp_reference_tps=stage_reference_tps,
                        requested_ap_clients=len(query_ids),
                        tp_high=True,
                        observed_dynamic_mb=scheduler.actual_dynamic_mb(),
                        running_ap_clients=len(scheduler.running),
                        oldest_ap_wait_seconds=0.0,
                    )
                    control = controller.step(observation)
                    if sb_runtime is not None:
                        control.update(
                            sb_runtime.apply_target(int(control["sb_mb"]))
                        )
                    control.update(
                        apply_ap_resource_control(
                            stage=stage,
                            epoch=str(control["epoch"]),
                            phase="natural_drain",
                            retention=float(control["tp_retention_ratio"]),
                            running_queries=len(scheduler.running),
                            window_seconds=args.control_window_seconds,
                            external_memory_control_changed=bool(
                                control.get("sb_runtime_changed", False)
                            ),
                        )
                    )
                    control["tp_reference_source"] = (
                        "frozen_multiwindow_no_ap_baseline"
                    )
                    control["phase"] = "natural_drain"
                    control["wall_elapsed_seconds"] = round(now - started, 3)
                    control["actual_running_ap_queries"] = len(scheduler.running)
                    controls.append(control)
                if control_due or completed_events:
                    write_rows(args.out_dir / "tp_samples.csv", samples)
                    write_rows(args.out_dir / "controller_actions.csv", controls)
                    write_rows(args.out_dir / "ap_query_events.csv", events)
                    write_rows(args.out_dir / "ap_progress.csv", progress_rows)
                    write_rows(
                        args.out_dir / "ap_resource_actions.csv", resource_rows
                    )
                if drain_deadline is not None and now >= drain_deadline:
                    raise TimeoutError(
                        f"{stage} SQL did not complete naturally within the "
                        f"explicit diagnostic watchdog of "
                        f"{args.drain_timeout_seconds}s"
                    )

            drain_finished_at = time.time()
            events.append(
                {
                    "event": "natural_drain_complete",
                    "stage": stage,
                    "elapsed_seconds": round(drain_finished_at - started, 3),
                    "drain_seconds": round(
                        drain_finished_at - drain_started_at, 3
                    ),
                }
            )
            write_rows(args.out_dir / "ap_query_events.csv", events)
            missing_queries = set(query_ids) - set(scheduler.first_started_at)
            if missing_queries:
                raise RuntimeError(
                    f"{stage} admission window ended without starting queries: "
                    + ",".join(f"Q{query_id}" for query_id in sorted(missing_queries))
                )

            stage_finished_at = time.time()
            ap_result = scheduler.service_summary(stage_finished_at)
            ap_result["admission_window_seconds"] = round(
                admission_closed_at - stage_started_at, 3
            )
            ap_result["natural_drain_seconds"] = round(
                drain_finished_at - drain_started_at, 3
            )
            ap_result["stage_total_seconds"] = round(
                stage_finished_at - stage_started_at, 3
            )
            stage_progress = [
                row for row in progress_rows if row["stage"] == stage
            ]
            cpu_by_query = {
                query_id: sum(
                    float(row["cpu_seconds_delta"])
                    for row in stage_progress
                    if int(row["query_id"]) == query_id
                )
                for query_id in query_ids
            }
            read_by_query = {
                query_id: sum(
                    float(row["read_mb_delta"])
                    for row in stage_progress
                    if int(row["query_id"]) == query_id
                )
                for query_id in query_ids
            }
            write_by_query = {
                query_id: sum(
                    float(row["write_mb_delta"])
                    for row in stage_progress
                    if int(row["query_id"]) == query_id
                )
                for query_id in query_ids
            }
            queries_with_progress = sum(
                cpu_by_query[query_id] >= args.ap_min_cpu_seconds
                or read_by_query[query_id] > 0
                or write_by_query[query_id] > 0
                for query_id in query_ids
            )
            ap_result.update(
                {
                    "min_backend_cpu_seconds": round(
                        min(cpu_by_query.values(), default=0.0), 6
                    ),
                    "total_backend_cpu_seconds": round(sum(cpu_by_query.values()), 6),
                    "total_backend_read_mb": round(sum(read_by_query.values()), 6),
                    "total_backend_write_mb": round(sum(write_by_query.values()), 6),
                    "queries_with_backend_progress": queries_with_progress,
                    "query_backend_cpu_seconds": ";".join(
                        f"q{query_id}={cpu_by_query[query_id]:.6f}"
                        for query_id in query_ids
                    ),
                    "initial_wait_slo_met": (
                        bool(ap_result["all_queries_started"])
                        and float(ap_result["max_initial_wait_seconds"])
                        <= args.ap_max_initial_wait_seconds
                    ),
                    "service_slo_met": (
                        float(ap_result["min_service_seconds"])
                        >= args.ap_min_service_seconds
                    ),
                    "backend_progress_slo_met": queries_with_progress == len(query_ids),
                    "natural_completion_slo_met": bool(
                        ap_result["all_queries_completed"]
                    ),
                }
            )
            ap_result["ap_nonstarvation_slo_met"] = all(
                bool(ap_result[key])
                for key in (
                    "initial_wait_slo_met",
                    "service_slo_met",
                    "backend_progress_slo_met",
                    "natural_completion_slo_met",
                )
            )
            ap_stage_results.append(ap_result)
            write_rows(args.out_dir / "ap_stage_acceptance.csv", ap_stage_results)
            if ap_resource_controller is not None:
                resource_recommendation = {
                    **ap_resource_controller.recommendation(),
                    "query_completion_seconds": ap_result[
                        "query_completion_seconds"
                    ],
                    "natural_drain_seconds": ap_result["natural_drain_seconds"],
                    "all_queries_completed": ap_result["all_queries_completed"],
                }
                resource_recommendations.append(resource_recommendation)
                write_rows(
                    args.out_dir / "stage_ap_resource_recommendations.csv",
                    resource_recommendations,
                )

        write_rows(args.out_dir / "tp_samples.csv", samples)
        write_rows(args.out_dir / "controller_actions.csv", controls)
        write_rows(args.out_dir / "ap_query_events.csv", events)
        write_rows(args.out_dir / "ap_progress.csv", progress_rows)
        write_rows(args.out_dir / "ap_resource_actions.csv", resource_rows)
        write_rows(
            args.out_dir / "stage_ap_resource_recommendations.csv",
            resource_recommendations,
        )
        make_plot(args.out_dir / "tp_slo_closed_loop.png", samples)
        make_acceptance_plot(
            args.out_dir / "five_stage_tp_slo_acceptance.png",
            controls,
            stages,
            str(args.tp_rate),
        )
        by_stage = {}
        for stage, _query_ids in stages:
            full_stage_controls = [
                row
                for row in controls
                if row["stage"] == stage
            ]
            stage_controls = [
                row
                for row in full_stage_controls
                if row.get("phase", "admission_window") == "admission_window"
            ]
            ratios = [float(row["tp_retention_ratio"]) for row in stage_controls]
            full_lifecycle_ratios = [
                float(row["tp_retention_ratio"])
                for row in full_stage_controls
            ]
            drain_ratios = [
                float(row["tp_retention_ratio"])
                for row in full_stage_controls
                if row.get("phase") == "natural_drain"
            ]
            rolling_count = min(3, len(ratios))
            final_rolling = statistics.mean(ratios[-rolling_count:])
            by_stage[stage] = {
                "control_window_mean_retention": statistics.mean(ratios),
                "final_control_window_retention": ratios[-1],
                "final_window_slo_met": ratios[-1] >= args.tp_floor_ratio,
                "final_rolling_window_count": rolling_count,
                "final_rolling_retention": final_rolling,
                "final_rolling_slo_met": final_rolling >= args.tp_floor_ratio,
                "final_rolling_within_plus_minus_5_percent": (
                    args.tp_floor_ratio <= final_rolling <= 1.05
                ),
                "violating_control_windows": sum(
                    ratio < args.tp_floor_ratio for ratio in ratios
                ),
                "full_lifecycle_control_windows": len(full_lifecycle_ratios),
                "full_lifecycle_mean_retention": statistics.mean(
                    full_lifecycle_ratios
                ),
                "full_lifecycle_min_retention": min(full_lifecycle_ratios),
                "full_lifecycle_violating_control_windows": sum(
                    ratio < args.tp_floor_ratio
                    for ratio in full_lifecycle_ratios
                ),
                "full_lifecycle_slo_met": all(
                    ratio >= args.tp_floor_ratio
                    for ratio in full_lifecycle_ratios
                ),
                "natural_drain_control_windows": len(drain_ratios),
                "natural_drain_mean_retention": (
                    statistics.mean(drain_ratios) if drain_ratios else None
                ),
                "natural_drain_min_retention": (
                    min(drain_ratios) if drain_ratios else None
                ),
                "natural_drain_violating_control_windows": sum(
                    ratio < args.tp_floor_ratio for ratio in drain_ratios
                ),
            }
        final_rolling_values = [
            float(result["final_rolling_retention"])
            for result in by_stage.values()
        ]
        full_lifecycle_mean_values = [
            float(result["full_lifecycle_mean_retention"])
            for result in by_stage.values()
        ]
        write_rows(
            args.out_dir / "stage_acceptance.csv",
            [
                {"stage": stage, **result}
                for stage, result in by_stage.items()
            ],
        )
        return {
            "mode": "real_query_boundary_closed_loop",
            "uses_tps_for_training": False,
            "tp_reference_tps": reference_tps,
            "measured_no_ap_baseline_tps": measured_no_ap_baseline_tps,
            "baseline_elapsed_seconds": round(baseline_elapsed_seconds, 3),
            "baseline_stable_window_tps": [
                round(value, 3) for value in selected_baseline_windows
            ],
            "fixed_rate_floor_applied": (
                fixed_offered_tps is not None
                and fixed_offered_tps > measured_no_ap_baseline_tps
            ),
            "tp_reference_mode": (
                "fixed_offered_rate"
                if fixed_offered_tps is not None
                else "measured_no_ap_capacity"
            ),
            "fixed_tp_terminals": args.tp_terminals,
            "tp_offered_rate": args.tp_rate,
            "sb_runtime_mode": "guc" if sb_runtime is not None else "disabled",
            "sb_control_applied": sb_runtime is not None,
            "ap_admission_and_work_mem_applied": True,
            "ap_backend_cgroup_applied": ap_cgroup.enabled,
            "ap_cpu_quota_cores": args.ap_cpu_quota_cores,
            "ap_read_bps": args.ap_read_bps,
            "ap_write_bps": args.ap_write_bps,
            "dynamic_ap_resource_control_applied": (
                ap_resource_controller is not None
            ),
            "ap_resource_recommendations": {
                str(row["stage"]): row for row in resource_recommendations
            },
            "stage_transition_mode": "wait_for_natural_query_completion",
            "stage_seconds_meaning": "AP admission and TP acceptance window",
            "drain_timeout_seconds": args.drain_timeout_seconds,
            "stage_results": by_stage,
            "ap_stage_results": {
                str(result["stage"]): result for result in ap_stage_results
            },
            "all_stages_ap_nonstarvation_slo_met": all(
                bool(result["ap_nonstarvation_slo_met"])
                for result in ap_stage_results
            ),
            "all_stages_ap_queries_completed_naturally": all(
                bool(result["all_queries_completed"])
                for result in ap_stage_results
            ),
            "ap_max_initial_wait_seconds": args.ap_max_initial_wait_seconds,
            "ap_min_service_seconds": args.ap_min_service_seconds,
            "ap_min_cpu_seconds": args.ap_min_cpu_seconds,
            "all_stages_final_rolling_slo_met": all(
                bool(result["final_rolling_slo_met"])
                for result in by_stage.values()
            ),
            "all_stages_full_lifecycle_slo_met": all(
                bool(result["full_lifecycle_slo_met"])
                for result in by_stage.values()
            ),
            "cross_stage_final_rolling_max_min_gap": (
                max(final_rolling_values) - min(final_rolling_values)
            ),
            "cross_stage_full_lifecycle_mean_max_min_gap": (
                max(full_lifecycle_mean_values)
                - min(full_lifecycle_mean_values)
            ),
        }
    finally:
        ap_cgroup.set_frozen(False)
        for spec in reversed(live):
            tpc5stage.stop(spec)
        tpc5stage.terminate_residual_workload_backends()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sb-recommendations", required=True, type=Path)
    parser.add_argument("--work-mem-recommendations", required=True, type=Path)
    parser.add_argument("--grant-candidates", required=True, type=Path)
    parser.add_argument("--query-predictions", required=True, type=Path)
    parser.add_argument("--query-execution-trace", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--stages",
        default=",".join(stage for stage, _query_ids in STAGES),
        help="comma-separated subset of stages, kept in canonical order",
    )
    parser.add_argument("--memory-target-max-mb", type=float, default=16384)
    parser.add_argument("--initial-sb-mb", type=int, default=8192)
    parser.add_argument(
        "--sb-runtime", choices=("disabled", "guc"), default="disabled",
        help=(
            "legacy runtime-SB prototype switch; active original openGauss "
            "requires disabled and stage-boundary restarts"
        ),
    )
    parser.add_argument("--sb-data-dir", type=Path, default=Path("/opt/openGauss/data"))
    parser.add_argument(
        "--sb-gausshome",
        type=Path,
        default=Path("/home/omm/opengauss-dynamic-sb-20260726"),
    )
    parser.add_argument("--sb-resize-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--sb-control-granule-mb", type=int, default=2048)
    parser.add_argument("--tp-floor-ratio", type=float, default=0.95)
    parser.add_argument("--tp-recovery-ratio", type=float, default=0.98)
    parser.add_argument("--tp-severe-ratio", type=float, default=0.90)
    parser.add_argument("--tp-terminals", type=int, default=32)
    parser.add_argument(
        "--tp-rate",
        default="unlimited",
        help="fixed TP offered TPS for stability acceptance, or unlimited for capacity tests",
    )
    parser.add_argument("--ap-cgroup-name", default="huawei5_ap_runtime")
    parser.add_argument("--ap-cpu-quota-cores", type=float, default=0.0)
    # The blkio controller throttles the whole NVMe device; this kernel rejects
    # the mounted partition (259:3) with ENODEV.
    parser.add_argument("--ap-io-device", default="259:0")
    parser.add_argument("--ap-read-bps", type=int, default=0)
    parser.add_argument("--ap-write-bps", type=int, default=0)
    parser.add_argument(
        "--dynamic-ap-resources",
        action="store_true",
        help="safely search AP CPU/I/O quotas online under the TP SLO",
    )
    parser.add_argument(
        "--ap-cpu-quota-levels",
        default="0.25,0.5,1,2,4",
        help="ascending CPU-core probe candidates",
    )
    parser.add_argument(
        "--ap-io-mib-levels",
        default="5,10,20,40,80,160,320",
        help="ascending shared AP read/write MiB/s probe candidates",
    )
    parser.add_argument("--ap-max-initial-wait-seconds", type=float, default=135.0)
    parser.add_argument("--ap-min-service-seconds", type=float, default=30.0)
    parser.add_argument("--ap-min-cpu-seconds", type=float, default=0.25)
    parser.add_argument("--tpcc-warehouses", type=int, default=250)
    parser.add_argument("--tpch-scale", type=float, default=85.0)
    parser.add_argument("--tp-warmup-seconds", type=int, default=45)
    parser.add_argument("--baseline-seconds", type=int, default=60)
    parser.add_argument("--baseline-max-seconds", type=int, default=900)
    parser.add_argument("--baseline-ready-ratio", type=float, default=0.98)
    parser.add_argument("--baseline-stable-windows", type=int, default=3)
    parser.add_argument("--stage-seconds", type=int, default=120)
    parser.add_argument("--control-window-seconds", type=int, default=15)
    parser.add_argument(
        "--drain-timeout-seconds",
        type=int,
        default=0,
        help=(
            "diagnostic watchdog for natural SQL completion; 0 waits indefinitely "
            "and is required for acceptance runs"
        ),
    )
    parser.add_argument(
        "--tp-runtime-guard-seconds",
        type=int,
        default=604800,
        help="TP harness runtime reserved when natural AP drain has no deadline",
    )
    parser.add_argument(
        "--tp-only-measure-seconds",
        type=int,
        default=0,
        help="after baseline readiness, measure TP with no AP for this duration",
    )
    args = parser.parse_args()
    if args.sb_runtime != "disabled":
        parser.error(
            "runtime shared-buffer control is disabled: use original openGauss "
            "and apply each stage's shared_buffers with a database restart"
        )
    try:
        stages = select_stages(args.stages)
        cpu_levels = parse_positive_levels(args.ap_cpu_quota_levels)
        io_levels = parse_positive_levels(args.ap_io_mib_levels)
    except ValueError as exc:
        parser.error(str(exc))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    targets = load_targets(args.sb_recommendations, args.work_mem_recommendations)
    predictions = QueryPredictionTable(
        args.query_predictions, args.query_execution_trace
    )
    scheduler = QueryBoundaryScheduler(predictions)
    sb_runtime = None
    initial_sb_mb = args.initial_sb_mb
    if args.sb_runtime == "guc":
        sb_runtime = GucSharedBuffersRuntime(
            args.sb_data_dir,
            args.sb_gausshome,
            lambda name: tpc5stage.gsql_output(f"SHOW {name};\n"),
            timeout_seconds=args.sb_resize_timeout_seconds,
        )
        status = sb_runtime.status()
        initial_sb_mb = int(round(status["target_mb"]))
        required_sb_mb = max(target.sb_mb for target in targets.values())
        if status["startup_max_mb"] + 1e-9 < required_sb_mb:
            raise RuntimeError(
                f"patched server startup shared_buffers={status['startup_max_mb']:g}MB "
                f"cannot reach replay target {required_sb_mb}MB"
            )
    controller = TpSloController(
        targets,
        load_grant_profiles(args.grant_candidates),
        args.memory_target_max_mb,
        initial_sb_mb,
        TpSloPolicy(
            floor_ratio=args.tp_floor_ratio,
            recovery_ratio=args.tp_recovery_ratio,
            severe_ratio=args.tp_severe_ratio,
            granule_mb=args.sb_control_granule_mb,
            sb_resize_enabled=sb_runtime is not None,
            sb_shrink_enabled=False,
            cancel_running_ap_on_severe=False,
            initial_probe_ap_clients=1,
            ap_max_wait_seconds=max(
                0.0,
                args.ap_max_initial_wait_seconds - 2 * args.control_window_seconds,
            ),
        ),
    )
    ap_resource_controller = None
    if args.dynamic_ap_resources:
        ap_resource_controller = ApResourceController(
            ApResourcePolicy(
                cpu_levels=cpu_levels,
                io_levels_mib=io_levels,
                initial_cpu_cores=args.ap_cpu_quota_cores,
                initial_io_mib=args.ap_read_bps / MIB,
                tp_floor_ratio=args.tp_floor_ratio,
                tp_probe_ratio=args.tp_recovery_ratio,
            )
        )

    if args.execute:
        summary = execute(
            args,
            controller,
            scheduler,
            stages,
            sb_runtime,
            ap_resource_controller,
        )
    else:
        rows = dry_run(scheduler, targets, stages)
        write_rows(args.out_dir / "query_boundary_plan.csv", rows)
        summary = {
            "mode": "dry_run",
            "query_boundary_plan_rows": len(rows),
            "all_grants_have_exact_replay_candidates": all(
                bool(row["exact_replay_candidate"]) for row in rows
            ),
            "note": "No database workload was started; add --execute for the continuous closed-loop experiment.",
        }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
