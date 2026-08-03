#!/usr/bin/env python3
"""Collect block request latency and attribute issuing OpenGauss threads.

The device stat counters are useful for a cheap always-on control signal, but
they cannot say whether a queueing burst originated in TP reads or AP spill
I/O.  This helper records block_rq_issue/complete pairs with bpftrace and
periodically snapshots the OpenGauss LWTID-to-application mapping.  It is an
observation facility: it writes no database setting and does not use TPS.
"""

from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import io_latency_sampler


def raw_device_number(device: str) -> int:
    stat = os.stat(Path("/dev") / device)
    major, minor = os.major(stat.st_rdev), os.minor(stat.st_rdev)
    # block tracepoints carry Linux's new_encode_dev(dev_t), whereas stat(2)
    # exposes the userspace representation.  bpftrace compares the raw field.
    return ((major & 0xFFF) << 20) | (minor & 0xFF) | ((minor & ~0xFF) << 12)


class LwtidBlockTrace:
    def __init__(self, out_dir: Path, device: str, script: Path) -> None:
        self.out_dir = out_dir
        self.device = device
        self.script = script
        self.process: subprocess.Popen[str] | None = None
        self.output_handle = None
        self.error_handle = None
        self.started_monotonic_ns = 0
        self.mappings: list[dict[str, object]] = []

    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.started_monotonic_ns = time.monotonic_ns()
        self.output_handle = (self.out_dir / "block_request_latency.csv").open(
            "w", encoding="utf-8"
        )
        self.error_handle = (self.out_dir / "block_request_latency.stderr").open(
            "w", encoding="utf-8"
        )
        self.process = subprocess.Popen(
            ["stdbuf", "-oL", "-eL", "bpftrace", str(self.script), str(raw_device_number(self.device))],
            stdout=self.output_handle,
            stderr=self.error_handle,
            text=True,
        )
        (self.out_dir / "block_request_latency_meta.json").write_text(
            json.dumps(
                {
                    "device": self.device,
                    "started_monotonic_ns": self.started_monotonic_ns,
                    "script": str(self.script),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def snapshot_lwtids(self, elapsed_seconds: float) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        sql = """
SELECT w.lwtid || ',' || a.application_name || ',' || a.datname
FROM pg_thread_wait_status w
JOIN pg_stat_activity a ON a.sessionid = w.sessionid
WHERE a.application_name LIKE 'sysbench_tp%'
   OR a.application_name LIKE 'ppt5_ap%';
"""
        try:
            output = io_latency_sampler.gsql_output(sql)
        except subprocess.CalledProcessError:
            return
        for line in output.splitlines():
            lwtid, application_name, datname = line.split(",", 2)
            self.mappings.append(
                {
                    "elapsed_seconds": round(elapsed_seconds, 6),
                    "lwtid": int(lwtid),
                    "application_name": application_name,
                    "database": datname,
                    "class": "tp" if application_name.startswith("sysbench_tp") else "ap",
                }
            )

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.output_handle is not None:
            self.output_handle.close()
        if self.error_handle is not None:
            self.error_handle.close()
        if self.mappings:
            path = self.out_dir / "lwtid_application_map.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(self.mappings[0]))
                writer.writeheader()
                writer.writerows(self.mappings)
