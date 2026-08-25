#!/usr/bin/env python3
"""Run resumable paired AP calibration groups from an explicit case plan."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.operator_model import parse_explain, plan_family
from huawei7.provenance import sha256


def run(argv: list[str]) -> None:
    completed = subprocess.run(argv, cwd=ROOT, check=False)
    if completed.returncode:
        raise RuntimeError(
            "calibration subprocess failed with status %d: %s"
            % (completed.returncode, argv[1])
        )


def _matching_command(
    path: Path, *, runtime_config: Path, machine: str, query_id: str,
    query_sha: str, memory: int, application_name: str,
) -> bool:
    if not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return (
            isinstance(value, dict)
            and value.get("schema") == "huawei7.ap-command/v3"
            and value.get("measurement") == "explain_analyze_buffers"
            and value.get("machine_fingerprint") == machine
            and str(value.get("query_id")) == query_id
            and value.get("query_sha256") == query_sha
            and int(value.get("work_mem_mb", -1)) == memory
            and value.get("application_name") == application_name
            and value.get("executor") == "row; enable_vector_engine=off"
            and int(value.get("query_dop", -1)) == 1
            and value.get("runtime_config_sha256") == sha256(runtime_config)
            and isinstance(value.get("dataset"), dict)
            and isinstance(value.get("argv"), list)
            and bool(value["argv"])
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _archive_file(path: Path, *, label: str) -> None:
    attempt = 1
    while True:
        rejected = path.with_name(
            "%s.rejected-%s-%02d%s"
            % (path.stem, label, attempt, path.suffix)
        )
        if not rejected.exists():
            path.rename(rejected)
            return
        attempt += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--blind-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    runtime = json.loads(args.runtime_config.read_text(encoding="utf-8"))
    if plan.get("schema") != "huawei7.ap-group-plan/v1":
        raise ValueError("unsupported AP group plan")
    if runtime.get("machine_fingerprint") != args.machine_fingerprint:
        raise ValueError("runtime config belongs to another machine")
    query_files = runtime.get("ap_query_files")
    if not isinstance(query_files, dict):
        raise ValueError("runtime config lacks AP query files")
    cases = plan.get("groups")
    if not isinstance(cases, list) or not cases:
        raise ValueError("AP group plan is empty")
    commands_dir = args.out_dir / "commands"
    groups_dir = args.out_dir / "groups"
    commands_dir.mkdir(parents=True, exist_ok=True)
    groups_dir.mkdir(parents=True, exist_ok=True)
    completed_groups = []
    request_probe = ROOT / "probes" / "block_rq_completion_total.bt"
    request_probe_sha = sha256(request_probe)
    required_idle_seconds = float(plan.get("idle_seconds", 30))
    required_settle_seconds = float(plan.get("settle_seconds", 1))
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("AP group case must be an object")
        group_id = str(case["group_id"])
        query_id = str(int(case["query_id"]))
        memory = int(case["work_mem_mb"])
        role = str(case["role"])
        if role not in ("training", "holdout") or memory <= 0:
            raise ValueError("invalid AP calibration group role/work_mem")
        query_file = Path(str(query_files[query_id])).resolve()
        query_sha = sha256(query_file)
        blind = (args.blind_dir / f"q{query_id}-wm{memory}.json").resolve()
        family = plan_family(parse_explain(json.loads(
            blind.read_text(encoding="utf-8")
        )))
        command_path = commands_dir / (group_id + ".json")
        group_dir = groups_dir / group_id
        result_path = group_dir / "isolated_device_delta.json"
        application_name = "ppt5_ap_%s" % group_id
        if command_path.exists() and not _matching_command(
            command_path, runtime_config=args.runtime_config.resolve(),
            machine=args.machine_fingerprint, query_id=query_id,
            query_sha=query_sha, memory=memory,
            application_name=application_name,
        ):
            _archive_file(command_path, label="obsolete-contract")
        if not command_path.exists():
            run([
                sys.executable, str(ROOT / "scripts/build_ap_collection_command.py"),
                "--runtime-config", str(args.runtime_config.resolve()),
                "--query-file", str(query_file), "--query-id", query_id,
                "--work-mem-mb", str(memory),
                "--machine-fingerprint", args.machine_fingerprint,
                "--application-name", application_name,
                "--explain-analyze", "--out", str(command_path),
            ])
        command_sha = sha256(command_path)
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                result = {}
            captures = result.get("captures")
            explain_runs = result.get("explain_runs")
            result_sink = result.get(
                "instrumentation_output_during_measurement"
            )
            current_method = (
                result.get("valid") is True
                and result.get("machine_fingerprint") == args.machine_fingerprint
                and str(result.get("query_id")) == query_id
                and result.get("query_sha256") == query_sha
                and result.get("plan_family") == family
                and result.get("work_mem_mb") == memory
                and result.get("command_artifact_sha256") == command_sha
                and isinstance(explain_runs, list) and len(explain_runs) == 3
                and {str(row.get("repeat", "")) for row in explain_runs
                     if isinstance(row, dict)} == {"1", "2", "3"}
                and result.get("request_count_method")
                == "block_rq_complete_whole_device"
                and isinstance(result_sink, dict)
                and result_sink.get("filesystem") == "tmpfs"
                and result_sink.get("mountpoint") == "/dev/shm"
                and result_sink.get("promoted_after_probe_stopped") is True
                and result_sink.get(
                    "promoted_files_fsynced_before_next_capture"
                ) is True
                and isinstance(captures, list) and len(captures) == 6
                and all(
                    isinstance(row, dict)
                    and isinstance(row.get("probe_artifact"), dict)
                    and row["probe_artifact"].get("sha256") == request_probe_sha
                    and isinstance(
                        row.get("instrumentation_output_during_measurement"),
                        dict,
                    )
                    and row[
                        "instrumentation_output_during_measurement"
                    ].get("filesystem") == "tmpfs"
                    and row[
                        "instrumentation_output_during_measurement"
                    ].get("promoted_after_probe_stopped") is True
                    and row[
                        "instrumentation_output_during_measurement"
                    ].get(
                        "promoted_files_fsynced_before_next_capture"
                    ) is True
                    and (
                        row.get("requested_capture_seconds")
                        == required_idle_seconds
                        if row.get("kind") == "idle"
                        else row.get("settle_seconds")
                        == required_settle_seconds
                    )
                    for row in captures
                )
            )
            if current_method:
                completed_groups.append(group_id)
                print("resume: verified %s" % group_id, flush=True)
                continue
            attempt = 1
            while True:
                rejected = groups_dir / (
                    "%s.rejected-obsolete-evidence-%02d" % (group_id, attempt)
                )
                if not rejected.exists():
                    shutil.move(str(group_dir), str(rejected))
                    break
                attempt += 1
            print(
                "resume: archived invalid or obsolete evidence for %s" % group_id,
                flush=True,
            )
        resume = group_dir.exists()
        collector = [
            sys.executable, str(ROOT / "scripts/collect_isolated_device_delta.py"),
            "--device", str(args.device), "--command-json", str(command_path),
            "--query-file", str(query_file), "--repeats", "3",
            "--idle-seconds", str(float(plan.get("idle_seconds", 3))),
            "--settle-seconds", str(float(plan.get("settle_seconds", 1))),
            "--machine-fingerprint", args.machine_fingerprint,
            "--query-id", query_id, "--plan-family", family,
            "--work-mem-mb", str(memory), "--out-dir", str(group_dir),
        ]
        if resume:
            collector.append("--resume")
            print("resume: retrying incomplete group %s" % group_id, flush=True)
        run(collector)
        completed_groups.append(group_id)
    print(json.dumps({
        "planned": len(cases), "completed": len(completed_groups),
        "group_ids": completed_groups,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
