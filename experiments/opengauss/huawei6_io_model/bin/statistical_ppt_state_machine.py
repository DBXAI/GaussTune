#!/usr/bin/env python3
"""Stateful, TPS-free recommender for the PPT's five mixed-load actions.

The controller deliberately has no ``stage`` or ``tp_mode`` input.  It only
uses monitorable demand: current SB, AP arrival/running counts, a replay-based
AP dynamic-memory demand estimate, and the offered TP rate.  TP capacity is a
TP-only calibration anchor, never an observed mixed-load TPS value.

The action is restart-bounded on stock openGauss whenever ``shared_buffers``
changes.  Therefore an S5 AP grant applies when AP sessions are recreated;
the controller does not claim it can resize a running statement.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    # The controller budget is below the DB process envelope.  It leaves
    # operating-system and fixed-memory headroom while allowing S1's measured
    # Q18 peak (about 2.1GiB alongside 8GiB SB) to remain feasible.
    memory_target_max_mb: int = 10_500
    rich_sb_mb: int = 8_192
    protected_sb_mb: int = 4_096
    rich_work_mem_mb: int = 1_150
    protected_work_mem_mb: int = 256
    low_tp_rate: int = 700
    saturated_tp_rate: int = 4_000
    surge_increment_tps: int = 300
    s1_ap_cap: int = 1
    s2_ap_cap: int = 2
    protected_ap_cap: int = 4
    surge_ap_cap: int = 2


@dataclass(frozen=True)
class Observation:
    """One online observation, all values available before a new action."""

    current_sb_mb: int
    running_ap_clients: int
    incoming_ap_clients: int
    predicted_dynamic_demand_mb: float
    offered_tp_tps: int
    protected_tp_tps: int

    @property
    def requested_ap_clients(self) -> int:
        return self.running_ap_clients + self.incoming_ap_clients


class StatisticalPptStateMachine:
    """Finite-state controller driven by capacity and arrival observations."""

    def __init__(self, policy: Policy = Policy()) -> None:
        self.policy = policy
        self.state = "memory_rich"
        self.reference_tp_tps: int | None = None

    def _result(
        self, observation: Observation, state: str, sb_mb: int, work_mem_mb: int,
        ap_cap: int, block_new_ap: bool, reason: str,
    ) -> dict[str, object]:
        return {
            "controller_state": state,
            "shared_buffers_mb": sb_mb,
            "work_mem_mb": work_mem_mb,
            "ap_cap": ap_cap,
            "block_new_ap": block_new_ap,
            "input_current_sb_mb": observation.current_sb_mb,
            "input_running_ap_clients": observation.running_ap_clients,
            "input_incoming_ap_clients": observation.incoming_ap_clients,
            "input_predicted_dynamic_demand_mb": observation.predicted_dynamic_demand_mb,
            "input_offered_tp_tps": observation.offered_tp_tps,
            "input_protected_tp_tps": observation.protected_tp_tps,
            "projected_managed_memory_mb": round(sb_mb + observation.predicted_dynamic_demand_mb, 3),
            "memory_target_max_mb": self.policy.memory_target_max_mb,
            "decision_uses_actual_mixed_tps": False,
            "reason": reason,
            "deployment": "restart_required_when_shared_buffers_changes",
        }

    def decide(self, observation: Observation) -> dict[str, object]:
        p = self.policy
        if observation.offered_tp_tps <= 0 or observation.protected_tp_tps <= 0:
            raise ValueError("TP demand rates must be positive")
        if observation.predicted_dynamic_demand_mb < 0:
            raise ValueError("predicted dynamic demand must be non-negative")
        if self.reference_tp_tps is None:
            self.reference_tp_tps = observation.protected_tp_tps

        # S5 is an immediate reversal.  It is the extra TP stream above the
        # already protected capacity stream, not S3's first transition from
        # low TP to saturated TP.
        if observation.offered_tp_tps >= observation.protected_tp_tps + p.surge_increment_tps:
            self.state = "tp_surge"
            return self._result(
                observation, "tp_surge", p.rich_sb_mb, p.protected_work_mem_mb,
                p.surge_ap_cap, True,
                "TP offered load rose above the protected baseline; restore SB and restrict AP admission",
            )

        demand_at_current_sb = observation.current_sb_mb + observation.predicted_dynamic_demand_mb
        demand_at_protected_sb = p.protected_sb_mb + observation.predicted_dynamic_demand_mb
        ap_growing = observation.incoming_ap_clients > 0
        tp_saturated = observation.protected_tp_tps >= p.saturated_tp_rate

        # S2: high SB cannot retain the requested AP dynamic demand under the
        # configured unified memory target, while its protected floor can.
        if (self.state == "memory_rich" and ap_growing
                and demand_at_current_sb > p.memory_target_max_mb
                and demand_at_protected_sb <= p.memory_target_max_mb):
            self.state = "shared_buffer_yield"
            return self._result(
                observation, self.state, p.protected_sb_mb, p.rich_work_mem_mb,
                p.s2_ap_cap, False,
                "projected SB plus AP demand exceeds the unified target; yield SB before reducing AP grants",
            )

        # S4: after grant reduction, another AP arrival would breach the
        # protected admission cap.  Existing queries are retained.
        if self.state == "protect_tp" and ap_growing and observation.running_ap_clients >= p.protected_ap_cap:
            self.state = "backpressure"
            return self._result(
                observation, self.state, p.protected_sb_mb, p.protected_work_mem_mb,
                p.protected_ap_cap, True,
                "protected AP cap is occupied and AP demand continues; queue new AP without cancelling existing work",
            )

        # S3: once TP reaches its independently calibrated capacity, do not
        # yield any more SB; reduce grants for future AP work instead.
        if self.state in {"memory_rich", "shared_buffer_yield", "protect_tp"} and tp_saturated and ap_growing:
            self.state = "protect_tp"
            return self._result(
                observation, self.state, p.protected_sb_mb, p.protected_work_mem_mb,
                p.protected_ap_cap, False,
                "TP reached calibrated saturation while AP continues to arrive; hold SB and lower future AP grants",
            )

        if self.state == "backpressure":
            return self._result(
                observation, self.state, p.protected_sb_mb, p.protected_work_mem_mb,
                p.protected_ap_cap, True,
                "backpressure remains active until AP demand drains or a TP surge reverses the policy",
            )

        return self._result(
            observation, "memory_rich", p.rich_sb_mb, p.rich_work_mem_mb,
            p.s1_ap_cap, False,
            "AP demand fits the unified memory target; keep the rich AP grant",
        )
