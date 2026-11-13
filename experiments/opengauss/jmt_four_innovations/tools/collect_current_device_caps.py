#!/usr/bin/env python3
"""Bounded, same-machine fio calibration on a NEW disposable marked file only.

No database files, GUC changes, device writes, or restarts. Physical rates use
interior, monotonic-clock sysfs windows while fio is running, not fio API IOPS.
The result is a capacity approximation, NOT a qualified mixed-load IO bridge.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql
from jmt.common.machine import collect_machine
from jmt.innovation_4_performance.fio_surface import prepare_target, validate_target

POINTS = [
    ("r8_qd1", [("tp", "randread", 8, 1, None)]),
    ("r8_qd16", [("tp", "randread", 8, 16, None)]),
    ("r8_qd40", [("tp", "randread", 8, 40, None)]),
    ("r8_qd64", [("tp", "randread", 8, 64, None)]),
    ("w8_qd40", [("tp", "randwrite", 8, 40, None)]),
    ("rw8_33r_qd40", [("tp", "randrw", 8, 40, 33)]),
    ("s128_qd8", [("ap", "read", 128, 8, None)]),
    ("r8_qd40+s128_qd2", [
        ("tp", "randread", 8, 40, None), ("ap", "read", 128, 2, None)]),
]


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine-document", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runtime", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 8 <= args.runtime <= 30 or not 3 <= args.repeats <= 5:
        parser.error("bounded runtime 8..30 and repeats 3..5 required")
    expected = json.loads(args.machine_document.read_text())["machine"]
    machine = collect_machine(Path(expected["device"]), Path("/opt/openGauss/bin/gaussdb"),
                              expected["source_commit"])
    if machine != expected:
        raise ValueError("machine changed")
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=15)

    def idle():
        n = client.json("postgres", """SELECT row_to_json(t) FROM
            (SELECT count(*) AS n FROM pg_stat_activity
             WHERE datname IN ('h5_tpcc','h5_tpch') AND pid<>pg_backend_pid()
             AND application_name<>'AutoVacWorker')t""")["n"]
        if n:
            raise ValueError("database has workload sessions; refuse fio calibration")

    idle()
    args.out.mkdir(parents=True, exist_ok=False)
    target = args.out / "huawei7_innovation4_disposable.img"
    if target.parent.stat().st_dev != Path("/opt/openGauss/data").stat().st_dev:
        raise ValueError("fio scratch and database are on different filesystems")
    mount_device = subprocess.check_output(
        ["findmnt", "-no", "SOURCE", "-T", str(target.parent)], text=True).strip()
    if not Path(mount_device).resolve().name.startswith(Path(expected["device"]).name):
        raise ValueError("scratch filesystem is not on the bound block device")
    prepare_target(target, 512 * 1024 * 1024)
    marker = validate_target(target)
    stat_path = Path("/sys/block") / Path(expected["device"]).name / "stat"
    points, artifacts = [], []
    fio = shutil.which("fio")
    for name, jobs in POINTS:
        for repeat in range(1, args.repeats + 1):
            idle()
            command = [
                fio, "--output-format=json", "--eta=never", "--thread=1",
                "--time_based=1", "--runtime=%d" % args.runtime, "--ramp_time=2",
                "--direct=1", "--ioengine=libaio", "--randrepeat=1",
                "--randseed=%d" % (42100 + repeat),
            ]
            for index, (job, rw, bs, qd, mix) in enumerate(jobs):
                command += [
                    "--name=" + job, "--filename=" + str(target),
                    "--rw=" + rw, "--bs=%dk" % bs, "--iodepth=%d" % qd,
                    "--offset=%d" % (index * marker["size_bytes"] // 2),
                    "--size=%d" % (marker["size_bytes"] // 2),
                ]
                if mix is not None:
                    command.append("--rwmixread=%d" % mix)
            raw = args.out / ("%s_r%d_FIO.json" % (name, repeat))
            error = args.out / ("%s_r%d_STDERR.txt" % (name, repeat))
            samples = []
            with raw.open("x") as stdout, error.open("x") as stderr:
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
                deadline = time.monotonic() + args.runtime + 45
                try:
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            raise TimeoutError("bounded fio deadline exceeded")
                        samples.append({
                            "monotonic_ns": time.monotonic_ns(),
                            "stat": list(map(int, stat_path.read_text().split())),
                        })
                        time.sleep(.25)
                    if process.returncode:
                        raise RuntimeError("fio failed: " + error.read_text()[:300])
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
            document = json.loads(raw.read_text())
            if len(document["jobs"]) != len(jobs) or any(j["error"] for j in document["jobs"]):
                raise ValueError("incomplete fio jobs")
            # Detect onset from actual block IO, then omit >=3 seconds (ramp +
            # dispatch startup). Also omit the final >=1 second. Preserve all
            # timestamps/counters; do not multiply observations to fix windows.
            onset = next((s["monotonic_ns"] for s in samples[1:]
                          if s["stat"][0] + s["stat"][4]
                          > samples[0]["stat"][0] + samples[0]["stat"][4] + 100), None)
            if onset is None:
                raise ValueError("no physical IO observed")
            selected = [s for s in samples if onset + 3_000_000_000 <= s["monotonic_ns"]
                        <= samples[-1]["monotonic_ns"] - 1_000_000_000]
            if len(selected) < 8:
                raise ValueError("insufficient interior physical-rate window")
            a, b = selected[0], selected[-1]
            duration = (b["monotonic_ns"] - a["monotonic_ns"]) / 1e9
            delta = [y - x for x, y in zip(a["stat"], b["stat"])]
            if any(v < 0 for v in delta[:8]):
                raise ValueError("device counters reset")
            rates = {
                "seconds": duration, "read_iops": delta[0] / duration,
                "write_iops": delta[4] / duration,
                "read_mb_s": delta[2] * 512 / duration / 1e6,
                "write_mb_s": delta[6] * 512 / duration / 1e6,
            }
            job_rows = {}
            for job in document["jobs"]:
                row = {}
                for direction in ("read", "write"):
                    io = job[direction]
                    if io["total_ios"]:
                        row[direction] = {
                            "iops": io["iops"], "mb_s": io["bw_bytes"] / 1e6,
                            "lat_ms": (io["clat_ns"]["mean"] + io["slat_ns"]["mean"]) / 1e6,
                        }
                job_rows[job["jobname"]] = row
            observation = {
                "point": name, "repeat": repeat, "jobs": job_rows,
                "device_whole_run": rates,
                "physical_rate_scope": "whole-device interior monotonic window; not fio-owned",
                "role": "validation" if "+" in name else "calibration",
                "selected_first": a, "selected_last": b, "samples": samples,
                "command": command, "raw_path": str(raw.resolve()), "raw_sha256": sha(raw),
            }
            obs = args.out / ("%s_r%d_OBSERVATION.json" % (name, repeat))
            dump(obs, observation)
            artifacts += [{"path": str(p.resolve()), "sha256": sha(p)} for p in (raw, obs)]
            points.append(observation)
            print(json.dumps({"point": name, "repeat": repeat, "rates": rates}), flush=True)
    medians = []
    for name, _ in POINTS:
        rows = [r for r in points if r["point"] == name]
        medians.append({
            "point": name,
            "device_whole_run": {key: statistics.median(
                r["device_whole_run"][key] for r in rows)
                for key in rows[0]["device_whole_run"]},
            "jobs": {job: {direction: {key: statistics.median(
                r["jobs"][job][direction][key] for r in rows)
                for key in rows[0]["jobs"][job][direction]}
                for direction in rows[0]["jobs"][job]} for job in rows[0]["jobs"]},
            "role": rows[0]["role"],
        })
    dump(args.out / "DEVICE_CAPS.json", {
        "schema": "jmt.current-machine-device-caps/v1", "machine": machine,
        "machine_fingerprint": machine["machine_fingerprint"], "points": medians,
        "created_at": datetime.now().astimezone().isoformat(),
        "source_artifacts": artifacts, "target_marker": marker,
        "cap_model_is_fio_capacity_approximation_not_2D_latency_surface": True,
        "AP_physical_IO_bridge_qualified": False, "configuration_applied": False,
        "collector_sha256": sha(Path(__file__)),
    })
    # Only our just-created, identity-checked disposable scratch is removed.
    validate_target(target)
    target.unlink()
    print(json.dumps({"completed": True, "scratch_removed": True}), flush=True)


if __name__ == "__main__":
    main()
