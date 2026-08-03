#!/usr/bin/env python3
"""Run an S5-equivalent TP-only baseline with aligned I/O-latency samples."""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import time
from pathlib import Path

import io_latency_sampler as sampler
import lwtid_io_trace


TP_PASSWORD = os.environ.get("HUAWEI6_TP_PASSWORD") or os.environ.get("HUAWEI5_TP_PASSWORD", "")


def parse_tps(path: Path) -> list[tuple[float, float]]:
    import re
    pattern = re.compile(r"^\[\s*([0-9.]+)s\s*\].*\btps:\s*([0-9.]+)")
    values = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.search(line)
        if match:
            values.append((float(match.group(1)), float(match.group(2))))
    return values


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=70)
    parser.add_argument("--threads", type=int, default=128)
    parser.add_argument("--rate", type=int, default=4000)
    parser.add_argument("--device", default="nvme0n1")
    parser.add_argument("--block-trace", action="store_true")
    parser.add_argument(
        "--block-trace-script",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "bpftrace" / "lwtid_block_latency_aggregate.bt",
    )
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    log = args.out_dir / "sysbench_tp_only.log"
    command = [
        "/usr/bin/sysbench", "/usr/share/sysbench/oltp_read_only.lua",
        "--db-driver=pgsql", "--pgsql-host=127.0.0.1", "--pgsql-port=5432",
        "--pgsql-user=h5_tpuser", f"--pgsql-password={TP_PASSWORD}",
        "--pgsql-db=h5_tpcc", "--db-ps-mode=disable", "--tables=16",
        "--table-size=1000000", f"--threads={args.threads}", f"--rate={args.rate}",
        f"--time={args.seconds}", "--report-interval=1", "--percentile=95", "run",
    ]
    environment = os.environ.copy()
    environment["PGAPPNAME"] = "sysbench_tp_baseline"
    started = time.monotonic()
    rows: list[dict[str, object]] = []
    block_trace = None
    if args.block_trace:
        block_trace = lwtid_io_trace.LwtidBlockTrace(
            args.out_dir / "block_trace", args.device, args.block_trace_script
        )
        block_trace.start()
    with log.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, env=environment)
        try:
            while process.poll() is None:
                row = sampler.sample(args.device, started)
                row.update({"stage": "stage5_tp_surge", "running_ap": 0, "queued_ap": 0})
                rows.append(row)
                if block_trace is not None:
                    block_trace.snapshot_lwtids(time.monotonic() - started)
                time.sleep(1.0)
            if process.wait() != 0:
                raise RuntimeError(f"TP baseline failed; see {log}")
        finally:
            if block_trace is not None:
                block_trace.stop()
    if rows:
        write_csv(args.out_dir / "io_latency_samples.csv", rows)
    if block_trace is not None:
        subprocess.run(
            [
                os.sys.executable,
                str(Path(__file__).resolve().parent / "block_io_attribution.py"),
                "--trace-dir", str(args.out_dir / "block_trace"),
                "--out", str(args.out_dir / "block_trace_attribution.csv"),
            ],
            check=True,
        )
    tps_rows = [
        {"elapsed_seconds": int(elapsed), "stage": "stage5_tp_surge", "tp_tps": tps}
        for elapsed, tps in parse_tps(log)
    ]
    write_csv(args.out_dir / "tp_tps_samples.csv", tps_rows)
    stable_rows = tps_rows[10:-5] if len(tps_rows) > 15 else tps_rows[5:]
    mean = sum(float(row["tp_tps"]) for row in stable_rows) / max(1, len(stable_rows))
    (args.out_dir / "run_summary.json").write_text(
        '{\n  "kind": "tp_only_baseline",\n  "stage5_mean_tp_tps": %.6f\n}\n' % mean,
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
