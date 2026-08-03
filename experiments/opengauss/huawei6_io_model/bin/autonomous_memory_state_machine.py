#!/usr/bin/env python3
"""Rule-based five-state memory controller driven by replay outputs.

No TPS labels or measured optimums are consumed.  Dynamic memory and spill
come from source/operator replay; transitions are explicit memory and I/O
constraints.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path

from runtime_memory_controller_replay import STAGE_ORDER, StageTarget, load_targets


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@dataclass
class GrantProfile:
    assignments: str
    dynamic_peak_mb: float
    spill_io_mb: float
    spilling_operators: int


@dataclass
class Observation:
    epoch: str
    target_stage: str
    arrival_multiplier: float
    tp_high: bool = False


def load_grant_profiles(path: Path) -> dict[str, list[GrantProfile]]:
    profiles = {stage: [] for stage in STAGE_ORDER}
    for row in read_csv(path):
        stage = row["stage"]
        profiles[stage].append(
            GrantProfile(
                assignments=row["query_work_mem_assignments"],
                dynamic_peak_mb=float(row["dynamic_peak_mb"]),
                spill_io_mb=float(row["spill_io_mb"]),
                spilling_operators=int(row["spilling_operators"]),
            )
        )
    return profiles


class AutonomousController:
    def __init__(
        self,
        targets: dict[str, StageTarget],
        profiles: dict[str, list[GrantProfile]],
        memory_target_max_mb: int,
        initial_sb_mb: int,
        low_tp_sb_floor_mb: int,
        high_tp_sb_target_mb: int,
        granule_mb: int,
        max_spill_amplification: float,
    ) -> None:
        self.targets = targets
        self.profiles = profiles
        self.memory_target_max_mb = memory_target_max_mb
        self.current_sb_mb = initial_sb_mb
        self.low_tp_sb_floor_mb = low_tp_sb_floor_mb
        self.high_tp_sb_target_mb = high_tp_sb_target_mb
        self.granule_mb = granule_mb
        self.max_spill_amplification = max_spill_amplification

    def _floor_to_granule(self, value: float) -> int:
        return max(0, int(value // self.granule_mb) * self.granule_mb)

    def _result(
        self,
        observation: Observation,
        state: str,
        target: StageTarget,
        requested: int,
        admitted: int,
        dynamic_mb: float,
        spill_mb: float,
        assignments: str,
        action: str,
    ) -> dict[str, object]:
        return {
            "epoch": observation.epoch,
            "input_query_mix": observation.target_stage,
            "tp_high": observation.tp_high,
            "arrival_multiplier": observation.arrival_multiplier,
            "controller_state": state,
            "action": action,
            "sb_mb": self.current_sb_mb,
            "requested_ap_clients": requested,
            "admitted_ap_clients": admitted,
            "queued_ap_clients": requested - admitted,
            "work_mem_assignments": assignments,
            "dynamic_peak_mb": round(dynamic_mb, 3),
            "managed_memory_mb": round(self.current_sb_mb + dynamic_mb, 3),
            "memory_target_max_mb": self.memory_target_max_mb,
            "spill_io_mb": round(spill_mb, 3),
            "preferred_spill_io_mb": round(
                target.spill_io_mb * admitted / max(1, target.base_clients), 3
            ),
            "memory_limit_respected": (
                self.current_sb_mb + dynamic_mb <= self.memory_target_max_mb
            ),
        }

    def decide(self, observation: Observation) -> dict[str, object]:
        target = self.targets[observation.target_stage]
        requested = max(
            1, int(math.ceil(target.base_clients * observation.arrival_multiplier))
        )
        demand_scale = requested / max(1, target.base_clients)
        desired_dynamic = target.dynamic_peak_mb * demand_scale
        desired_spill = target.spill_io_mb * demand_scale

        if observation.tp_high:
            self.current_sb_mb = self.high_tp_sb_target_mb
            available = max(0.0, self.memory_target_max_mb - self.current_sb_mb)
            per_client = target.dynamic_peak_mb / max(1, target.base_clients)
            admitted = min(
                requested,
                int(available // per_client) if per_client > 0 else requested,
            )
            scale = admitted / max(1, target.base_clients)
            return self._result(
                observation,
                "tp_surge",
                target,
                requested,
                admitted,
                target.dynamic_peak_mb * scale,
                target.spill_io_mb * scale,
                target.work_mem_assignments,
                "raise SB first; admit only AP groups that fit the remaining pool",
            )

        if self.current_sb_mb + desired_dynamic <= self.memory_target_max_mb:
            return self._result(
                observation,
                "memory_rich",
                target,
                requested,
                requested,
                desired_dynamic,
                desired_spill,
                target.work_mem_assignments,
                "use the replay-recommended per-query grants",
            )

        max_sb_with_preferred_grants = self.memory_target_max_mb - desired_dynamic
        if max_sb_with_preferred_grants >= self.low_tp_sb_floor_mb:
            new_sb = max(
                self.low_tp_sb_floor_mb,
                self._floor_to_granule(max_sb_with_preferred_grants),
            )
            self.current_sb_mb = min(self.current_sb_mb, new_sb)
            return self._result(
                observation,
                "shared_buffer_yield",
                target,
                requested,
                requested,
                desired_dynamic,
                desired_spill,
                target.work_mem_assignments,
                "shrink SB by granules while preserving the low-TP SB floor",
            )

        spill_limit = max(
            1.0,
            desired_spill * self.max_spill_amplification,
        )
        feasible = []
        for profile in self.profiles[observation.target_stage]:
            dynamic = profile.dynamic_peak_mb * demand_scale
            spill = profile.spill_io_mb * demand_scale
            if (
                self.low_tp_sb_floor_mb + dynamic <= self.memory_target_max_mb
                and spill <= spill_limit
            ):
                feasible.append((spill, -profile.dynamic_peak_mb, profile))
        if feasible:
            _spill, _negative_dynamic, profile = min(feasible)
            self.current_sb_mb = max(self.current_sb_mb, self.low_tp_sb_floor_mb)
            return self._result(
                observation,
                "protect_tp",
                target,
                requested,
                requested,
                profile.dynamic_peak_mb * demand_scale,
                profile.spill_io_mb * demand_scale,
                profile.assignments,
                "lower per-session grants; stop shrinking SB; enforce spill budget",
            )

        self.current_sb_mb = max(self.current_sb_mb, self.low_tp_sb_floor_mb)
        available = max(0.0, self.memory_target_max_mb - self.current_sb_mb)
        per_client = target.dynamic_peak_mb / max(1, target.base_clients)
        admitted = min(
            requested,
            int(available // per_client) if per_client > 0 else requested,
        )
        admitted_scale = admitted / max(1, target.base_clients)
        return self._result(
            observation,
            "backpressure",
            target,
            requested,
            admitted,
            target.dynamic_peak_mb * admitted_scale,
            target.spill_io_mb * admitted_scale,
            target.work_mem_assignments,
            "queue new AP groups because neither SB yield nor bounded grant reduction fits",
        )


def default_observations() -> list[Observation]:
    return [
        Observation("E1_light_AP", "stage1_memory_rich", 1.0),
        Observation("E2_memory_query", "stage2_reach_limit", 1.0),
        Observation("E3_two_AP", "stage3_protect_tp", 1.0),
        Observation("E4_four_heavy_AP", "stage4_backpressure", 1.0),
        Observation("E5_AP_plus_50pct", "stage4_backpressure", 1.5),
        Observation("E6_AP_double", "stage4_backpressure", 2.0),
        Observation("E7_TP_surge", "stage5_tp_surge", 1.0, tp_high=True),
    ]


def make_plot(path: Path, rows: list[dict[str, object]]) -> None:
    import matplotlib.pyplot as plt

    labels = [str(row["epoch"]).replace("_", "\n") for row in rows]
    x = list(range(len(rows)))
    sb = [float(row["sb_mb"]) for row in rows]
    dynamic = [float(row["dynamic_peak_mb"]) for row in rows]
    requested = [int(row["requested_ap_clients"]) for row in rows]
    admitted = [int(row["admitted_ap_clients"]) for row in rows]

    fig, axes = plt.subplots(2, 1, figsize=(13, 8), constrained_layout=True)
    axes[0].bar(x, sb, label="shared_buffers", color="#2b7bba")
    axes[0].bar(x, dynamic, bottom=sb, label="admitted dynamic memory", color="#e28a28")
    axes[0].axhline(
        float(rows[0]["memory_target_max_mb"]), color="#b83b3b", linestyle="--",
        label="memory_target_max",
    )
    axes[0].set_xticks(x, labels)
    axes[0].set_ylabel("Managed memory (MB)")
    axes[0].set_title("Autonomous rule transitions from replay-derived memory demand")
    axes[0].legend()
    axes[0].grid(axis="y", alpha=0.25)
    for index, row in enumerate(rows):
        axes[0].text(
            index,
            sb[index] + dynamic[index] + 250,
            str(row["controller_state"]),
            ha="center",
            fontsize=9,
        )

    width = 0.36
    axes[1].bar(
        [value - width / 2 for value in x], requested, width,
        color="#d0d0d0", label="requested AP clients",
    )
    axes[1].bar(
        [value + width / 2 for value in x], admitted, width,
        color="#3f8f63", label="admitted AP clients",
    )
    axes[1].set_xticks(x, labels)
    axes[1].set_ylabel("AP clients")
    axes[1].set_title("Admission becomes backpressure only after bounded grant reduction fails")
    axes[1].legend()
    axes[1].grid(axis="y", alpha=0.25)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sb-recommendations", required=True, type=Path)
    parser.add_argument("--work-mem-recommendations", required=True, type=Path)
    parser.add_argument("--grant-candidates", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--memory-target-max-mb", type=int, default=16384)
    parser.add_argument("--initial-sb-mb", type=int, default=8192)
    parser.add_argument("--low-tp-sb-floor-mb", type=int, default=512)
    parser.add_argument("--high-tp-sb-target-mb", type=int, default=8192)
    parser.add_argument("--granule-mb", type=int, default=256)
    parser.add_argument("--max-spill-amplification", type=float, default=1.35)
    args = parser.parse_args()

    targets = load_targets(args.sb_recommendations, args.work_mem_recommendations)
    profiles = load_grant_profiles(args.grant_candidates)
    controller = AutonomousController(
        targets,
        profiles,
        args.memory_target_max_mb,
        args.initial_sb_mb,
        args.low_tp_sb_floor_mb,
        args.high_tp_sb_target_mb,
        args.granule_mb,
        args.max_spill_amplification,
    )
    rows = [controller.decide(item) for item in default_observations()]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "autonomous_state_transitions.csv", rows)
    trace_stage_indexes = {
        "stage1_memory_rich": 0,
        "stage2_reach_limit": 1,
        "stage3_protect_tp": 2,
        "stage4_backpressure": 3,
        "stage5_tp_surge": 6,
    }
    trace_targets = [
        {
            "stage": stage,
            "recommended_sb_mb": rows[index]["sb_mb"],
            "controller_state": rows[index]["controller_state"],
            "source_epoch": rows[index]["epoch"],
        }
        for stage, index in trace_stage_indexes.items()
    ]
    write_csv(args.out_dir / "trace_stage_sb_targets.csv", trace_targets)
    make_plot(args.out_dir / "autonomous_state_transitions.png", rows)
    summary = {
        "model": "deterministic memory/spill constrained controller",
        "uses_tps_labels": False,
        "policy_inputs": {
            "memory_target_max_mb": args.memory_target_max_mb,
            "low_tp_sb_floor_mb": args.low_tp_sb_floor_mb,
            "high_tp_sb_target_mb": args.high_tp_sb_target_mb,
            "granule_mb": args.granule_mb,
            "max_spill_amplification": args.max_spill_amplification,
        },
        "rows": rows,
        "limitations": [
            "Arrival multipliers above 1x are control-plane stress observations, not fabricated page traces.",
            "The spill amplification bound is an explicit operator policy and must be replaced by a measured device/TP I/O budget before deployment.",
            "State actions are not executed by the current openGauss kernel.",
        ],
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
