#!/usr/bin/env python3
"""Safe online search for AP CPU and I/O quotas under a TP SLO."""

from __future__ import annotations

from dataclasses import dataclass


MIB = 1024 * 1024


@dataclass(frozen=True)
class ApResourcePolicy:
    cpu_levels: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0)
    io_levels_mib: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0, 80.0, 160.0)
    initial_cpu_cores: float | None = None
    initial_io_mib: float | None = None
    tp_floor_ratio: float = 0.95
    tp_probe_ratio: float = 0.98
    healthy_windows_before_probe: int = 2
    saturation_ratio: float = 0.65
    io_wait_ratio: float = 0.40
    probe_evaluation_windows: int = 6
    minimum_probe_gain: float = 0.10
    floor_violation_windows_before_freeze: int = 2
    freeze_min_evaluation_windows: int = 2
    freeze_evaluation_windows: int = 4
    frozen_healthy_windows_before_resume: int = 2
    confirmed_freeze_max_windows: int = 8

    def __post_init__(self) -> None:
        if not self.cpu_levels or not self.io_levels_mib:
            raise ValueError("resource levels cannot be empty")
        if tuple(sorted(set(self.cpu_levels))) != self.cpu_levels:
            raise ValueError("cpu levels must be unique and ascending")
        if tuple(sorted(set(self.io_levels_mib))) != self.io_levels_mib:
            raise ValueError("I/O levels must be unique and ascending")
        if (
            self.initial_cpu_cores is not None
            and self.initial_cpu_cores not in self.cpu_levels
        ):
            raise ValueError("initial CPU quota must be present in cpu_levels")
        if (
            self.initial_io_mib is not None
            and self.initial_io_mib not in self.io_levels_mib
        ):
            raise ValueError("initial I/O quota must be present in io_levels_mib")


@dataclass(frozen=True)
class ApResourceObservation:
    stage: str
    epoch: str
    tp_retention_ratio: float
    running_ap_queries: int
    window_seconds: float
    ap_cpu_seconds: float
    ap_read_mb: float
    ap_write_mb: float
    io_wait_samples: int
    total_wait_samples: int
    external_memory_control_changed: bool = False


@dataclass(frozen=True)
class ApResourceDecision:
    stage: str
    epoch: str
    action: str
    reason: str
    cpu_quota_cores: float
    read_bps: int
    write_bps: int
    cpu_utilization: float
    io_utilization: float
    io_wait_ratio: float
    ap_cpu_cores_used: float
    ap_io_mib_per_second: float
    tp_retention_ratio: float
    safe_for_tp: bool
    ap_frozen: bool


class ApResourceController:
    """Probe upward while TP is healthy; roll back without killing AP SQL."""

    def __init__(self, policy: ApResourcePolicy | None = None) -> None:
        self.policy = policy or ApResourcePolicy()
        self.initial_cpu_index = self.policy.cpu_levels.index(
            self.policy.initial_cpu_cores
            if self.policy.initial_cpu_cores is not None
            else self.policy.cpu_levels[0]
        )
        self.initial_io_index = self.policy.io_levels_mib.index(
            self.policy.initial_io_mib
            if self.policy.initial_io_mib is not None
            else self.policy.io_levels_mib[0]
        )
        self.stage = ""
        self.cpu_index = self.initial_cpu_index
        self.io_index = self.initial_io_index
        self.healthy_windows = 0
        self.last_probe = ""
        self.probe_previous_index = 0
        self.probe_baseline_rate = 0.0
        self.probe_rates: list[float] = []
        self.mitigation_dimension = ""
        self.mitigation_previous_index = 0
        self.mitigation_retentions: list[float] = []
        self.failed_mitigation_dimensions: set[str] = set()
        self.probe_cooldown_windows = 0
        self.floor_violation_windows = 0
        self.ap_frozen = False
        self.freeze_causal: bool | None = None
        self.freeze_retentions: list[float] = []
        self.frozen_healthy_windows = 0
        self.frozen_windows = 0
        self.freeze_disabled_for_running_query = False
        self.freeze_mitigation_dimension = ""
        self.ineffective_freezes = 0
        self.stage_ap_causal_confirmed = False
        self.cpu_ceiling_index = len(self.policy.cpu_levels) - 1
        self.io_ceiling_index = len(self.policy.io_levels_mib) - 1
        self.last_confirmed_cpu_index = self.initial_cpu_index
        self.last_confirmed_io_index = self.initial_io_index
        self.stage_decisions: list[ApResourceDecision] = []

    @property
    def cpu_quota_cores(self) -> float:
        return self.policy.cpu_levels[self.cpu_index]

    @property
    def io_quota_mib(self) -> float:
        return self.policy.io_levels_mib[self.io_index]

    def enter_stage(self, stage: str) -> ApResourceDecision:
        self.stage = stage
        self.cpu_index = self.initial_cpu_index
        self.io_index = self.initial_io_index
        self.healthy_windows = 0
        self.last_probe = ""
        self.probe_previous_index = 0
        self.probe_baseline_rate = 0.0
        self.probe_rates = []
        self.mitigation_dimension = ""
        self.mitigation_previous_index = 0
        self.mitigation_retentions = []
        self.failed_mitigation_dimensions = set()
        self.probe_cooldown_windows = 0
        self.floor_violation_windows = 0
        self.ap_frozen = False
        self.freeze_causal = None
        self.freeze_retentions = []
        self.frozen_healthy_windows = 0
        self.frozen_windows = 0
        self.freeze_disabled_for_running_query = False
        self.freeze_mitigation_dimension = ""
        self.ineffective_freezes = 0
        self.stage_ap_causal_confirmed = False
        self.cpu_ceiling_index = len(self.policy.cpu_levels) - 1
        self.io_ceiling_index = len(self.policy.io_levels_mib) - 1
        self.last_confirmed_cpu_index = self.initial_cpu_index
        self.last_confirmed_io_index = self.initial_io_index
        self.stage_decisions = []
        return self._decision(
            epoch=f"{stage}_resource_start",
            action="reset_to_probe_floor",
            reason="start each stage from the minimum bounded AP resource probe",
            retention=1.0,
            cpu_used=0.0,
            io_rate=0.0,
            io_wait=0.0,
        )

    def _decision(
        self,
        *,
        epoch: str,
        action: str,
        reason: str,
        retention: float,
        cpu_used: float,
        io_rate: float,
        io_wait: float,
    ) -> ApResourceDecision:
        cpu_utilization = cpu_used / max(self.cpu_quota_cores, 1e-9)
        io_utilization = io_rate / max(self.io_quota_mib, 1e-9)
        decision = ApResourceDecision(
            stage=self.stage,
            epoch=epoch,
            action=action,
            reason=reason,
            cpu_quota_cores=self.cpu_quota_cores,
            read_bps=round(self.io_quota_mib * MIB),
            write_bps=round(self.io_quota_mib * MIB),
            cpu_utilization=round(cpu_utilization, 6),
            io_utilization=round(io_utilization, 6),
            io_wait_ratio=round(io_wait, 6),
            ap_cpu_cores_used=round(cpu_used, 6),
            ap_io_mib_per_second=round(io_rate, 6),
            tp_retention_ratio=round(retention, 6),
            safe_for_tp=retention >= self.policy.tp_floor_ratio,
            ap_frozen=self.ap_frozen,
        )
        self.stage_decisions.append(decision)
        return decision

    def step(self, observation: ApResourceObservation) -> ApResourceDecision:
        if observation.stage != self.stage:
            raise ValueError(
                f"resource controller is in {self.stage!r}, got {observation.stage!r}"
            )
        window = max(observation.window_seconds, 1e-9)
        cpu_used = observation.ap_cpu_seconds / window
        io_rate = (observation.ap_read_mb + observation.ap_write_mb) / window
        io_wait = (
            observation.io_wait_samples / observation.total_wait_samples
            if observation.total_wait_samples
            else 0.0
        )
        retention = observation.tp_retention_ratio

        if observation.running_ap_queries <= 0:
            self.healthy_windows = 0
            self.ap_frozen = False
            return self._decision(
                epoch=observation.epoch,
                action="hold_no_running_ap",
                reason="there is no running AP query to probe",
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        if observation.external_memory_control_changed:
            # Do not learn an AP quota boundary from a window in which SB also
            # changed. Serialize the actuators so the next response has a
            # defensible cause.
            self.healthy_windows = 0
            self.last_probe = ""
            self.probe_rates = []
            self.mitigation_dimension = ""
            self.mitigation_retentions = []
            self.floor_violation_windows = 0
            self.probe_cooldown_windows = 2
            self.ap_frozen = False
            self.freeze_causal = None
            self.freeze_retentions = []
            self.frozen_windows = 0
            self.freeze_mitigation_dimension = ""
            return self._decision(
                epoch=observation.epoch,
                action="hold_during_external_memory_transition",
                reason=(
                    "shared_buffers changed in this window; hold the AP quota "
                    "so memory and resource effects are not confounded"
                ),
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        if self.ap_frozen:
            self.frozen_windows += 1
            if self.freeze_causal is None:
                self.freeze_retentions.append(retention)
                recent = self.freeze_retentions[
                    -self.policy.freeze_min_evaluation_windows :
                ]
                recovered = (
                    len(recent) >= self.policy.freeze_min_evaluation_windows
                    and sum(recent) / len(recent) >= self.policy.tp_probe_ratio
                )
                if (
                    not recovered
                    and len(self.freeze_retentions)
                    < self.policy.freeze_evaluation_windows
                ):
                    return self._decision(
                        epoch=observation.epoch,
                        action="evaluate_frozen_ap_for_tp",
                        reason="measure whether pausing AP restores TP",
                        retention=retention,
                        cpu_used=cpu_used,
                        io_rate=io_rate,
                        io_wait=io_wait,
                    )
                evaluated = recent if recovered else self.freeze_retentions
                mean_retention = sum(evaluated) / len(evaluated)
                self.freeze_retentions = []
                if mean_retention < self.policy.tp_probe_ratio:
                    if self.stage_ap_causal_confirmed:
                        self.freeze_causal = True
                        self.frozen_healthy_windows = 0
                        return self._decision(
                            epoch=observation.epoch,
                            action="hold_confirmed_causal_ap_frozen",
                            reason=(
                                "AP previously caused TP loss in this stage; keep the "
                                "same SQL paused until TP recovers"
                            ),
                            retention=retention,
                            cpu_used=cpu_used,
                            io_rate=io_rate,
                            io_wait=io_wait,
                        )
                    self.ap_frozen = False
                    self.freeze_causal = False
                    self.freeze_mitigation_dimension = ""
                    self.probe_cooldown_windows = 2
                    return self._decision(
                        epoch=observation.epoch,
                        action="resume_ap_after_failed_freeze_test",
                        reason=(
                            f"TP stayed at {100.0 * mean_retention:.1f}% while "
                            "AP was frozen, so AP was not the causal disturbance"
                        ),
                        retention=retention,
                        cpu_used=cpu_used,
                        io_rate=io_rate,
                        io_wait=io_wait,
                    )
                self.freeze_causal = True
                self.stage_ap_causal_confirmed = True
                self.frozen_healthy_windows = 0
                return self._decision(
                    epoch=observation.epoch,
                    action="accept_ap_freeze_for_tp",
                    reason=(
                        f"TP recovered to {100.0 * mean_retention:.1f}% while AP "
                        "was paused"
                    ),
                    retention=retention,
                    cpu_used=cpu_used,
                    io_rate=io_rate,
                    io_wait=io_wait,
                )

            if retention >= self.policy.tp_probe_ratio:
                self.frozen_healthy_windows += 1
            else:
                self.frozen_healthy_windows = 0
            if (
                self.frozen_healthy_windows
                >= self.policy.frozen_healthy_windows_before_resume
            ):
                self.ap_frozen = False
                self.freeze_causal = None
                self.frozen_healthy_windows = 0
                self.frozen_windows = 0
                self.floor_violation_windows = 0
                resume_dimension = self.freeze_mitigation_dimension
                self.freeze_mitigation_dimension = ""
                if resume_dimension == "io" and self.io_index > 0:
                    self.io_index -= 1
                    self.io_ceiling_index = min(
                        self.io_ceiling_index, self.io_index
                    )
                    action = "resume_ap_at_lower_io_after_tp_recovery"
                    reason = (
                        "TP recovered while AP was frozen; resume the same SQL "
                        "at the next lower I/O level"
                    )
                elif resume_dimension == "cpu" and self.cpu_index > 0:
                    self.cpu_index -= 1
                    self.cpu_ceiling_index = min(
                        self.cpu_ceiling_index, self.cpu_index
                    )
                    action = "resume_ap_at_lower_cpu_after_tp_recovery"
                    reason = (
                        "TP recovered while AP was frozen; resume the same SQL "
                        "at the next lower CPU level"
                    )
                else:
                    action = "resume_ap_after_tp_recovery"
                    reason = (
                        "TP was healthy for two frozen windows; resume the same SQL"
                    )
                return self._decision(
                    epoch=observation.epoch,
                    action=action,
                    reason=reason,
                    retention=retention,
                    cpu_used=cpu_used,
                    io_rate=io_rate,
                    io_wait=io_wait,
                )
            if self.frozen_windows >= self.policy.confirmed_freeze_max_windows:
                self.ap_frozen = False
                self.freeze_causal = False
                self.frozen_healthy_windows = 0
                self.frozen_windows = 0
                self.freeze_disabled_for_running_query = True
                self.freeze_mitigation_dimension = ""
                self.ineffective_freezes += 1
                return self._decision(
                    epoch=observation.epoch,
                    action="resume_ap_after_ineffective_confirmed_freeze",
                    reason=(
                        "freezing no longer restores TP; resume the same SQL so "
                        "retained resources can be released by natural completion"
                    ),
                    retention=retention,
                    cpu_used=cpu_used,
                    io_rate=io_rate,
                    io_wait=io_wait,
                )
            return self._decision(
                epoch=observation.epoch,
                action="hold_ap_frozen_for_tp",
                reason="keep the same AP SQL paused until TP recovery is stable",
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        if (
            retention < self.policy.tp_probe_ratio
            and self.stage_ap_causal_confirmed
            and not self.freeze_disabled_for_running_query
            and not self.last_probe
        ):
            self.mitigation_dimension = ""
            self.mitigation_retentions = []
            self.probe_cooldown_windows = 0
            self.floor_violation_windows = 0
            self.ap_frozen = True
            self.freeze_causal = True
            self.freeze_retentions = []
            self.frozen_healthy_windows = 0
            self.frozen_windows = 0
            self.freeze_mitigation_dimension = (
                "io"
                if self.io_index > 0
                and (
                    io_wait >= self.policy.io_wait_ratio
                    or self.cpu_index == 0
                )
                else "cpu" if self.cpu_index > 0 else ""
            )
            return self._decision(
                epoch=observation.epoch,
                action="freeze_ap_for_confirmed_tp_protection",
                reason=(
                    "AP causality was already confirmed in this stage and TP entered "
                    "the 98% protection guard band"
                ),
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        if self.mitigation_dimension:
            self.mitigation_retentions.append(retention)
            if len(self.mitigation_retentions) < 2:
                return self._decision(
                    epoch=observation.epoch,
                    action=f"evaluate_lower_{self.mitigation_dimension}_for_tp",
                    reason="measure whether the temporary quota reduction restores TP",
                    retention=retention,
                    cpu_used=cpu_used,
                    io_rate=io_rate,
                    io_wait=io_wait,
                )
            mean_retention = sum(self.mitigation_retentions) / len(
                self.mitigation_retentions
            )
            dimension = self.mitigation_dimension
            self.mitigation_dimension = ""
            self.mitigation_retentions = []
            self.healthy_windows = 0
            if mean_retention >= self.policy.tp_probe_ratio:
                self.failed_mitigation_dimensions.discard(dimension)
                # The immediately higher level was active when TP crossed the
                # floor, and lowering this dimension restored TP. Keep that
                # measured unsafe level out of subsequent probes in this stage.
                if dimension == "io":
                    self.io_ceiling_index = min(
                        self.io_ceiling_index, self.io_index
                    )
                else:
                    self.cpu_ceiling_index = min(
                        self.cpu_ceiling_index, self.cpu_index
                    )
                return self._decision(
                    epoch=observation.epoch,
                    action=f"accept_lower_{dimension}_for_tp",
                    reason=(
                        f"TP recovered to {100.0 * mean_retention:.1f}% after "
                        f"the temporary {dimension} reduction"
                    ),
                    retention=retention,
                    cpu_used=cpu_used,
                    io_rate=io_rate,
                    io_wait=io_wait,
                )
            if dimension == "io":
                self.io_index = self.mitigation_previous_index
            else:
                self.cpu_index = self.mitigation_previous_index
            self.failed_mitigation_dimensions.add(dimension)
            self.probe_cooldown_windows = 2
            return self._decision(
                epoch=observation.epoch,
                action=f"restore_{dimension}_after_failed_tp_mitigation",
                reason=(
                    f"TP stayed at {100.0 * mean_retention:.1f}%; the AP quota "
                    "was not the causal disturbance"
                ),
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        if self.probe_cooldown_windows > 0:
            self.probe_cooldown_windows -= 1
            return self._decision(
                epoch=observation.epoch,
                action="hold_after_failed_tp_mitigation",
                reason="avoid another AP resource probe during an external TP disturbance",
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        if retention < self.policy.tp_floor_ratio:
            self.healthy_windows = 0
            if self.last_probe == "io" and self.io_index > 0:
                self.io_ceiling_index = min(self.io_ceiling_index, self.io_index - 1)
                self.io_index -= 1
                action = "rollback_io_for_tp"
            elif self.last_probe == "cpu" and self.cpu_index > 0:
                self.cpu_ceiling_index = min(
                    self.cpu_ceiling_index, self.cpu_index - 1
                )
                self.cpu_index -= 1
                action = "rollback_cpu_for_tp"
            elif self.io_index > 0 and "io" not in self.failed_mitigation_dimensions and (
                io_wait >= self.policy.io_wait_ratio or self.cpu_index == 0
            ):
                self.mitigation_dimension = "io"
                self.mitigation_previous_index = self.io_index
                self.mitigation_retentions = []
                self.io_index -= 1
                action = "probe_lower_io_for_tp"
            elif self.cpu_index > 0 and "cpu" not in self.failed_mitigation_dimensions:
                self.mitigation_dimension = "cpu"
                self.mitigation_previous_index = self.cpu_index
                self.mitigation_retentions = []
                self.cpu_index -= 1
                action = "probe_lower_cpu_for_tp"
            else:
                self.floor_violation_windows += 1
                if self.freeze_disabled_for_running_query:
                    action = "hold_ineffective_freeze_allow_ap_completion"
                elif self.freeze_causal is False:
                    action = "hold_external_tp_disturbance"
                elif (
                    self.floor_violation_windows
                    >= self.policy.floor_violation_windows_before_freeze
                ):
                    self.ap_frozen = True
                    self.freeze_causal = None
                    self.freeze_retentions = []
                    self.frozen_windows = 0
                    self.floor_violation_windows = 0
                    self.freeze_mitigation_dimension = (
                        "io"
                        if self.io_index > 0
                        and (
                            io_wait >= self.policy.io_wait_ratio
                            or self.cpu_index == 0
                        )
                        else "cpu" if self.cpu_index > 0 else ""
                    )
                    action = "freeze_ap_for_tp_causal_test"
                else:
                    action = "hold_probe_floor_for_tp"
            if self.last_probe:
                self.last_probe = ""
                self.probe_rates = []
            return self._decision(
                epoch=observation.epoch,
                action=action,
                reason="TP retention crossed the 95% safety floor",
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        if retention < self.policy.tp_probe_ratio:
            self.healthy_windows = 0
            return self._decision(
                epoch=observation.epoch,
                action="hold_tp_guard_band",
                reason="TP is safe but lacks headroom for a new resource probe",
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        self.floor_violation_windows = 0
        self.failed_mitigation_dimensions = set()
        self.freeze_disabled_for_running_query = False
        if self.freeze_causal is False:
            self.freeze_causal = None

        cpu_util = cpu_used / max(self.cpu_quota_cores, 1e-9)
        io_util = io_rate / max(self.io_quota_mib, 1e-9)

        if self.last_probe:
            measured_rate = io_rate if self.last_probe == "io" else cpu_used
            self.probe_rates.append(measured_rate)
            if len(self.probe_rates) < self.policy.probe_evaluation_windows:
                return self._decision(
                    epoch=observation.epoch,
                    action=f"evaluate_{self.last_probe}_probe",
                    reason="collect post-probe AP progress before accepting the quota",
                    retention=retention,
                    cpu_used=cpu_used,
                    io_rate=io_rate,
                    io_wait=io_wait,
                )
            observed_rate = sum(self.probe_rates) / len(self.probe_rates)
            gain = (
                observed_rate / self.probe_baseline_rate - 1.0
                if self.probe_baseline_rate > 0
                else float("inf")
            )
            probe = self.last_probe
            self.last_probe = ""
            self.probe_rates = []
            self.healthy_windows = 0
            if gain < self.policy.minimum_probe_gain:
                if probe == "io":
                    self.io_ceiling_index = min(
                        self.io_ceiling_index, self.io_index - 1
                    )
                    self.io_index = self.probe_previous_index
                else:
                    self.cpu_ceiling_index = min(
                        self.cpu_ceiling_index, self.cpu_index - 1
                    )
                    self.cpu_index = self.probe_previous_index
                return self._decision(
                    epoch=observation.epoch,
                    action=f"rollback_{probe}_probe_no_gain",
                    reason=(
                        f"higher {probe} quota improved AP progress by only "
                        f"{100.0 * gain:.1f}%"
                    ),
                    retention=retention,
                    cpu_used=cpu_used,
                    io_rate=io_rate,
                    io_wait=io_wait,
                )
            self.last_confirmed_cpu_index = self.cpu_index
            self.last_confirmed_io_index = self.io_index
            return self._decision(
                epoch=observation.epoch,
                action=f"accept_{probe}_probe_gain",
                reason=(
                    f"higher {probe} quota improved AP progress by "
                    f"{100.0 * gain:.1f}% without violating TP"
                ),
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        self.healthy_windows += 1
        if self.healthy_windows >= self.policy.healthy_windows_before_probe:
            self.last_confirmed_cpu_index = self.cpu_index
            self.last_confirmed_io_index = self.io_index

        if self.healthy_windows < self.policy.healthy_windows_before_probe:
            return self._decision(
                epoch=observation.epoch,
                action="confirm_current_quota",
                reason="wait for a second healthy TP window before probing upward",
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        can_probe_io = self.io_index < self.io_ceiling_index
        can_probe_cpu = self.cpu_index < self.cpu_ceiling_index
        io_bound = io_wait >= self.policy.io_wait_ratio
        io_saturated = io_bound and io_util >= self.policy.saturation_ratio
        cpu_saturated = cpu_util >= self.policy.saturation_ratio

        if can_probe_io and io_saturated and (
            not cpu_saturated or io_util >= cpu_util
        ):
            self.probe_previous_index = self.io_index
            self.probe_baseline_rate = io_rate
            self.probe_rates = []
            self.io_index += 1
            self.healthy_windows = 0
            self.last_probe = "io"
            return self._decision(
                epoch=observation.epoch,
                action="probe_higher_io_quota",
                reason="AP is I/O-waiting and consumes most of the current I/O quota",
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        if can_probe_cpu and cpu_saturated:
            self.probe_previous_index = self.cpu_index
            self.probe_baseline_rate = cpu_used
            self.probe_rates = []
            self.cpu_index += 1
            self.healthy_windows = 0
            self.last_probe = "cpu"
            return self._decision(
                epoch=observation.epoch,
                action="probe_higher_cpu_quota",
                reason="AP consumes most of the current CPU quota while TP is healthy",
                retention=retention,
                cpu_used=cpu_used,
                io_rate=io_rate,
                io_wait=io_wait,
            )

        return self._decision(
            epoch=observation.epoch,
            action="hold_measured_resource_ceiling",
            reason="neither CPU nor I/O quota is measurably constraining AP",
            retention=retention,
            cpu_used=cpu_used,
            io_rate=io_rate,
            io_wait=io_wait,
        )

    def recommendation(self) -> dict[str, object]:
        measured_windows = [
            decision
            for decision in self.stage_decisions
            if not decision.epoch.endswith("_resource_start")
        ]
        frozen_windows = sum(decision.ap_frozen for decision in measured_windows)
        return {
            "stage": self.stage,
            "recommended_active_cpu_quota_cores": self.cpu_quota_cores,
            "recommended_active_io_mib_per_second": self.io_quota_mib,
            "recommended_cpu_quota_cores": self.cpu_quota_cores,
            "recommended_read_bps": round(self.io_quota_mib * MIB),
            "recommended_write_bps": round(self.io_quota_mib * MIB),
            "recommended_io_mib_per_second": self.io_quota_mib,
            "cpu_search_ceiling_cores": self.policy.cpu_levels[
                self.cpu_ceiling_index
            ],
            "io_search_ceiling_mib_per_second": self.policy.io_levels_mib[
                self.io_ceiling_index
            ],
            "decisions": len(self.stage_decisions),
            "freeze_control_windows": frozen_windows,
            "freeze_duty_cycle": round(
                frozen_windows / max(len(measured_windows), 1), 6
            ),
            "requires_dynamic_pause_control": frozen_windows > 0,
            "ap_interference_causally_confirmed": self.stage_ap_causal_confirmed,
            "ineffective_freezes": self.ineffective_freezes,
            "method": "online_safe_probe_without_tps_training_or_query_cancellation",
        }
