#!/usr/bin/env python3
"""Recollect only timing-invalid S5 arms; preserve every original source row.

Replacement eligibility depends exclusively on CPU endpoint timestamps, never
TPS. There are at most two fresh attempts per rejected arm. Both driver groups
are owned by collect_arm; SB restoration is unconditional.
"""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.common.db import Gsql
from jmt.common.machine import collect_machine
from collect_current_tp_matrix import (
    OnlineSB, CPUWindowBoundaryError, digest, dump, validate_cpu_window,
)
from collect_current_tp_surge import collect_arm, GATED_LUA
from recommend_current_five_stages import surge_baselines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--warmup-seconds", type=int, default=20)
    parser.add_argument("--window-seconds", type=int, default=10)
    args = parser.parse_args()
    if not 20 <= args.warmup_seconds <= 90 or not 5 <= args.window_seconds <= 60:
        raise ValueError("bounded warmup/window required")
    source = json.loads(args.matrix.read_text())
    if not source["complete"] or not source["restoration_verified"] or source["error"]:
        raise ValueError("source collection must already have completed/restored")
    if json.loads((args.matrix.parent / "SOURCE.json").read_text())["wrapper"] != GATED_LUA:
        raise ValueError("surge wrapper changed")
    rejected = {}
    for index, row in enumerate(source["rows"]):
        path = Path(row["window_path"])
        if digest(path) != row["window_sha256"]:
            raise ValueError("source window changed")
        try:
            validate_cpu_window(json.loads(path.read_text()))
        except CPUWindowBoundaryError as exc:
            rejected[index] = str(exc)
    if not rejected or len(rejected) > 3:
        raise ValueError("repair requires one to three timestamp-invalid arms")
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=15)
    active = client.json("postgres", """
SELECT coalesce(json_agg(x),'[]'::json) FROM
(SELECT application_name FROM pg_stat_activity WHERE pid<>pg_backend_pid()
 AND state='active' AND application_name NOT IN
 ('JobScheduler','PercentileJob','Asp','workload'))x
""")
    if active:
        raise RuntimeError("other workload active; repair not started")
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    if machine["machine_fingerprint"] != source["machine_fingerprint"]:
        raise ValueError("source and repair machine differ")
    args.out.mkdir(parents=True, exist_ok=False)
    dump(args.out / "MACHINE.json", machine)
    dump(args.out / "SOURCE.json", {
        "wrapper": GATED_LUA, "script_sha256": digest(__file__),
        "collector_sha256": digest(ROOT / "tools/collect_current_tp_surge.py"),
        "source_matrix": str(args.matrix.resolve()),
        "source_matrix_sha256": digest(args.matrix),
        "selection_criterion": "CPU timestamps only; no TPS-based retries",
        "maximum_attempts_per_invalid_arm": 2,
    })
    rows, ledger, error = list(source["rows"]), [], None
    sb = OnlineSB(client, args.out)
    try:
        for index, reason in sorted(rejected.items()):
            old = rows[index]
            record = {"original": old, "rejection_reason": reason, "attempts": []}
            ledger.append(record)
            sb.set(old["shared_buffers_mib"])
            for attempt in (1, 2):
                out = args.out / ("sb%d_r%02d_attempt%d" % (
                    old["shared_buffers_mib"], old["repeat"], attempt))
                out.mkdir()
                try:
                    row = collect_arm(client, sb, SimpleNamespace(
                        warmup_seconds=args.warmup_seconds,
                        window_seconds=args.window_seconds), out, old["repeat"])
                except CPUWindowBoundaryError as exc:
                    record["attempts"].append({
                        "directory": str(out.resolve()), "accepted": False,
                        "timestamp_error": str(exc)})
                    if attempt == 2:
                        raise
                    continue
                replacement = {
                    "shared_buffers_mib": old["shared_buffers_mib"],
                    "repeat": old["repeat"], "TPS": row["observed_committed_TPS"],
                    "CPU_ms_per_tx": row["CPU"]["backend_cpu_ms_per_kernel_window_transaction"],
                    "window_path": str((out / "WINDOW.json").resolve()),
                    "window_sha256": digest(out / "WINDOW.json")}
                rows[index] = replacement
                record["attempts"].append({"accepted": True, "replacement": replacement})
                print(json.dumps(replacement), flush=True)
                break
    except BaseException as exc:
        error = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        restored = sb.restore()
        dump(args.out / "REPAIR_LEDGER.json", ledger)
        result = dict(source, rows=rows, error=error,
                      complete=error is None,
                      restoration_verified=restored["config_restored_byte_exact"]
                      and restored["server_status_matches_original"],
                      source_matrix=str(args.matrix.resolve()),
                      source_matrix_sha256=digest(args.matrix),
                      discarded_source_windows_preserved=True)
        dump(args.out / "MATRIX.json", result)
    checked = surge_baselines(args.out / "MATRIX.json", machine["machine_fingerprint"])
    dump(args.out / "VALIDATED_BASELINES.json", checked)
    print(json.dumps({"complete": True, "baselines": checked}), flush=True)


if __name__ == "__main__":
    main()
