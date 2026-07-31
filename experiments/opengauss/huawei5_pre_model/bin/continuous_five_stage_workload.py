#!/usr/bin/env python3
"""Generate and run the PPT-defined continuous Huawei5 five-stage workload.

The five stages are pressure states in one workload trajectory.  They are not
five independent query batches: TP stays alive, AP requests keep arriving,
and an AP statement may start in one stage and finish in a later stage.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable


SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import tpc5stage  # noqa: E402


DEFAULT_QUERY_IDS = (3, 5, 7, 9, 13, 18, 21)
DEFAULT_INTERVALS = (90.0, 60.0, 30.0, 15.0, 15.0)
DEFAULT_WORK_MEM_MB = {
    3: 1150,
    5: 1024,
    7: 1083,
    9: 1174,
    13: 1024,
    18: 4096,
    21: 2968,
}
DEFAULT_OPERATOR_COVERAGE = (
    PACKAGE_ROOT / "results" / "tpch_memory_operator_audit_20260721"
    / "operator_coverage.csv"
)
SYSBENCH_SCRIPT = Path("/usr/share/sysbench/oltp_read_write.lua")


@dataclass(frozen=True)
class PhaseSpec:
    index: int
    name: str
    tp_mode: str
    ap_arrival_interval_seconds: float
    expected_state: str
    expected_controller_action: str


@dataclass(frozen=True)
class ApRequest:
    request_id: int
    query_id: int
    due_elapsed_seconds: float
    arrival_stage: str


@dataclass
class RunningRequest:
    request: ApRequest
    started_elapsed_seconds: float
    start_stage: str
    work_mem_mb: int
    process: object
    application_name: str


def phase_blueprints(intervals: tuple[float, ...]) -> tuple[PhaseSpec, ...]:
    if len(intervals) != 5:
        raise ValueError("exactly five AP arrival intervals are required")
    if any(value <= 0 for value in intervals):
        raise ValueError("AP arrival intervals must be positive")
    if any(intervals[index + 1] > intervals[index] for index in range(3)):
        raise ValueError("AP arrival rate must not decrease from S1 through S4")
    if intervals[4] > intervals[3]:
        raise ValueError("AP injection must continue at S4 rate or faster in S5")

    definitions = (
        (
            "stage1_memory_rich",
            "low",
            "few AP requests; memory remains below memory_target_max",
            "increase per-query dynamic memory to reduce spill",
        ),
        (
            "stage2_reach_limit",
            "low",
            "AP pressure reaches memory_target_max",
            "shrink shared_buffers by granules and transfer capacity to AP",
        ),
        (
            "stage3_protect_tp",
            "low",
            "AP arrival pressure continues after the memory limit",
            "stop shrinking shared_buffers and lower per-query AP grants",
        ),
        (
            "stage4_backpressure",
            "low",
            "new AP requests continue when no safe memory remains",
            "queue new AP requests; let already running SQL continue",
        ),
        (
            "stage5_tp_surge",
            "high",
            "TP jumps from about 10% CPU to more than 60% TP-only CPU",
            "raise shared_buffers and gracefully lower running AP grants",
        ),
    )
    return tuple(
        PhaseSpec(index + 1, name, tp_mode, intervals[index], state, action)
        for index, (name, tp_mode, state, action) in enumerate(definitions)
    )


class ContinuousProtocol:
    def __init__(
        self,
        phase_seconds: float,
        intervals: tuple[float, ...],
        query_ids: tuple[int, ...],
    ) -> None:
        if phase_seconds <= 0:
            raise ValueError("phase duration must be positive")
        if not query_ids:
            raise ValueError("AP query cycle must not be empty")
        self.phase_seconds = phase_seconds
        self.phases = phase_blueprints(intervals)
        self.query_ids = query_ids
        self.total_seconds = phase_seconds * len(self.phases)
        self.arrivals = self._build_arrivals()

    def _build_arrivals(self) -> tuple[ApRequest, ...]:
        requests: list[ApRequest] = []
        query_index = 0
        request_id = 0
        for phase in self.phases:
            start = (phase.index - 1) * self.phase_seconds
            end = phase.index * self.phase_seconds
            due = start + phase.ap_arrival_interval_seconds / 2.0
            while due < end - 1e-9:
                request_id += 1
                requests.append(
                    ApRequest(
                        request_id=request_id,
                        query_id=self.query_ids[query_index % len(self.query_ids)],
                        due_elapsed_seconds=round(due, 6),
                        arrival_stage=phase.name,
                    )
                )
                query_index += 1
                due += phase.ap_arrival_interval_seconds
        if not requests or requests[0].due_elapsed_seconds <= 0:
            raise ValueError("protocol must start with zero AP and inject later")
        return tuple(requests)

    def phase_at(self, elapsed_seconds: float) -> PhaseSpec:
        index = min(
            len(self.phases) - 1,
            max(0, int(elapsed_seconds // self.phase_seconds)),
        )
        return self.phases[index]

    def planned_rows(self) -> list[dict[str, object]]:
        return [asdict(request) for request in self.arrivals]


class ContinuousApScheduler:
    """Maintain AP request lifecycle without a stage-boundary drain."""

    def __init__(self, requests: tuple[ApRequest, ...]) -> None:
        self.scheduled = deque(requests)
        self.pending: deque[ApRequest] = deque()
        self.running: dict[int, RunningRequest] = {}
        self.finished: list[dict[str, object]] = []

    def enqueue_due(self, elapsed_seconds: float) -> list[ApRequest]:
        due: list[ApRequest] = []
        while (
            self.scheduled
            and self.scheduled[0].due_elapsed_seconds <= elapsed_seconds + 1e-9
        ):
            request = self.scheduled.popleft()
            self.pending.append(request)
            due.append(request)
        return due

    def take_launchable(self, max_running: int, block_new: bool = False) -> list[ApRequest]:
        if max_running < 0:
            raise ValueError("max_running must not be negative")
        if block_new:
            return []
        slots = max(0, max_running - len(self.running))
        return [self.pending.popleft() for _ in range(min(slots, len(self.pending)))]

    def mark_started(
        self,
        request: ApRequest,
        elapsed_seconds: float,
        stage: str,
        work_mem_mb: int,
        process: object,
        application_name: str,
    ) -> None:
        if request.request_id in self.running:
            raise ValueError(f"request {request.request_id} is already running")
        self.running[request.request_id] = RunningRequest(
            request=request,
            started_elapsed_seconds=elapsed_seconds,
            start_stage=stage,
            work_mem_mb=work_mem_mb,
            process=process,
            application_name=application_name,
        )

    def poll_completed(
        self,
        elapsed_seconds: float,
        stage: str,
        poll: Callable[[object], int | None] | None = None,
    ) -> list[dict[str, object]]:
        poll_process = poll or (lambda process: process.poll())
        completed: list[dict[str, object]] = []
        for request_id, running in list(self.running.items()):
            return_code = poll_process(running.process)
            if return_code is None:
                continue
            row = {
                "request_id": request_id,
                "query_id": running.request.query_id,
                "arrival_stage": running.request.arrival_stage,
                "start_stage": running.start_stage,
                "completion_stage": stage,
                "work_mem_mb": running.work_mem_mb,
                "queue_wait_seconds": round(
                    running.started_elapsed_seconds
                    - running.request.due_elapsed_seconds,
                    3,
                ),
                "service_seconds": round(
                    elapsed_seconds - running.started_elapsed_seconds,
                    3,
                ),
                "return_code": return_code,
                "crossed_stage_boundary": running.start_stage != stage,
                "application_name": running.application_name,
            }
            completed.append(row)
            self.finished.append(row)
            del self.running[request_id]
        return completed

    def done(self) -> bool:
        return not self.scheduled and not self.pending and not self.running


def parse_number_tuple(value: str, cast: Callable[[str], object]) -> tuple:
    values = tuple(cast(item.strip()) for item in value.split(",") if item.strip())
    if not values:
        raise ValueError("value list must not be empty")
    return values


def parse_work_mem(value: str) -> dict[int, int]:
    result: dict[int, int] = {}
    for item in value.split(";"):
        query, separator, memory = item.strip().partition("=")
        if not separator or not query.lower().startswith("q"):
            raise ValueError(f"invalid work_mem assignment: {item!r}")
        result[int(query[1:])] = int(memory)
    return result


def read_control_state(
    path: Path | None,
    default_max_running: int,
    default_work_mem: dict[int, int],
) -> dict[str, object]:
    default = {
        "admitted_ap_clients": default_max_running,
        "block_new_ap": False,
        "work_mem_mb": dict(default_work_mem),
        "source": "workload_default",
    }
    if path is None:
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {**default, "source": "control_file_not_created"}
    except json.JSONDecodeError:
        # Controllers should publish with rename(2).  This fallback also
        # tolerates one poll of a non-atomic first writer.
        return {**default, "source": "control_file_incomplete"}
    assignments = payload.get("work_mem_mb", default_work_mem)
    if isinstance(assignments, str):
        assignments = parse_work_mem(assignments)
    else:
        assignments = {
            int(query): int(memory) for query, memory in assignments.items()
        }
    admitted = int(payload.get("admitted_ap_clients", default_max_running))
    if admitted < 0:
        raise ValueError("control admitted_ap_clients must not be negative")
    return {
        "admitted_ap_clients": admitted,
        "block_new_ap": bool(payload.get("block_new_ap", False)),
        "work_mem_mb": assignments,
        "source": "external_control_file",
    }


def validate_operator_coverage(query_ids: tuple[int, ...], path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise FileNotFoundError(f"operator coverage file does not exist: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = {int(row["query_id"]): row for row in csv.DictReader(handle)}
    checks: list[dict[str, object]] = []
    for query_id in query_ids:
        row = rows.get(query_id)
        if row is None:
            raise ValueError(f"Q{query_id} is absent from operator coverage")
        aggregate_count = int(row["hash_aggregate"]) + int(row["group_aggregate"])
        valid = int(row["hash_join"]) > 0 and int(row["sort"]) > 0 and aggregate_count > 0
        checks.append(
            {
                "query_id": query_id,
                "hash_join_count": int(row["hash_join"]),
                "aggregate_count": aggregate_count,
                "sort_count": int(row["sort"]),
                "valid_complex_ap": valid,
            }
        )
        if not valid:
            raise ValueError(
                f"Q{query_id} does not contain Hash Join + aggregate + Sort"
            )
    return checks


def protocol_document(
    protocol: ContinuousProtocol,
    args: argparse.Namespace,
    coverage: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "protocol": "ppt_continuous_five_stage_v1",
        "load_semantics": {
            "single_database_instance": True,
            "stage_boundary_ap_drain": False,
            "cancel_ap_on_normal_path": False,
            "ap_requests_continue_during_stage4_and_stage5": True,
            "tp_low_process_survives_stage5": True,
            "stage5_uses_incremental_tp_process": True,
            "controller_actions_hard_coded_in_workload": False,
        },
        "tp": {
            "generator": "sysbench oltp_read_write",
            "low_threads": args.tp_low_threads,
            "low_rate": args.tp_low_rate,
            "high_total_threads": args.tp_high_threads,
            "high_total_rate": args.tp_high_rate,
            "low_cpu_acceptance": "about 10% total host CPU",
            "high_cpu_acceptance": ">=60% total host CPU with TP only",
        },
        "ap": {
            "generator": "TPC-H single-query sessions",
            "database": args.tpch_database,
            "scale_factor": args.tpch_scale,
            "query_cycle": list(protocol.query_ids),
            "operator_coverage": coverage,
            "arrival_count": len(protocol.arrivals),
            "external_control_state_file": (
                str(args.control_state_file) if args.control_state_file else None
            ),
        },
        "phases": [asdict(phase) for phase in protocol.phases],
        "acceptance_invariants": [
            "AP count is zero at t=0",
            "AP arrival rate is non-decreasing from S1 through S5",
            "running AP statements survive phase transitions",
            "S1-S4 use low TP and S5 adds the TP surge",
            "normal completion performs zero AP cancellations and zero DB restarts",
        ],
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_protocol_artifacts(
    protocol: ContinuousProtocol,
    args: argparse.Namespace,
) -> tuple[Path, dict[str, object]]:
    coverage = validate_operator_coverage(protocol.query_ids, args.operator_coverage)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    document = protocol_document(protocol, args, coverage)
    protocol_path = args.out_dir / "workload_protocol.json"
    protocol_path.write_text(
        json.dumps(document, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    write_csv(args.out_dir / "planned_ap_arrivals.csv", protocol.planned_rows())
    write_csv(args.out_dir / "ap_operator_coverage.csv", coverage)
    phase_counts = {
        phase.name: sum(
            request.arrival_stage == phase.name for request in protocol.arrivals
        )
        for phase in protocol.phases
    }
    validation = {
        "passed": True,
        "checks": {
            "zero_ap_at_t0": protocol.arrivals[0].due_elapsed_seconds > 0,
            "nondecreasing_ap_arrival_rate": all(
                protocol.phases[index + 1].ap_arrival_interval_seconds
                <= protocol.phases[index].ap_arrival_interval_seconds
                for index in range(4)
            ),
            "tp_low_s1_through_s4": all(
                phase.tp_mode == "low" for phase in protocol.phases[:4]
            ),
            "tp_high_only_from_s5": protocol.phases[4].tp_mode == "high",
            "all_ap_queries_have_required_memory_operators": all(
                bool(row["valid_complex_ap"]) for row in coverage
            ),
            "stage_boundary_drain_disabled": not document["load_semantics"][
                "stage_boundary_ap_drain"
            ],
            "normal_path_ap_cancellation_disabled": not document["load_semantics"][
                "cancel_ap_on_normal_path"
            ],
        },
        "planned_arrivals_by_stage": phase_counts,
        "planned_total_arrivals": len(protocol.arrivals),
    }
    validation["passed"] = all(validation["checks"].values())
    (args.out_dir / "static_protocol_validation.json").write_text(
        json.dumps(validation, indent=2) + "\n", encoding="utf-8"
    )
    report_lines = [
        "# Continuous five-stage workload protocol",
        "",
        f"Static protocol validation: **{'PASS' if validation['passed'] else 'FAIL'}**",
        "",
        "| Stage | TP mode | AP interval (s) | Planned arrivals | Expected controller action |",
        "|---|---|---:|---:|---|",
    ]
    for phase in protocol.phases:
        report_lines.append(
            f"| S{phase.index} | {phase.tp_mode} | "
            f"{phase.ap_arrival_interval_seconds:g} | {phase_counts[phase.name]} | "
            f"{phase.expected_controller_action} |"
        )
    report_lines.extend(
        [
            "",
            "The workload only creates the PPT-defined TP/AP pressure trajectory. "
            "The expected actions are observations for the controller and are not "
            "hard-coded as workload outcomes.",
            "",
            "A real run additionally requires an accepted TP-only CPU calibration, "
            "zero AP cancellations, zero database restarts, and natural completion "
            "of every admitted AP statement.",
            "",
        ]
    )
    (args.out_dir / "PROTOCOL_REPORT.md").write_text(
        "\n".join(report_lines), encoding="utf-8"
    )
    return protocol_path, document


def sysbench_common(args: argparse.Namespace) -> list[str]:
    return [
        str(args.sysbench_binary),
        str(args.sysbench_script),
        "--db-driver=pgsql",
        f"--pgsql-host={args.pg_host}",
        f"--pgsql-port={args.pg_port}",
        f"--pgsql-user={args.tp_user}",
        f"--pgsql-password={args.tp_password}",
        f"--pgsql-db={args.tp_database}",
        "--db-ps-mode=disable",
        f"--tables={args.sysbench_tables}",
        f"--table-size={args.sysbench_table_size}",
    ]


def sysbench_run_command(
    args: argparse.Namespace,
    threads: int,
    rate: int,
    seconds: int | None = None,
) -> list[str]:
    if threads <= 0 or rate <= 0:
        raise ValueError("sysbench threads and rate must be positive")
    return sysbench_common(args) + [
        f"--threads={threads}",
        f"--rate={rate}",
        f"--time={seconds if seconds is not None else args.tp_process_seconds}",
        "--report-interval=1",
        "--percentile=95",
        "run",
    ]


def start_process(
    name: str,
    command: list[str],
    log_path: Path,
    application_name: str,
) -> tpc5stage.ProcSpec:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    environment = os.environ.copy()
    environment["PGAPPNAME"] = application_name
    print(f"[{time.strftime('%F %T')}] start {name}: {' '.join(command)}", flush=True)
    process = subprocess.Popen(
        command,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    return tpc5stage.ProcSpec(name=name, proc=process, log=log_path)


def stop_tp_process(spec: tpc5stage.ProcSpec) -> None:
    if spec.proc.poll() is not None:
        return
    spec.proc.send_signal(signal.SIGTERM)
    try:
        spec.proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        spec.proc.kill()
        spec.proc.wait(timeout=10)


def check_execution_preconditions(args: argparse.Namespace) -> None:
    for path in (args.sysbench_binary, args.sysbench_script):
        if not Path(path).exists():
            raise RuntimeError(f"required executable/input does not exist: {path}")
    free_gib = shutil.disk_usage("/").free / 1024**3
    if free_gib < args.minimum_free_gib:
        raise RuntimeError(
            f"only {free_gib:.2f} GiB is free on /; at least "
            f"{args.minimum_free_gib:.2f} GiB is required before AP spill testing"
        )
    table_check = tpc5stage.gsql_output(
        "SELECT count(*) FROM pg_tables WHERE tablename LIKE 'sbtest%';",
        db=args.tp_database,
    )
    if int(table_check or "0") < args.sysbench_tables:
        raise RuntimeError(
            f"{args.tp_database} has {table_check or 0} sysbench tables; "
            f"run this script's prepare command first"
        )


def calibration_path(args: argparse.Namespace) -> Path:
    return args.tp_calibration_file or (args.out_dir / "tp_cpu_calibration.json")


def measure_tp_only_cpu(
    args: argparse.Namespace,
    name: str,
    threads: int,
    rate: int,
) -> tuple[float, list[dict[str, object]]]:
    duration = args.calibration_warmup_seconds + args.calibration_sample_seconds
    spec = start_process(
        name,
        sysbench_run_command(args, threads, rate, duration + 10),
        args.out_dir / f"{name}.log",
        name,
    )
    started_at = time.monotonic()
    samples: list[dict[str, object]] = []
    tpc5stage.LAST_CPU = None
    tpc5stage.cpu_percent()
    try:
        while time.monotonic() - started_at < duration:
            time.sleep(1.0)
            return_code = spec.proc.poll()
            if return_code is not None:
                raise RuntimeError(
                    f"{name} exited early with code {return_code}; see {spec.log}"
                )
            elapsed = time.monotonic() - started_at
            cpu = tpc5stage.cpu_percent()
            if cpu and elapsed >= args.calibration_warmup_seconds:
                samples.append(
                    {
                        "profile": name,
                        "elapsed_seconds": round(elapsed, 3),
                        "cpu_percent": float(cpu),
                    }
                )
    finally:
        stop_tp_process(spec)
    if not samples:
        raise RuntimeError(f"no CPU samples collected for {name}")
    return sum(float(row["cpu_percent"]) for row in samples) / len(samples), samples


def calibrate_tp(args: argparse.Namespace) -> dict[str, object]:
    check_execution_preconditions(args)
    low_mean, low_samples = measure_tp_only_cpu(
        args, "sysbench_calibration_low", args.tp_low_threads, args.tp_low_rate
    )
    time.sleep(args.calibration_cooldown_seconds)
    high_mean, high_samples = measure_tp_only_cpu(
        args, "sysbench_calibration_high", args.tp_high_threads, args.tp_high_rate
    )
    low_pass = args.tp_low_cpu_min <= low_mean <= args.tp_low_cpu_max
    high_pass = high_mean >= args.tp_high_cpu_min
    result = {
        "generator": "sysbench oltp_read_write",
        "tp_database": args.tp_database,
        "sysbench_tables": args.sysbench_tables,
        "sysbench_table_size": args.sysbench_table_size,
        "low": {
            "threads": args.tp_low_threads,
            "rate": args.tp_low_rate,
            "mean_host_cpu_percent": round(low_mean, 3),
            "required_range_percent": [args.tp_low_cpu_min, args.tp_low_cpu_max],
            "passed": low_pass,
        },
        "high": {
            "threads": args.tp_high_threads,
            "rate": args.tp_high_rate,
            "mean_host_cpu_percent": round(high_mean, 3),
            "required_minimum_percent": args.tp_high_cpu_min,
            "passed": high_pass,
        },
        "accepted": low_pass and high_pass,
    }
    output = calibration_path(args)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    write_csv(args.out_dir / "tp_cpu_calibration_samples.csv", low_samples + high_samples)
    if not result["accepted"]:
        raise RuntimeError(
            "TP CPU calibration did not meet the PPT thresholds; adjust TP rates/threads "
            f"and rerun calibration (low={low_mean:.2f}%, high={high_mean:.2f}%)"
        )
    return result


def require_matching_calibration(args: argparse.Namespace) -> dict[str, object]:
    path = calibration_path(args)
    if not path.exists():
        raise RuntimeError(
            f"missing TP-only CPU calibration: {path}; run the calibrate command first"
        )
    result = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "low": (args.tp_low_threads, args.tp_low_rate),
        "high": (args.tp_high_threads, args.tp_high_rate),
    }
    for profile, (threads, rate) in expected.items():
        actual = result.get(profile, {})
        if actual.get("threads") != threads or actual.get("rate") != rate:
            raise RuntimeError(
                f"TP calibration {profile} profile does not match this run: "
                f"calibrated={actual.get('threads')} threads/{actual.get('rate')} TPS, "
                f"requested={threads} threads/{rate} TPS"
            )
    if not result.get("accepted"):
        raise RuntimeError(f"TP calibration is not accepted: {path}")
    return result


def prepare_sysbench(args: argparse.Namespace) -> None:
    command = sysbench_common(args) + [f"--threads={args.prepare_threads}", "prepare"]
    subprocess.run(command, check=True)


def ap_runtime_args(
    args: argparse.Namespace,
    query_id: int,
    work_mem_mb: int,
    application_name: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        tpch_scale=args.tpch_scale,
        tpch_database=args.tpch_database,
        ap_work_mem=f"{work_mem_mb}MB",
        ap_application_name=application_name,
        query_id=query_id,
    )


class EventLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("w", encoding="utf-8")

    def append(self, event: str, elapsed: float, stage: str, **fields: object) -> None:
        row = {
            "event": event,
            "wall_time": time.strftime("%F %T"),
            "elapsed_seconds": round(elapsed, 3),
            "stage": stage,
            **fields,
        }
        self.handle.write(json.dumps(row, ensure_ascii=True) + "\n")
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


def execute(
    args: argparse.Namespace,
    protocol: ContinuousProtocol,
    document: dict[str, object],
) -> dict[str, object]:
    check_execution_preconditions(args)
    calibration = require_matching_calibration(args)
    work_mem = parse_work_mem(args.ap_work_mem)
    missing = set(protocol.query_ids) - set(work_mem)
    if missing:
        raise ValueError(f"missing AP work_mem for Query IDs: {sorted(missing)}")
    if args.tp_high_threads <= args.tp_low_threads:
        raise ValueError("high TP threads must exceed low TP threads")
    if args.tp_high_rate <= args.tp_low_rate:
        raise ValueError("high TP rate must exceed low TP rate")

    scheduler = ContinuousApScheduler(protocol.arrivals)
    events = EventLog(args.out_dir / "events.jsonl")
    tp_processes: list[tpc5stage.ProcSpec] = []
    failed_ap: list[dict[str, object]] = []
    cpu_samples: list[dict[str, object]] = []
    started_at = time.monotonic()
    current_phase = protocol.phases[0]
    s5_started = False
    normal_completion = False
    previous_control_signature: tuple[object, ...] | None = None

    low = start_process(
        "sysbench_tp_low",
        sysbench_run_command(args, args.tp_low_threads, args.tp_low_rate),
        args.out_dir / "sysbench_tp_low.log",
        "sysbench_tp_low",
    )
    tp_processes.append(low)
    events.append("phase_enter", 0.0, current_phase.name, tp_mode="low")
    tpc5stage.LAST_CPU = None
    tpc5stage.cpu_percent()

    try:
        while True:
            elapsed = time.monotonic() - started_at
            in_drain = elapsed >= protocol.total_seconds
            phase = (
                protocol.phases[-1]
                if in_drain
                else protocol.phase_at(elapsed)
            )
            if phase.name != current_phase.name:
                events.append(
                    "phase_exit",
                    elapsed,
                    current_phase.name,
                    running_ap=len(scheduler.running),
                    queued_ap=len(scheduler.pending),
                    stage_boundary_drain=False,
                )
                current_phase = phase
                events.append(
                    "phase_enter",
                    elapsed,
                    current_phase.name,
                    tp_mode=current_phase.tp_mode,
                    inherited_running_ap=len(scheduler.running),
                )

            if current_phase.tp_mode == "high" and not s5_started:
                surge_threads = args.tp_high_threads - args.tp_low_threads
                surge_rate = args.tp_high_rate - args.tp_low_rate
                surge = start_process(
                    "sysbench_tp_surge_delta",
                    sysbench_run_command(args, surge_threads, surge_rate),
                    args.out_dir / "sysbench_tp_surge_delta.log",
                    "sysbench_tp_surge_delta",
                )
                tp_processes.append(surge)
                s5_started = True
                events.append(
                    "tp_surge_start",
                    elapsed,
                    current_phase.name,
                    total_threads=args.tp_high_threads,
                    total_rate=args.tp_high_rate,
                )

            for request in scheduler.enqueue_due(elapsed):
                events.append(
                    "ap_arrive",
                    elapsed,
                    request.arrival_stage,
                    request_id=request.request_id,
                    query_id=request.query_id,
                    queue_depth=len(scheduler.pending),
                )

            completion_stage = "natural_drain" if in_drain else current_phase.name
            for row in scheduler.poll_completed(elapsed, completion_stage):
                events.append("ap_complete", elapsed, completion_stage, **row)
                if int(row["return_code"]) != 0:
                    failed_ap.append(row)

            control = read_control_state(
                args.control_state_file, args.ap_max_running, work_mem
            )
            control_work_mem = control["work_mem_mb"]
            control_missing = set(protocol.query_ids) - set(control_work_mem)
            if control_missing:
                raise ValueError(
                    "control state is missing work_mem for Query IDs: "
                    f"{sorted(control_missing)}"
                )
            control_signature = (
                control["admitted_ap_clients"],
                control["block_new_ap"],
                tuple(sorted(control_work_mem.items())),
                control["source"],
            )
            if control_signature != previous_control_signature:
                events.append(
                    "control_observed",
                    elapsed,
                    completion_stage,
                    admitted_ap_clients=control["admitted_ap_clients"],
                    block_new_ap=control["block_new_ap"],
                    work_mem_mb=control_work_mem,
                    control_source=control["source"],
                )
                previous_control_signature = control_signature

            for request in scheduler.take_launchable(
                int(control["admitted_ap_clients"]),
                bool(control["block_new_ap"]),
            ):
                memory_mb = control_work_mem[request.query_id]
                app_name = f"ppt5_ap_r{request.request_id:04d}_q{request.query_id}"
                runtime = ap_runtime_args(
                    args, request.query_id, memory_mb, app_name
                )
                spec = tpc5stage.start(
                    app_name,
                    tpc5stage.tpch_single_query_cmd(request.query_id, runtime),
                    args.out_dir / "ap_logs" / f"{app_name}.log",
                )
                start_stage = "natural_drain" if in_drain else current_phase.name
                scheduler.mark_started(
                    request,
                    elapsed,
                    start_stage,
                    memory_mb,
                    spec.proc,
                    app_name,
                )
                events.append(
                    "ap_start",
                    elapsed,
                    start_stage,
                    request_id=request.request_id,
                    query_id=request.query_id,
                    arrival_stage=request.arrival_stage,
                    work_mem_mb=memory_mb,
                    running_ap=len(scheduler.running),
                    queue_wait_seconds=round(
                        elapsed - request.due_elapsed_seconds, 3
                    ),
                )

            for spec in tp_processes:
                return_code = spec.proc.poll()
                if return_code is not None:
                    raise RuntimeError(
                        f"TP generator {spec.name} exited early with code {return_code}; "
                        f"see {spec.log}"
                    )

            cpu = tpc5stage.cpu_percent()
            if cpu:
                cpu_samples.append(
                    {
                        "elapsed_seconds": round(elapsed, 3),
                        "stage": completion_stage,
                        "cpu_percent": float(cpu),
                        "running_ap": len(scheduler.running),
                        "queued_ap": len(scheduler.pending),
                    }
                )

            if in_drain and scheduler.done():
                normal_completion = True
                events.append(
                    "workload_complete",
                    elapsed,
                    "natural_drain",
                    ap_cancellations=0,
                    database_restarts=0,
                )
                break
            time.sleep(args.poll_interval)
    finally:
        for spec in reversed(tp_processes):
            stop_tp_process(spec)
        events.close()

    write_csv(args.out_dir / "ap_completions.csv", scheduler.finished)
    write_csv(args.out_dir / "cpu_samples.csv", cpu_samples)
    stage_cpu: dict[str, list[float]] = {}
    for row in cpu_samples:
        if row["stage"] == "natural_drain":
            continue
        stage_cpu.setdefault(str(row["stage"]), []).append(float(row["cpu_percent"]))
    summary = {
        "protocol": document["protocol"],
        "normal_completion": normal_completion,
        "ap_requests": len(protocol.arrivals),
        "ap_completed": len(scheduler.finished),
        "ap_failed": len(failed_ap),
        "ap_cancellations": 0,
        "database_restarts": 0,
        "cross_stage_completions": sum(
            bool(row["crossed_stage_boundary"]) for row in scheduler.finished
        ),
        "stage_mean_host_cpu_percent": {
            stage: round(sum(values) / len(values), 3)
            for stage, values in stage_cpu.items()
            if values
        },
        "cpu_acceptance_requires_tp_only_calibration": True,
        "tp_cpu_calibration": calibration,
    }
    (args.out_dir / "run_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    if failed_ap:
        raise RuntimeError(f"{len(failed_ap)} AP requests failed; see ap_completions.csv")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "prepare", "calibrate", "run"))
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=PACKAGE_ROOT / "results" / "continuous_five_stage_workload_latest",
    )
    parser.add_argument("--phase-seconds", type=float, default=180.0)
    parser.add_argument(
        "--ap-arrival-intervals",
        default=",".join(str(value) for value in DEFAULT_INTERVALS),
        help="S1..S5 request intervals; shorter means more pressure",
    )
    parser.add_argument(
        "--ap-query-cycle",
        default=",".join(str(value) for value in DEFAULT_QUERY_IDS),
    )
    parser.add_argument(
        "--ap-work-mem",
        default=";".join(f"q{query}={memory}" for query, memory in DEFAULT_WORK_MEM_MB.items()),
    )
    parser.add_argument("--ap-max-running", type=int, default=4)
    parser.add_argument(
        "--control-state-file",
        type=Path,
        help=(
            "controller-owned JSON with admitted_ap_clients, block_new_ap, and "
            "per-query work_mem_mb; publish updates atomically"
        ),
    )
    parser.add_argument("--operator-coverage", type=Path, default=DEFAULT_OPERATOR_COVERAGE)
    parser.add_argument("--tpch-scale", type=float, default=10.0)
    parser.add_argument("--tpch-database", default="h5_tpch_sf10")
    parser.add_argument("--tp-low-threads", type=int, default=2)
    parser.add_argument("--tp-low-rate", type=int, default=40)
    parser.add_argument("--tp-high-threads", type=int, default=32)
    parser.add_argument("--tp-high-rate", type=int, default=800)
    parser.add_argument("--tp-process-seconds", type=int, default=86400)
    parser.add_argument("--tp-calibration-file", type=Path)
    parser.add_argument("--calibration-warmup-seconds", type=int, default=15)
    parser.add_argument("--calibration-sample-seconds", type=int, default=45)
    parser.add_argument("--calibration-cooldown-seconds", type=int, default=10)
    parser.add_argument("--tp-low-cpu-min", type=float, default=7.0)
    parser.add_argument("--tp-low-cpu-max", type=float, default=15.0)
    parser.add_argument("--tp-high-cpu-min", type=float, default=60.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--minimum-free-gib", type=float, default=30.0)
    parser.add_argument("--sysbench-binary", type=Path, default=Path("/usr/bin/sysbench"))
    parser.add_argument("--sysbench-script", type=Path, default=SYSBENCH_SCRIPT)
    parser.add_argument("--sysbench-tables", type=int, default=16)
    parser.add_argument("--sysbench-table-size", type=int, default=1_000_000)
    parser.add_argument("--prepare-threads", type=int, default=16)
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=tpc5stage.PORT)
    parser.add_argument("--tp-database", default=tpc5stage.TPCC_DB)
    parser.add_argument("--tp-user", default=tpc5stage.TP_USER)
    parser.add_argument("--tp-password", default=tpc5stage.TP_PASS)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        intervals = parse_number_tuple(args.ap_arrival_intervals, float)
        query_ids = parse_number_tuple(args.ap_query_cycle, int)
        protocol = ContinuousProtocol(args.phase_seconds, intervals, query_ids)
        protocol_path, document = write_protocol_artifacts(protocol, args)
        if args.command == "prepare":
            check_free = shutil.disk_usage("/").free / 1024**3
            if check_free < args.minimum_free_gib:
                raise RuntimeError(
                    f"only {check_free:.2f} GiB is free on /; prepare requires "
                    f"at least {args.minimum_free_gib:.2f} GiB"
                )
            prepare_sysbench(args)
        elif args.command == "calibrate":
            result = calibrate_tp(args)
            print(json.dumps(result, indent=2), flush=True)
        elif args.command == "run":
            summary = execute(args, protocol, document)
            print(json.dumps(summary, indent=2), flush=True)
        else:
            print(
                f"protocol: {protocol_path}\n"
                f"planned AP requests: {len(protocol.arrivals)}\n"
                f"duration before natural drain: {protocol.total_seconds:g}s",
                flush=True,
            )
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
