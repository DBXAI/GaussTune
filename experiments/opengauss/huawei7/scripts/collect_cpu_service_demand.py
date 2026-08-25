#!/usr/bin/env python3
"""Collect isolated TP/AP CPU service demand.

This is the CPU analogue of the isolated I/O service calibration.  It never
runs TP and AP together and never uses a mixed-stage TPS as a calibration
target.  The output is therefore suitable as a portable resource surface:
rerun the same isolated measurements on a new machine, then feed the surface
to the queueing model.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.cpu_contention import (
    sample_process_roots, summarize_window,
)
from huawei7.dataset import dataset_audit_from_runtime
from huawei7.native_stats_session import DatabaseStatsSession
from huawei7.provenance import sha256
from huawei7.stage_execution import (
    ap_gsql_command, benchbase_command, benchbase_xml, sysbench_command,
    tp_connection,
)


def _runtime(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("runtime config must be an object")
    if value.get("schema") != "huawei7.stage-runtime/v1":
        raise ValueError("unsupported runtime config")
    return value


def _gaussdb_pid(data_dir: str = "/opt/openGauss/data") -> int:
    expected = str(Path(data_dir).resolve())
    candidates = []
    for path in Path("/proc").glob("[0-9]*"):
        try:
            command = (path / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            )
        except OSError:
            continue
        if "gaussdb" in command and expected in command:
            try:
                candidates.append(int(path.name))
            except ValueError:
                pass
    if not candidates:
        raise RuntimeError("cannot find running gaussdb for %s" % expected)
    return max(candidates)


def _sample_for(
    root_pid: int, seconds: float, interval_seconds: float,
) -> List[Mapping[str, object]]:
    started = time.monotonic()
    rows = []
    while time.monotonic() - started <= seconds:
        rows.append(sample_process_roots([root_pid]))
        time.sleep(interval_seconds)
    return rows


def _sample_until(
    process: subprocess.Popen[str], root_pid: int, *,
    interval_seconds: float, timeout_seconds: float,
    keepalive=None,
) -> Tuple[List[Mapping[str, object]], int]:
    started = time.monotonic()
    next_keepalive = started + 15.0 if keepalive is not None else None
    rows = []
    while True:
        rows.append(sample_process_roots([root_pid]))
        status = process.poll()
        if status is not None:
            return rows, int(status)
        if (
            keepalive is not None
            and next_keepalive is not None
            and time.monotonic() >= next_keepalive
        ):
            keepalive()
            next_keepalive = time.monotonic() + 15.0
        if time.monotonic() - started > timeout_seconds:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=30)
            raise TimeoutError(
                "isolated CPU workload exceeded %.1fs" % timeout_seconds
            )
        time.sleep(interval_seconds)


def _sample_after_benchbase_warmup(
    process: subprocess.Popen[str],
    root_pid: int,
    log_path: Path,
    *,
    interval_seconds: float,
    timeout_seconds: float,
) -> List[Mapping[str, object]]:
    """Return only post-warmup CPU samples for a BenchBase run."""

    started = time.monotonic()
    while True:
        status = process.poll()
        text = log_path.read_text(encoding="utf-8", errors="replace")
        if "Warmup complete, starting measurements." in text:
            break
        if status is not None:
            raise RuntimeError(
                "BenchBase exited before its warmup marker with status %d"
                % int(status)
            )
        if time.monotonic() - started > timeout_seconds:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=30)
            raise TimeoutError("BenchBase warmup marker timed out")
        time.sleep(interval_seconds)
    # Establish the measurement-window first sample after the marker, then
    # continue until the driver exits.  Warmup CPU is intentionally excluded.
    rows = [sample_process_roots([root_pid])]
    while True:
        status = process.poll()
        if status is not None:
            return rows
        if time.monotonic() - started > timeout_seconds:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=30)
            raise TimeoutError(
                "post-warmup isolated CPU workload exceeded %.1fs"
                % timeout_seconds
            )
        time.sleep(interval_seconds)
        rows.append(sample_process_roots([root_pid]))


def _command_document(path: Path, replacements: Mapping[str, object]) -> List[str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError("command JSON must be an argv list: %s" % path)
    result = []
    for item in value:
        for key, replacement in replacements.items():
            item = item.replace("{%s}" % key, str(replacement))
        result.append(item)
    return result


def _run_precondition(
    *,
    repeat_dir: Path,
    restart_command_json: Optional[Path],
    dataset_reset_command_json: Optional[Path],
    shared_buffers_mb: int,
) -> None:
    repeat_dir.mkdir(parents=True, exist_ok=True)
    if dataset_reset_command_json is not None:
        report = repeat_dir / "dataset-reset.json"
        command = _command_document(
            dataset_reset_command_json,
            {"reset_report": report},
        )
        with (repeat_dir / "dataset-reset.log").open(
            "w", encoding="utf-8"
        ) as handle:
            subprocess.run(
                command, check=True, stdout=handle,
                stderr=subprocess.STDOUT, text=True,
            )
    if restart_command_json is not None:
        command = _command_document(
            restart_command_json,
            {"shared_buffers_mb": shared_buffers_mb},
        )
        with (repeat_dir / "restart.log").open(
            "w", encoding="utf-8"
        ) as handle:
            subprocess.run(
                command, check=True, stdout=handle,
                stderr=subprocess.STDOUT, text=True,
            )


def _summary_units(result_dir: Path) -> float:
    summaries = sorted(result_dir.rglob("*.summary.json"))
    if len(summaries) != 1:
        raise RuntimeError(
            "expected exactly one TPCC summary in %s, found %d"
            % (result_dir, len(summaries))
        )
    value = json.loads(summaries[0].read_text(encoding="utf-8"))
    units = float(value.get("Measured Requests", 0))
    if units <= 0:
        raise RuntimeError("TPCC summary contains no measured requests")
    return units


def _sysbench_units(log_path: Path) -> float:
    """Read total transactions from a sysbench summary log."""

    import re

    text = log_path.read_text(encoding="utf-8", errors="replace")
    matches = re.findall(r"transactions:\s*([0-9]+)", text)
    if not matches:
        raise RuntimeError("sysbench log contains no transaction count")
    units = float(matches[-1])
    if units <= 0:
        raise RuntimeError("sysbench log contains no positive transactions")
    return units


def _run_one(
    *,
    config: Mapping[str, object],
    mode: str,
    query_id: str,
    work_mem_mb: int,
    terminals: int,
    warmup_seconds: int,
    measure_seconds: int,
    out_dir: Path,
    repeat: int,
    idle_seconds: float,
    sample_interval_seconds: float,
    timeout_seconds: float,
    precondition_dir: Path,
    restart_command_json: Optional[Path],
    dataset_reset_command_json: Optional[Path],
    shared_buffers_mb: int,
) -> Mapping[str, object]:
    _run_precondition(
        repeat_dir=precondition_dir,
        restart_command_json=restart_command_json,
        dataset_reset_command_json=dataset_reset_command_json,
        shared_buffers_mb=shared_buffers_mb,
    )
    root_pid = _gaussdb_pid()
    idle_samples = _sample_for(root_pid, idle_seconds, sample_interval_seconds)
    idle_window = summarize_window(
        idle_samples,
        int(idle_samples[0]["monotonic_ns"]),
        int(idle_samples[-1]["monotonic_ns"]),
    )
    idle_samples_path = out_dir / (
        "repeat-%02d.idle-cpu-samples.json" % repeat
    )
    idle_samples_path.write_text(
        json.dumps(list(idle_samples), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    scratch = Path(tempfile.mkdtemp(
        prefix="huawei7-cpu-service-", dir="/dev/shm",
    ))
    try:
        log_path = out_dir / ("repeat-%02d.workload.log" % repeat)
        result_dir = scratch / "results"
        result_dir.mkdir()
        ap_stats = None
        ap_stats_before = None
        if mode == "tpcc":
            tp = config["tp"]["benchbase-tpcc"]  # type: ignore[index]
            if not isinstance(tp, dict):
                raise ValueError("BenchBase TPCC runtime config is invalid")
            password_name = str(tp["password_env"])
            password = os.environ.get(password_name, "")
            if not password:
                raise RuntimeError(
                    "required password environment variable is unset: %s"
                    % password_name
                )
            xml_path = scratch / "tpcc.xml"
            xml_path.write_text(
                benchbase_xml(
                    config,
                    terminals=terminals,
                    warmup_seconds=warmup_seconds,
                    measure_seconds=measure_seconds,
                    password=password,
                ),
                encoding="utf-8",
            )
            os.chmod(xml_path, 0o600)
            command = benchbase_command(
                config, xml_path=xml_path, result_dir=result_dir,
            )
            units_name = "transaction"
        elif mode == "sysbench":
            connection = tp_connection(config, "sysbench")
            password_name = str(connection["password_env"])
            password = os.environ.get(password_name, "")
            if not password:
                raise RuntimeError(
                    "required password environment variable is unset: %s"
                    % password_name
                )
            secret_path = scratch / "sysbench-secret.cfg"
            secret_path.write_text(
                "pgsql-password=%s\n" % password, encoding="utf-8"
            )
            os.chmod(secret_path, 0o600)
            command = sysbench_command(
                config,
                terminals=terminals,
                total_seconds=measure_seconds,
                config_file=secret_path,
            )
            units_name = "transaction"
        elif mode == "ap":
            query_files = config.get("ap_query_files")
            if not isinstance(query_files, dict):
                raise ValueError("runtime config lacks ap_query_files")
            query_path = Path(str(query_files[str(query_id)]))
            command = ap_gsql_command(
                config,
                query_file=query_path,
                work_mem_mb=work_mem_mb,
                application_name="huawei7_cpu_service_q%s_r%d"
                % (query_id, repeat),
            )
            units_name = "query"
            ap_stats = DatabaseStatsSession(observer_nice=-10)
            ap_database = str(config["postgres"]["ap_database"])
            ap_stats_before = ap_stats.snapshot(ap_database)
        else:
            raise ValueError("mode must be tpcc or ap")
        try:
            with log_path.open("w", encoding="utf-8") as handle:
                process = subprocess.Popen(
                    list(command), stdout=handle, stderr=subprocess.STDOUT,
                    text=True, env=dict(os.environ), start_new_session=True,
                )
                if mode == "tpcc":
                    work_samples = _sample_after_benchbase_warmup(
                        process, root_pid, log_path,
                        interval_seconds=sample_interval_seconds,
                        timeout_seconds=timeout_seconds,
                    )
                    status = int(process.wait())
                else:
                    work_samples, status = _sample_until(
                        process, root_pid,
                        interval_seconds=sample_interval_seconds,
                        timeout_seconds=timeout_seconds,
                        keepalive=(
                            ap_stats.keepalive
                            if ap_stats is not None else None
                        ),
                    )
            ap_stats_after = (
                ap_stats.snapshot(ap_database) if ap_stats is not None else None
            )
        finally:
            if ap_stats is not None:
                ap_stats.close()
        if status != 0:
            raise RuntimeError(
                "%s repeat %d exited with status %d; see %s"
                % (mode, repeat, status, log_path)
            )
        work_window = summarize_window(
            work_samples,
            int(work_samples[0]["monotonic_ns"]),
            int(work_samples[-1]["monotonic_ns"]),
        )
        workload_samples_path = out_dir / (
            "repeat-%02d.workload-cpu-samples.json" % repeat
        )
        workload_samples_path.write_text(
            json.dumps(list(work_samples), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        idle_cpu_rate = idle_window.process_cpu_seconds / idle_window.wall_seconds
        excess_cpu_seconds = max(
            0.0,
            work_window.process_cpu_seconds
            - idle_cpu_rate * work_window.wall_seconds,
        )
        if mode == "tpcc":
            units = _summary_units(result_dir)
        elif mode == "sysbench":
            units = _sysbench_units(log_path)
        else:
            units = 1.0
        row = {
            "schema": "huawei7.cpu-service-demand-repeat/v1",
            "machine_fingerprint": str(config["machine_fingerprint"]),
            "dataset_fingerprint": dataset_audit_from_runtime(
                config, machine_fingerprint=str(config["machine_fingerprint"])
            )[0]["dataset_fingerprint"],
            "mode": mode,
            "key": (
                "tpcc" if mode == "tpcc"
                else "sysbench" if mode == "sysbench"
                else str(query_id)
            ),
            "units": units_name,
            "unit_count": units,
            "idle_seconds": idle_window.wall_seconds,
            "workload_seconds": work_window.wall_seconds,
            "idle_process_cpu_seconds": idle_window.process_cpu_seconds,
            "workload_process_cpu_seconds": work_window.process_cpu_seconds,
            "idle_cpu_seconds_per_wall_second": idle_cpu_rate,
            "excess_process_cpu_seconds": excess_cpu_seconds,
            "cpu_seconds_per_unit": excess_cpu_seconds / units,
            "wall_seconds_per_unit": work_window.wall_seconds / units,
            "logical_cpus": os.cpu_count(),
            "raw_workload_log": {
                "path": str(log_path.resolve()),
                "sha256": sha256(log_path),
            },
            "raw_cpu_samples": {
                "idle": {
                    "path": str(idle_samples_path.resolve()),
                    "sha256": sha256(idle_samples_path),
                },
                "workload": {
                    "path": str(workload_samples_path.resolve()),
                    "sha256": sha256(workload_samples_path),
                },
            },
            "calibration_contract": {
                "final_stage_tps_used": False,
                "target_stage_tps_used_for_calibration": False,
                "mixed_tp_ap_tps_used": False,
                "isolated_workload_only": True,
                "database_buffer_accesses_measured": (
                    mode == "ap"
                ),
            },
            "valid": True,
        }
        if mode == "ap":
            if ap_stats_before is None or ap_stats_after is None:
                raise RuntimeError("AP buffer stats were not captured")
            delta = {
                key: int(ap_stats_after[key]) - int(ap_stats_before[key])
                for key in ("buffer_accesses", "blks_hit", "blks_read")
                if key in ap_stats_before and key in ap_stats_after
            }
            accesses = float(delta.get("buffer_accesses", 0))
            if accesses <= 0:
                accesses = float(
                    delta.get("blks_hit", 0) + delta.get("blks_read", 0)
                )
            if accesses <= 0:
                raise RuntimeError("AP query produced no buffer accesses")
            row["buffer_accesses_per_unit"] = accesses / units
            row["buffer_accesses_per_second"] = (
                accesses / work_window.wall_seconds
            )
            row["shared_buffer_hit_ratio"] = (
                float(delta.get("blks_hit", 0)) / accesses
            )
        path = out_dir / ("repeat-%02d.json" % repeat)
        path.write_text(
            json.dumps(row, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return row
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("tpcc", "sysbench", "ap"), required=True,
    )
    parser.add_argument("--query-id", default="")
    parser.add_argument("--work-mem-mb", type=int, default=64)
    parser.add_argument("--terminals", type=int, default=128)
    parser.add_argument("--warmup-seconds", type=int, default=60)
    parser.add_argument("--measure-seconds", type=int, default=120)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--idle-seconds", type=float, default=15)
    parser.add_argument("--sample-interval-seconds", type=float, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument("--restart-command-json", type=Path)
    parser.add_argument("--dataset-reset-command-json", type=Path)
    parser.add_argument("--shared-buffers-mb", type=int, default=8192)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "ap" and not args.query_id:
        parser.error("--query-id is required for AP mode")
    if args.repeats < 3 or args.idle_seconds < 5:
        parser.error("CPU service calibration requires >=3 repeats and idle>=5s")
    config = _runtime(args.runtime_config)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for repeat in range(1, args.repeats + 1):
        row = _run_one(
            config=config,
            mode=args.mode,
            query_id=args.query_id,
            work_mem_mb=args.work_mem_mb,
            terminals=args.terminals,
            warmup_seconds=args.warmup_seconds,
            measure_seconds=args.measure_seconds,
            out_dir=args.out_dir,
            repeat=repeat,
            idle_seconds=args.idle_seconds,
            sample_interval_seconds=args.sample_interval_seconds,
            timeout_seconds=args.timeout_seconds,
            precondition_dir=args.out_dir / ("repeat-%02d-state" % repeat),
            restart_command_json=args.restart_command_json,
            dataset_reset_command_json=args.dataset_reset_command_json,
            shared_buffers_mb=args.shared_buffers_mb,
        )
        rows.append(row)
        print(json.dumps({
            "mode": args.mode,
            "key": row["key"],
            "repeat": repeat,
            "cpu_seconds_per_unit": row["cpu_seconds_per_unit"],
        }, sort_keys=True), flush=True)
    summary = {
        "schema": "huawei7.cpu-service-demand/v1",
        "machine_fingerprint": str(config["machine_fingerprint"]),
        "mode": args.mode,
        "key": rows[0]["key"],
        "units": rows[0]["units"],
        "repeats": rows,
        "calibration_contract": {
            "final_stage_tps_used": False,
            "target_stage_tps_used_for_calibration": False,
            "mixed_tp_ap_tps_used": False,
            "isolated_workload_only": True,
            "minimum_repeats": 3,
            "database_buffer_accesses_measured": args.mode == "ap",
        },
        "valid": True,
    }
    (args.out_dir / "cpu-service-demand.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "schema": summary["schema"],
        "mode": args.mode,
        "key": summary["key"],
        "repeats": len(rows),
        "valid": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
