#!/usr/bin/env python3
"""TP-SLO-first runtime memory controller.

TPS is consumed only as live feedback.  It is not used to fit a performance
model or to choose an offline optimum.  Cache/operator replay supplies the
safe SB and AP-grant candidates; the controller prioritizes TP stability and
degrades AP service when the observed TP retention ratio violates the SLO.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from runtime_memory_controller_replay import (  # noqa: E402
    STAGE_ORDER,
    StageTarget,
    load_targets,
    read_csv,
    write_csv,
)


@dataclass(frozen=True)
class GrantProfile:
    assignments: str
    dynamic_peak_mb: float
    spill_io_mb: float
    spilling_operators: int


@dataclass(frozen=True)
class TpSloPolicy:
    floor_ratio: float = 0.95
    recovery_ratio: float = 0.98
    severe_ratio: float = 0.90
    violation_ticks_before_pause: int = 2
    recovery_ticks_before_restore: int = 3
    grant_reclaim_mb_per_tick: float = 1024.0
    max_spill_io_mb: float = 32768.0
    max_spill_amplification: float = 1.35
    granule_mb: int = 256
    high_tp_sb_target_mb: int = 8192
    sb_resize_enabled: bool = True
    sb_shrink_enabled: bool = True
    cancel_running_ap_on_severe: bool = False
    initial_probe_ap_clients: int | None = None
    ap_max_wait_seconds: float | None = None
    ap_admission_peak_safety: float = 1.35


@dataclass(frozen=True)
class Observation:
    epoch: str
    stage: str
    tp_tps: float
    tp_reference_tps: float
    requested_ap_clients: int
    tp_high: bool = False
    # Real Query-boundary execution can report the sum of replay-predicted
    # peaks for sessions that are still alive.  None keeps the deterministic
    # open-loop debt model used by offline diagnostics.
    observed_dynamic_mb: float | None = None
    running_ap_clients: int | None = None
    oldest_ap_wait_seconds: float | None = None


def load_grant_profiles(path: Path) -> dict[str, list[GrantProfile]]:
    profiles = {stage: [] for stage in STAGE_ORDER}
    for row in read_csv(path):
        if row.get("allocation_mode") not in (None, "", "global_session_setting"):
            continue
        stage = row["stage"]
        if stage not in profiles:
            continue
        profiles[stage].append(
            GrantProfile(
                assignments=row["query_work_mem_assignments"],
                dynamic_peak_mb=float(row["dynamic_peak_mb"]),
                spill_io_mb=float(row["spill_io_mb"]),
                spilling_operators=int(row["spilling_operators"]),
            )
        )
    return profiles


def load_observations(path: Path) -> list[Observation]:
    rows = []
    for row in read_csv(path):
        rows.append(
            Observation(
                epoch=row["epoch"],
                stage=row["stage"],
                tp_tps=float(row["tp_tps"]),
                tp_reference_tps=float(row["tp_reference_tps"]),
                requested_ap_clients=int(row["requested_ap_clients"]),
                tp_high=str(row.get("tp_high", "")).lower() in ("1", "true", "yes"),
                observed_dynamic_mb=(
                    float(row["observed_dynamic_mb"])
                    if row.get("observed_dynamic_mb") not in (None, "")
                    else None
                ),
                running_ap_clients=(
                    int(row["running_ap_clients"])
                    if row.get("running_ap_clients") not in (None, "")
                    else None
                ),
            )
        )
    return rows


class TpSloController:
    """Deterministic SLO controller with graceful AP-memory debt."""

    def __init__(
        self,
        targets: dict[str, StageTarget],
        profiles: dict[str, list[GrantProfile]],
        memory_target_max_mb: float,
        initial_sb_mb: int,
        policy: TpSloPolicy | None = None,
    ) -> None:
        self.targets = targets
        self.profiles = profiles
        self.memory_target_max_mb = memory_target_max_mb
        self.current_sb_mb = initial_sb_mb
        self.policy = policy or TpSloPolicy()

        self.current_stage: str | None = None
        self.current_profile: GrantProfile | None = None
        self.actual_dynamic_mb = 0.0
        self.target_dynamic_mb = 0.0
        self.admitted_ap_clients = 0
        self.violation_streak = 0
        self.recovery_streak = 0
        self.protective_state = False
        self.stage_had_running_ap = False

    def _preferred_profile(self, target: StageTarget) -> GrantProfile:
        return GrantProfile(
            assignments=target.work_mem_assignments,
            dynamic_peak_mb=target.dynamic_peak_mb,
            spill_io_mb=target.spill_io_mb,
            spilling_operators=target.spilling_operators,
        )

    def _profile_scale(self, target: StageTarget, admitted: int) -> float:
        return admitted / max(1, target.base_clients)

    def _dynamic_for(
        self, target: StageTarget, profile: GrantProfile, admitted: int
    ) -> float:
        return profile.dynamic_peak_mb * self._profile_scale(target, admitted)

    def _spill_for(
        self, target: StageTarget, profile: GrantProfile, admitted: int
    ) -> float:
        return profile.spill_io_mb * self._profile_scale(target, admitted)

    def _settle_graceful_debt(self) -> float:
        old = self.actual_dynamic_mb
        if self.actual_dynamic_mb > self.target_dynamic_mb:
            self.actual_dynamic_mb = max(
                self.target_dynamic_mb,
                self.actual_dynamic_mb - self.policy.grant_reclaim_mb_per_tick,
            )
        elif self.actual_dynamic_mb < self.target_dynamic_mb:
            available = max(
                0.0,
                self.memory_target_max_mb - self.current_sb_mb - self.actual_dynamic_mb,
            )
            self.actual_dynamic_mb += min(
                self.target_dynamic_mb - self.actual_dynamic_mb,
                self.policy.grant_reclaim_mb_per_tick,
                available,
            )
        return abs(old - self.actual_dynamic_mb)

    def _enter_stage(self, observation: Observation) -> None:
        target = self.targets[observation.stage]
        preferred = self._preferred_profile(target)
        previous_actual_dynamic_mb = self.actual_dynamic_mb
        had_previous_stage = self.current_stage is not None
        available = max(0.0, self.memory_target_max_mb - self.current_sb_mb)
        per_client = preferred.dynamic_peak_mb / max(1, target.base_clients)
        slots = observation.requested_ap_clients
        if per_client > 0:
            slots = int(available // per_client)
        self.admitted_ap_clients = min(
            observation.requested_ap_clients, max(0, slots)
        )
        if self.policy.initial_probe_ap_clients is not None:
            self.admitted_ap_clients = min(
                self.admitted_ap_clients,
                max(0, self.policy.initial_probe_ap_clients),
            )
        self.current_profile = preferred
        self.target_dynamic_mb = self._dynamic_for(
            target, preferred, self.admitted_ap_clients
        )
        # A stage boundary changes future grants, not memory already held by a
        # running operator.  Preserve that allocation as graceful debt.
        self.actual_dynamic_mb = min(
            max(previous_actual_dynamic_mb, self.target_dynamic_mb)
            if had_previous_stage else self.target_dynamic_mb,
            available,
        )
        self.current_stage = observation.stage
        self.violation_streak = 0
        self.recovery_streak = 0
        self.stage_had_running_ap = False

    def _spill_limit(self, target: StageTarget) -> float:
        return max(
            self.policy.max_spill_io_mb,
            target.spill_io_mb * self.policy.max_spill_amplification,
        )

    def _lower_grant_profile(self, target: StageTarget) -> GrantProfile | None:
        assert self.current_profile is not None
        current_dynamic = self.current_profile.dynamic_peak_mb
        candidates = [
            profile
            for profile in self.profiles.get(target.stage, [])
            if profile.dynamic_peak_mb < current_dynamic - 1e-9
            and profile.spill_io_mb <= self._spill_limit(target)
        ]
        if not candidates:
            return None
        # TP is the priority: among profiles that release memory, first minimize
        # spill interference, then retain the larger AP grant.
        return min(candidates, key=lambda p: (p.spill_io_mb, -p.dynamic_peak_mb))

    def _admission_profile(
        self,
        target: StageTarget,
        admitted: int,
        actual_dynamic_mb: float,
        running_ap_clients: int,
    ) -> GrantProfile | None:
        preferred = self._preferred_profile(target)
        candidates = [preferred, *self.profiles.get(target.stage, [])]
        incremental_clients = max(0, admitted - running_ap_clients)
        feasible = [
            profile
            for profile in candidates
            if self.current_sb_mb
            + actual_dynamic_mb
            + (
                profile.dynamic_peak_mb
                / max(1, target.base_clients)
                * incremental_clients
                * self.policy.ap_admission_peak_safety
            )
            <= self.memory_target_max_mb + 1e-9
            and self._spill_for(target, profile, admitted) <= self._spill_limit(target)
        ]
        if not feasible:
            return None
        available = max(1.0, self.memory_target_max_mb - self.current_sb_mb)
        spill_limit = max(1.0, self._spill_limit(target))
        return min(
            feasible,
            key=lambda profile: (
                self._dynamic_for(target, profile, admitted) / available
                + self._spill_for(target, profile, admitted) / spill_limit,
                profile.dynamic_peak_mb,
            ),
        )

    def _raise_sb_after_reclaim(self, target_mb: int) -> bool:
        if not self.policy.sb_resize_enabled:
            return False
        if self.current_sb_mb >= target_mb:
            return False
        proposed = min(target_mb, self.current_sb_mb + self.policy.granule_mb)
        if proposed + self.actual_dynamic_mb > self.memory_target_max_mb + 1e-9:
            return False
        self.current_sb_mb = proposed
        return True

    def _shrink_sb_for_recovery(self, target_mb: int) -> bool:
        if not self.policy.sb_resize_enabled or not self.policy.sb_shrink_enabled:
            return False
        if self.current_sb_mb <= target_mb:
            return False
        self.current_sb_mb = max(target_mb, self.current_sb_mb - self.policy.granule_mb)
        return True

    def step(self, observation: Observation) -> dict[str, object]:
        if observation.stage not in self.targets:
            raise KeyError(f"unknown stage: {observation.stage}")
        if observation.tp_reference_tps <= 0:
            raise ValueError("tp_reference_tps must be positive")
        if self.current_stage != observation.stage:
            self._enter_stage(observation)

        target = self.targets[observation.stage]
        assert self.current_profile is not None
        if observation.running_ap_clients is not None and observation.running_ap_clients > 0:
            self.stage_had_running_ap = True
        if observation.observed_dynamic_mb is None:
            reclaimed_this_tick = self._settle_graceful_debt()
        else:
            old_dynamic_mb = self.actual_dynamic_mb
            self.actual_dynamic_mb = max(0.0, observation.observed_dynamic_mb)
            reclaimed_this_tick = max(0.0, old_dynamic_mb - self.actual_dynamic_mb)
        ratio = observation.tp_tps / observation.tp_reference_tps
        actions: list[str] = []
        block_new_ap = False

        if (
            ratio < self.policy.floor_ratio
            and observation.running_ap_clients == 0
            and not self.stage_had_running_ap
        ):
            # TP-only noise or an external disturbance cannot be repaired by
            # throttling AP that is not running.  Admit one bounded probe so
            # subsequent feedback has a causal AP exposure.
            self.violation_streak = 0
            self.recovery_streak = 0
            self.protective_state = False
            probe_clients = min(1, observation.requested_ap_clients)
            probe_dynamic_mb = self._dynamic_for(
                target, self.current_profile, probe_clients
            )
            if self.current_sb_mb + probe_dynamic_mb <= self.memory_target_max_mb:
                self.admitted_ap_clients = max(self.admitted_ap_clients, probe_clients)
                self.target_dynamic_mb = self._dynamic_for(
                    target, self.current_profile, self.admitted_ap_clients
                )
                actions.append("admit_one_bounded_probe_ap")
            else:
                block_new_ap = True
                actions.append("keep_probe_queued_memory_limit")

        elif ratio < self.policy.floor_ratio and observation.running_ap_clients == 0:
            # AP has already run in this stage and may have left cache/I/O
            # after-effects.  Do not probe again until TP has genuinely
            # recovered through the normal hysteresis path.
            self.violation_streak = 0
            self.recovery_streak = 0
            self.protective_state = True
            self.admitted_ap_clients = 0
            self.target_dynamic_mb = 0.0
            block_new_ap = True
            actions.append("wait_tp_recovery_with_ap_fully_blocked")
            sb_target = max(
                target.sb_mb,
                self.policy.high_tp_sb_target_mb if observation.tp_high else target.sb_mb,
            )
            if self._raise_sb_after_reclaim(sb_target):
                actions.append("raise_sb_with_ap_fully_blocked")

        elif ratio < self.policy.floor_ratio:
            self.protective_state = True
            self.violation_streak += 1
            self.recovery_streak = 0
            block_new_ap = True
            actions.append("block_new_ap")

            lower = (
                self._lower_grant_profile(target)
                if self.admitted_ap_clients > 0 else None
            )
            if lower is not None:
                self.current_profile = lower
                actions.append("lower_running_ap_grant")

            should_pause = (
                ratio < self.policy.severe_ratio
                or self.violation_streak >= self.policy.violation_ticks_before_pause
            )
            if should_pause and self.admitted_ap_clients > 0:
                self.admitted_ap_clients -= 1
                actions.append("pause_one_ap_at_query_boundary")
            running_ap_clients = (
                observation.running_ap_clients
                if observation.running_ap_clients is not None
                else self.admitted_ap_clients
            )
            if (
                self.policy.cancel_running_ap_on_severe
                and should_pause
                and running_ap_clients > 0
            ):
                actions.append("cancel_one_running_ap")

            self.target_dynamic_mb = self._dynamic_for(
                target, self.current_profile, self.admitted_ap_clients
            )
            sb_target = max(
                target.sb_mb,
                self.policy.high_tp_sb_target_mb if observation.tp_high else target.sb_mb,
            )
            if self._raise_sb_after_reclaim(sb_target):
                actions.append("raise_sb_after_grant_reclaimed")

        elif ratio < self.policy.recovery_ratio:
            self.protective_state = True
            self.violation_streak = 0
            self.recovery_streak = 0
            block_new_ap = True
            actions.append("hold_protective_state")

        else:
            self.violation_streak = 0
            self.recovery_streak += 1
            running_has_reached_admitted = (
                observation.running_ap_clients is None
                or observation.running_ap_clients >= self.admitted_ap_clients
            )
            wait_slo_due = (
                self.policy.ap_max_wait_seconds is not None
                and observation.oldest_ap_wait_seconds is not None
                and observation.oldest_ap_wait_seconds
                >= self.policy.ap_max_wait_seconds
                and self.admitted_ap_clients < observation.requested_ap_clients
                and running_has_reached_admitted
            )
            if wait_slo_due:
                desired_admitted = self.admitted_ap_clients + 1
                admission_profile = self._admission_profile(
                    target,
                    desired_admitted,
                    self.actual_dynamic_mb,
                    observation.running_ap_clients or 0,
                )
                if admission_profile is None:
                    block_new_ap = True
                    actions.append("ap_wait_slo_blocked_by_memory_or_spill")
                else:
                    if admission_profile != self.current_profile:
                        actions.append("adjust_ap_grant_for_wait_slo")
                    actions.append("admit_one_for_ap_wait_slo")
                    self.current_profile = admission_profile
                    self.admitted_ap_clients = desired_admitted
                    self.target_dynamic_mb = self._dynamic_for(
                        target, admission_profile, desired_admitted
                    )
                    self.recovery_streak = 0
            elif self.recovery_streak < self.policy.recovery_ticks_before_restore:
                block_new_ap = self.protective_state
                actions.append("wait_recovery_hysteresis")
            else:
                preferred = self._preferred_profile(target)
                if self._shrink_sb_for_recovery(target.sb_mb):
                    actions.append("shrink_sb_one_granule_for_ap_recovery")
                else:
                    desired_admitted = self.admitted_ap_clients
                    if running_has_reached_admitted:
                        desired_admitted = min(
                            observation.requested_ap_clients,
                            self.admitted_ap_clients + 1,
                        )
                    admission_profile = self._admission_profile(
                        target,
                        desired_admitted,
                        self.actual_dynamic_mb,
                        observation.running_ap_clients or 0,
                    )
                    if admission_profile is not None:
                        if desired_admitted > self.admitted_ap_clients:
                            actions.append("admit_one_queued_ap")
                            self.recovery_streak = 0
                        if self.current_profile != admission_profile:
                            actions.append("select_replay_safe_ap_grant")
                        self.admitted_ap_clients = desired_admitted
                        self.current_profile = admission_profile
                        self.target_dynamic_mb = self._dynamic_for(
                            target, admission_profile, desired_admitted
                        )
                    else:
                        actions.append("keep_ap_queued_memory_limit")
                if (
                    self.admitted_ap_clients >= observation.requested_ap_clients
                    and self.current_profile == preferred
                    and self.current_sb_mb <= target.sb_mb
                ):
                    self.protective_state = False

        self.target_dynamic_mb = self._dynamic_for(
            target, self.current_profile, self.admitted_ap_clients
        )
        debt = max(0.0, self.actual_dynamic_mb - self.target_dynamic_mb)
        queued = max(0, observation.requested_ap_clients - self.admitted_ap_clients)
        spill = self._spill_for(target, self.current_profile, self.admitted_ap_clients)
        return {
            "epoch": observation.epoch,
            "stage": observation.stage,
            "tp_high": observation.tp_high,
            "tp_tps": round(observation.tp_tps, 3),
            "tp_reference_tps": round(observation.tp_reference_tps, 3),
            "tp_retention_ratio": round(ratio, 6),
            "tp_slo_floor_ratio": self.policy.floor_ratio,
            "tp_slo_met": ratio >= self.policy.floor_ratio,
            "controller_state": (
                "violation" if ratio < self.policy.floor_ratio
                else "guarded" if ratio < self.policy.recovery_ratio
                else "healthy"
            ),
            "actions": ";".join(actions) if actions else "none",
            "block_new_ap": block_new_ap,
            "requested_ap_clients": observation.requested_ap_clients,
            "admitted_ap_clients": self.admitted_ap_clients,
            "queued_ap_clients": queued,
            "work_mem_assignments": self.current_profile.assignments,
            "actual_dynamic_mb": round(self.actual_dynamic_mb, 3),
            "target_dynamic_mb": round(self.target_dynamic_mb, 3),
            "graceful_debt_mb": round(debt, 3),
            "reclaimed_this_tick_mb": round(reclaimed_this_tick, 3),
            "predicted_spill_io_mb": round(spill, 3),
            "sb_mb": self.current_sb_mb,
            "managed_memory_mb": round(self.current_sb_mb + self.actual_dynamic_mb, 3),
            "memory_target_max_mb": self.memory_target_max_mb,
            "memory_limit_respected": (
                self.current_sb_mb + self.actual_dynamic_mb
                <= self.memory_target_max_mb + 1e-9
            ),
            "violation_streak": self.violation_streak,
            "recovery_streak": self.recovery_streak,
            "oldest_ap_wait_seconds": (
                round(observation.oldest_ap_wait_seconds, 3)
                if observation.oldest_ap_wait_seconds is not None
                else ""
            ),
            "ap_max_wait_seconds": (
                self.policy.ap_max_wait_seconds
                if self.policy.ap_max_wait_seconds is not None
                else ""
            ),
        }


def diagnostic_observations(
    stage_tps_path: Path, reference_tps: float, ticks_per_stage: int
) -> list[Observation]:
    by_stage = {row["stage"]: row for row in read_csv(stage_tps_path)}
    rows = []
    for stage in STAGE_ORDER:
        source = by_stage[stage]
        for tick in range(1, ticks_per_stage + 1):
            rows.append(
                Observation(
                    epoch=f"{stage}_tick{tick}",
                    stage=stage,
                    tp_tps=float(source["recommended_actual_tps"]),
                    tp_reference_tps=reference_tps,
                    requested_ap_clients=int(source["ap_clients"]),
                    tp_high=stage == "stage5_tp_surge",
                )
            )
    return rows


def make_plot(path: Path, rows: list[dict[str, object]]) -> None:
    import matplotlib.pyplot as plt

    x = list(range(len(rows)))
    labels = [str(row["epoch"]).replace("stage", "S").replace("_tick", "-") for row in rows]
    ratio = [100 * float(row["tp_retention_ratio"]) for row in rows]
    admitted = [int(row["admitted_ap_clients"]) for row in rows]
    requested = [int(row["requested_ap_clients"]) for row in rows]
    sb = [float(row["sb_mb"]) for row in rows]
    dynamic = [float(row["actual_dynamic_mb"]) for row in rows]

    fig, axes = plt.subplots(3, 1, figsize=(14, 10), constrained_layout=True)
    axes[0].plot(x, ratio, marker="o", color="#147b83", label="observed TP retention")
    axes[0].axhline(95, color="#c44742", linestyle="--", label="95% SLO floor")
    axes[0].axhline(98, color="#2f8f60", linestyle=":", label="98% recovery threshold")
    axes[0].set_ylabel("TP retention (%)")
    axes[0].set_title("TP-SLO is live feedback, not a TPS training label")
    axes[0].legend(ncol=3)
    axes[0].grid(alpha=0.2)

    axes[1].step(x, requested, where="mid", color="#aab2b8", label="requested AP")
    axes[1].step(x, admitted, where="mid", color="#df8428", label="admitted AP")
    axes[1].set_ylabel("AP clients")
    axes[1].set_title("New AP is blocked first; sustained violation pauses AP at query boundaries")
    axes[1].legend()
    axes[1].grid(alpha=0.2)

    axes[2].bar(x, sb, color="#3474ad", label="shared_buffers")
    axes[2].bar(x, dynamic, bottom=sb, color="#e3a050", label="actual AP dynamic memory")
    axes[2].axhline(
        float(rows[0]["memory_target_max_mb"]), color="#c44742", linestyle="--",
        label="memory_target_max",
    )
    axes[2].set_ylabel("Managed memory (MB)")
    axes[2].set_title("SB grows only after graceful AP-memory debt is reclaimed")
    axes[2].legend(ncol=3)
    axes[2].grid(axis="y", alpha=0.2)

    axes[2].set_xticks(x, labels, rotation=55, ha="right", fontsize=8)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sb-recommendations", required=True, type=Path)
    parser.add_argument("--work-mem-recommendations", required=True, type=Path)
    parser.add_argument("--grant-candidates", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--stage-tps", type=Path)
    parser.add_argument("--diagnostic-reference-tps", type=float, default=1324.392697)
    parser.add_argument("--ticks-per-stage", type=int, default=3)
    parser.add_argument("--memory-target-max-mb", type=float, default=16384.0)
    parser.add_argument("--initial-sb-mb", type=int, default=8192)
    parser.add_argument("--tp-floor-ratio", type=float, default=0.95)
    parser.add_argument("--tp-recovery-ratio", type=float, default=0.98)
    parser.add_argument("--tp-severe-ratio", type=float, default=0.90)
    parser.add_argument("--reclaim-mb-per-tick", type=float, default=1024.0)
    parser.add_argument("--max-spill-io-mb", type=float, default=32768.0)
    args = parser.parse_args()

    if bool(args.observations) == bool(args.stage_tps):
        parser.error("provide exactly one of --observations or --stage-tps")

    targets = load_targets(args.sb_recommendations, args.work_mem_recommendations)
    profiles = load_grant_profiles(args.grant_candidates)
    policy = TpSloPolicy(
        floor_ratio=args.tp_floor_ratio,
        recovery_ratio=args.tp_recovery_ratio,
        severe_ratio=args.tp_severe_ratio,
        grant_reclaim_mb_per_tick=args.reclaim_mb_per_tick,
        max_spill_io_mb=args.max_spill_io_mb,
    )
    controller = TpSloController(
        targets, profiles, args.memory_target_max_mb, args.initial_sb_mb, policy
    )
    observations = (
        load_observations(args.observations)
        if args.observations
        else diagnostic_observations(
            args.stage_tps, args.diagnostic_reference_tps, args.ticks_per_stage
        )
    )
    rows = [controller.step(observation) for observation in observations]
    if not all(bool(row["memory_limit_respected"]) for row in rows):
        raise RuntimeError("controller exceeded memory_target_max")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "tp_slo_controller_actions.csv", rows)
    make_plot(args.out_dir / "tp_slo_controller_actions.png", rows)
    summary = {
        "model": "deterministic TP-SLO-first feedback controller",
        "uses_tps_for_training": False,
        "uses_tps_as_live_feedback": True,
        "open_loop_diagnostic": bool(args.stage_tps),
        "policy": asdict(policy),
        "memory_target_max_mb": args.memory_target_max_mb,
        "rows": len(rows),
        "slo_violating_observations": sum(not bool(row["tp_slo_met"]) for row in rows),
        "memory_limit_respected": True,
        "limitations": [
            "The stage-TPS mode repeats frozen observations to exercise policy escalation; it does not claim that the actions repaired TPS.",
            "A production guarantee requires a continuous run where each action changes subsequent observed TPS.",
            "Running operators release reduced grants gracefully; kernel/WLM quota exchange is still required for real enforcement.",
        ],
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
