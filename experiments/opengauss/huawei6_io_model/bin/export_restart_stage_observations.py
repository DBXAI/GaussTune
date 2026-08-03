#!/usr/bin/env python3
"""Export the TPS-free online observation stream for restart-stage policy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--steady-audit", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    audit = json.loads(args.steady_audit.read_text(encoding="utf-8"))
    stages = audit["stages"]
    assert isinstance(stages, list)
    modes = ("low", "low", "saturated", "saturated", "surge")
    observations = []
    for mode, raw in zip(modes, stages):
        assert isinstance(raw, dict)
        # Running plus queued AP is the current admission demand.  It is a
        # monitor counter, not a recommendation or a performance label.
        observations.append({
            "stage_input": raw["stage"],
            "requested_ap_clients": int(raw["ap_count"]) + int(raw["queued_new_ap_requests"]),
            "queued_ap_clients": int(raw["queued_new_ap_requests"]),
            "tp_mode": mode,
            "host_cpu_percent": raw["mean_host_cpu_percent"],
            "dynamic_used_mb": raw["mean_dynamic_used_mb"],
            "device_iops": raw["device_iops"],
        })
    payload = {"schema": "restart_stage_online_observations_v1", "contains_actual_tps": False,
               "contains_actual_configuration": False, "observations": observations}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
