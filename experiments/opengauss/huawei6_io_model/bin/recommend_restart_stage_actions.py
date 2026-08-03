#!/usr/bin/env python3
"""Recommend restart-bounded PPT actions from online load statistics.

This adapter is intentionally separated from TPS validation.  It receives
arrival pressure, AP memory use, queue depth, TP mode and device I/O only;
observed TPS is read by the optional validator after recommendations exist.
Numeric grants are offline replay/capacity anchors, while their activation is
decided from the current stage's runtime signals.
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Policy:
    rich_sb_mb: int = 8192
    yielded_sb_mb: int = 4096
    rich_work_mem_mb: int = 1150
    protected_work_mem_mb: int = 256
    s1_ap_cap: int = 1
    s2_ap_cap: int = 2
    protected_ap_cap: int = 4
    surge_ap_cap: int = 2
    sustainable_surge_tps: int = 300
    low_tp_max_cpu: float = 20.0
    queue_threshold: int = 1


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def classify(row: dict[str, object], policy: Policy) -> dict[str, object]:
    """Classify one observed load state without reading any TPS measurement."""
    requested = int(row["requested_ap_clients"])
    queued = int(row["queued_ap_clients"])
    tp_mode = str(row["tp_mode"])
    cpu = float(row["host_cpu_percent"])
    dynamic = float(row["dynamic_used_mb"])
    device_iops = float(row["device_iops"])
    common = {
        "input_requested_ap_clients": requested,
        "input_queued_ap_clients": queued,
        "input_tp_mode": tp_mode,
        "input_host_cpu_percent": cpu,
        "input_dynamic_used_mb": dynamic,
        "input_device_iops": device_iops,
        "decision_uses_actual_tps": False,
    }
    if tp_mode == "surge":
        return {**common, "state": "S5_tp_surge", "shared_buffers_mb": policy.rich_sb_mb,
                "work_mem_mb": policy.protected_work_mem_mb, "ap_cap": policy.surge_ap_cap,
                "block_new_ap": True, "tp_surge_tps": policy.sustainable_surge_tps,
                "action": "raise SB; rebuild AP with protected grant; retain only safe AP admission"}
    if queued >= policy.queue_threshold:
        return {**common, "state": "S4_backpressure", "shared_buffers_mb": policy.yielded_sb_mb,
                "work_mem_mb": policy.protected_work_mem_mb, "ap_cap": policy.protected_ap_cap,
                "block_new_ap": True, "tp_surge_tps": 0,
                "action": "hold SB and protected grant; queue every new AP request"}
    if tp_mode == "saturated" or cpu > policy.low_tp_max_cpu:
        return {**common, "state": "S3_protect_tp", "shared_buffers_mb": policy.yielded_sb_mb,
                "work_mem_mb": policy.protected_work_mem_mb, "ap_cap": policy.protected_ap_cap,
                "block_new_ap": False, "tp_surge_tps": 0,
                "action": "stop SB yield; lower per-AP grant before AP queueing begins"}
    if requested > policy.s1_ap_cap:
        return {**common, "state": "S2_reach_limit", "shared_buffers_mb": policy.yielded_sb_mb,
                "work_mem_mb": policy.rich_work_mem_mb, "ap_cap": policy.s2_ap_cap,
                "block_new_ap": False, "tp_surge_tps": 0,
                "action": "restart with lower SB and preserve high AP grant"}
    return {**common, "state": "S1_memory_rich", "shared_buffers_mb": policy.rich_sb_mb,
            "work_mem_mb": policy.rich_work_mem_mb, "ap_cap": policy.s1_ap_cap,
            "block_new_ap": False, "tp_surge_tps": 0,
            "action": "keep rich SB and grant AP enough memory to avoid spill"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    source = read_json(args.observations)
    if source.get("contains_actual_tps") or source.get("contains_actual_configuration"):
        raise RuntimeError("recommendation input must not include TPS or actual configuration")
    observations = source.get("observations")
    if not isinstance(observations, list) or len(observations) != 5:
        raise RuntimeError("expected five TPS-free online observations")
    policy = Policy()
    rows = []
    for observation in observations:
        if not isinstance(observation, dict):
            raise RuntimeError("observation is not an object")
        rows.append({"stage_input": observation["stage_input"], **classify(observation, policy)})
    write_csv(args.out_dir / "stage_actions_blinded.csv", rows)
    summary = {"mode": "runtime_statistics_plus_offline_replay_capacity_anchors",
               "policy": policy.__dict__, "recommendations": rows,
               "validation_tps_not_loaded": True,
               "input_observation_file": str(args.observations)}
    (args.out_dir / "stage_actions_blinded.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
