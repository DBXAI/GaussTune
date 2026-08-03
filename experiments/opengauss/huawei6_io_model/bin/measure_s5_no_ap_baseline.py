#!/usr/bin/env python3
"""Measure the original Stage 5 TP streams without any AP clients."""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from types import SimpleNamespace

import tpc5stage


def db_counters() -> tuple[int, int, int]:
    output = tpc5stage.gsql_output(
        """
SELECT (xact_commit + xact_rollback)::bigint,
       blks_hit::bigint,
       blks_read::bigint
FROM pg_stat_database
WHERE datname = 'h5_tpcc';
"""
    )
    fields = output.strip().split("|")
    if len(fields) != 3:
        fields = output.strip().split()
    if len(fields) != 3:
        raise RuntimeError(f"unexpected pg_stat_database output: {output!r}")
    return tuple(int(value.strip()) for value in fields)  # type: ignore[return-value]


def session_counts() -> tuple[int, int]:
    output = tpc5stage.gsql_output(
        """
SELECT count(*)::int,
       coalesce(sum(CASE WHEN state = 'active' THEN 1 ELSE 0 END), 0)::int
FROM pg_stat_activity
WHERE datname = 'h5_tpcc'
  AND application_name LIKE 'tpcc%';
"""
    )
    fields = output.strip().split("|")
    return int(fields[0]), int(fields[1])


def wait_for_sessions(expected: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        total, _active = session_counts()
        if total >= expected:
            return
        time.sleep(0.5)
    raise TimeoutError(f"TPCC sessions did not reach {expected}; current={session_counts()[0]}")


def runtime_args(total_seconds: int) -> SimpleNamespace:
    return SimpleNamespace(
        seed=20260614,
        tpcc_warehouses=250,
        tpch_scale=85.0,
        ap_work_mem="1024MB",
        ap_temp_file_limit="",
        tp_low_terminals=2,
        tp_low_rate=40,
        tp_high_terminals=12,
        tp_high_rate="unlimited",
        stable_tp_high_rate="180",
        stable_workload=True,
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
        sample_interval=5,
        stage_boundary_mode="time",
        tpch_start_timeout_seconds=60.0,
        tpch_query_timeout_seconds=0.0,
        tp_run_seconds=total_seconds,
        total_seconds=total_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--sb-mb", required=True, type=int)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--measure-seconds", type=int, default=90)
    parser.add_argument("--sample-interval", type=int, default=5)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    total_seconds = args.warmup_seconds + args.measure_seconds + 90
    paths = tpc5stage.render_configs(runtime_args(total_seconds))
    live: list[tpc5stage.ProcSpec] = []
    tpc5stage.terminate_residual_workload_backends()

    try:
        low = tpc5stage.start(
            "tpcc_low_no_ap",
            tpc5stage.benchbase_cmd(
                "tpcc",
                paths["tpcc_low"],
                create=False,
                load=False,
                execute=True,
                output_dir=args.out_dir / "benchbase_low",
            ),
            args.out_dir / "tpcc_low.log",
        )
        high = tpc5stage.start(
            "tpcc_high_no_ap",
            tpc5stage.benchbase_cmd(
                "tpcc",
                paths["tpcc_high"],
                create=False,
                load=False,
                execute=True,
                output_dir=args.out_dir / "benchbase_high",
            ),
            args.out_dir / "tpcc_high.log",
        )
        live.extend([low, high])
        wait_for_sessions(14)
        time.sleep(args.warmup_seconds)

        start_wall = time.time()
        start_tx, start_hit, start_read = db_counters()
        previous_wall = start_wall
        previous_tx = start_tx
        samples: list[float] = []
        session_samples: list[tuple[int, int]] = []
        deadline = start_wall + args.measure_seconds
        while time.time() < deadline:
            time.sleep(min(args.sample_interval, max(0.05, deadline - time.time())))
            now = time.time()
            tx, _hit, _read = db_counters()
            elapsed = now - previous_wall
            if elapsed > 0:
                samples.append((tx - previous_tx) / elapsed)
            session_samples.append(session_counts())
            previous_wall = now
            previous_tx = tx

        end_wall = time.time()
        end_tx, end_hit, end_read = db_counters()
        elapsed = end_wall - start_wall
        accesses = (end_hit - start_hit) + (end_read - start_read)
        row = {
            "sb_mb": args.sb_mb,
            "ap_clients": 0,
            "tp_low_terminals": 2,
            "tp_low_target_tps": 40,
            "tp_high_terminals": 12,
            "tp_high_target_tps": 180,
            "target_total_tps": 220,
            "measure_seconds": f"{elapsed:.3f}",
            "transactions": end_tx - start_tx,
            "total_tp_tps": f"{(end_tx - start_tx) / elapsed:.6f}",
            "sample_min_tps": f"{min(samples):.6f}" if samples else "",
            "sample_max_tps": f"{max(samples):.6f}" if samples else "",
            "avg_tpcc_sessions": (
                f"{sum(total for total, _active in session_samples) / len(session_samples):.3f}"
                if session_samples
                else ""
            ),
            "avg_active_tpcc_sessions": (
                f"{sum(active for _total, active in session_samples) / len(session_samples):.3f}"
                if session_samples
                else ""
            ),
            "sb_hit_rate": f"{(end_hit - start_hit) / accesses:.6f}" if accesses else "",
        }
        output = args.out_dir / "no_ap_tps.csv"
        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        (args.out_dir / "run_config.json").write_text(
            json.dumps(vars(args), default=str, indent=2) + "\n",
            encoding="utf-8",
        )
        print(output)
        print(json.dumps(row, indent=2))
    finally:
        for spec in reversed(live):
            tpc5stage.stop(spec)
        tpc5stage.terminate_residual_workload_backends()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
