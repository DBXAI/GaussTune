#!/usr/bin/env python3
"""Build a PPT-aligned five-stage Sysbench memory-transition artifact.

This tool deliberately does not add a model stage and does not claim that
openGauss has executed an online shared_buffers resize.  It turns the
existing V3 candidate grid into an auditable five-state *planned* trajectory:

    S1 memory-rich -> S2 yield SB -> S3 protect TP -> S4 backpressure
    -> S5 TP surge

The output separates replay/model evidence from runtime acceptance evidence.
That distinction prevents a static recommendation or a restart-bounded probe
from being presented as the PPT's no-restart dynamic-memory acceptance.
"""

from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


STAGES = ("S1", "S2", "S3", "S4", "S5")
PPT_ACTIONS = {
    "S1": {
        "state": "memory_rich",
        "trigger": "AP pressure is below the managed-memory limit",
        "action": (
            "establish the rich-stage SB target and increase per-query AP memory"
        ),
    },
    "S2": {
        "state": "reach_limit",
        "trigger": "AP pressure reaches memory_target_max",
        "action": "shrink shared_buffers by granules and transfer capacity to AP",
    },
    "S3": {
        "state": "protect_tp",
        "trigger": "AP pressure continues after the memory limit is reached",
        "action": "hold shared_buffers and lower per-query AP grants",
    },
    "S4": {
        "state": "backpressure",
        "trigger": "new AP requests continue after safe grants are exhausted",
        "action": "hold shared_buffers and queue new AP requests",
    },
    "S5": {
        "state": "tp_surge",
        "trigger": "the TP stream changes from low load to the PPT surge",
        "action": "raise shared_buffers and keep AP grants bounded",
    },
}


def _read_json(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object: %s" % path)
    return value


def _as_assignments(value: object) -> Dict[int, int]:
    if isinstance(value, dict):
        return {
            int(query): int(memory)
            for query, memory in value.items()
        }
    if isinstance(value, list):
        return {
            int(query): int(memory)
            for query, memory in value
        }
    raise ValueError("work_mem assignments must be an object or list")


def _assignment_rows(assignments: Mapping[int, int]) -> List[List[int]]:
    return [
        [int(query), int(memory)]
        for query, memory in sorted(assignments.items())
    ]


def _candidate_assignments(row: Mapping[str, object]) -> Dict[int, int]:
    return _as_assignments(row.get("work_mem", ()))


def _valid_candidates(result: Mapping[str, object]) -> List[Mapping[str, object]]:
    rows = result.get("candidates", ())
    if not isinstance(rows, list):
        raise ValueError("model result candidates must be a list")
    valid = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("valid") is True
        and row.get("predicted_tps") is not None
    ]
    if not valid:
        raise ValueError("model result has no valid candidates")
    return valid


def _reference_tps(candidates: Sequence[Mapping[str, object]]) -> float:
    return max(float(row["predicted_tps"]) for row in candidates)


def _eligible(
    candidates: Sequence[Mapping[str, object]],
    reference_tps: float,
    tolerance: float,
) -> List[Mapping[str, object]]:
    return [
        row for row in candidates
        if float(row["predicted_tps"])
        >= reference_tps * (1.0 - tolerance)
    ]


def _pick(
    candidates: Sequence[Mapping[str, object]],
    *,
    shared_buffers_mb: int,
    reference_tps: float,
    tolerance: float,
    maximize_dynamic: bool,
) -> Mapping[str, object]:
    rows = [
        row for row in candidates
        if int(row["shared_buffers_mb"]) == shared_buffers_mb
    ]
    if not rows:
        raise ValueError(
            "no existing candidate at SB=%dMB" % shared_buffers_mb
        )
    rows = _eligible(rows, reference_tps, tolerance)
    if not rows:
        raise ValueError(
            "no candidate at SB=%dMB within %.3f TPS tolerance"
            % (shared_buffers_mb, tolerance)
        )
    if maximize_dynamic:
        return max(
            rows,
            key=lambda row: (
                float(row["ap_dynamic_peak_mb"]),
                float(row["predicted_tps"]),
                tuple(_candidate_assignments(row).items()),
            ),
        )
    return min(
        rows,
        key=lambda row: (
            float(row["ap_dynamic_peak_mb"]),
            -float(row["predicted_tps"]),
            tuple(_candidate_assignments(row).items()),
        ),
    )


def _baseline_work_mem(
    query_ids: Sequence[int], value_mb: int,
) -> Dict[int, int]:
    return {int(query): int(value_mb) for query in query_ids}


def _stage_before_work_mem(
    stage_queries: Sequence[int],
    previous: Optional[Mapping[int, int]],
    baseline_mb: int,
) -> Dict[int, int]:
    previous = previous or {}
    return {
        int(query): int(previous.get(int(query), baseline_mb))
        for query in stage_queries
    }


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _effect_references(
    effect_dirs: Optional[Sequence[Path]],
) -> Dict[str, object]:
    if not effect_dirs:
        return {
            "available": False,
            "note": "No runtime effect directory was supplied.",
        }
    evidence_sets: List[Dict[str, object]] = []
    for effect_dir in effect_dirs:
        rows: Dict[str, object] = {}
        for stage in STAGES:
            paths = [
                effect_dir / ("sysbench-%s" % stage) / "stage_summary.json",
                effect_dir / stage / "stage_summary.json",
            ]
            for path in paths:
                if path.is_file():
                    rows[stage] = {
                        "path": str(path.resolve()),
                        "sha256": _sha256(path),
                    }
                    break
        evidence_sets.append({
            "directory": str(effect_dir.resolve()),
            "stage_summaries": rows,
        })
    return {
        "available": any(
            bool(item["stage_summaries"]) for item in evidence_sets
        ),
        "sets": evidence_sets,
        "note": (
            "These are independent stage probes, not one continuous "
            "no-restart dynamic-memory trajectory."
        ),
    }


def build_trajectory(
    recommendations: Mapping[str, object],
    *,
    recommendations_path: Path,
    model_dir: Path,
    memory_budget: Mapping[str, object],
    memory_budget_path: Path,
    memory_grid_mb: int = 64,
    baseline_shared_buffers_mb: int = 512,
    baseline_work_mem_mb: int = 32,
    tps_tolerance: float = 0.03,
    effect_dir: Optional[Path] = None,
    effect_dirs: Optional[Sequence[Path]] = None,
    kernel_evidence: Optional[Mapping[str, object]] = None,
    runtime_evidence: Optional[Mapping[str, object]] = None,
    runtime_evidence_path: Optional[Path] = None,
) -> Dict[str, object]:
    if recommendations.get("schema") != (
        "huawei7.five-stage-recommendations/v3"
    ):
        raise ValueError("this artifact is restricted to V3 recommendations")
    if memory_grid_mb <= 0:
        raise ValueError("memory_grid_mb must be positive")
    if baseline_shared_buffers_mb <= 0 or baseline_work_mem_mb <= 0:
        raise ValueError("baseline memory values must be positive")
    if not 0 <= tps_tolerance < 1:
        raise ValueError("tps_tolerance must be in [0, 1)")
    if effect_dirs is None and effect_dir is not None:
        effect_dirs = (effect_dir,)
    kernel_smoke_passed = bool(
        kernel_evidence is not None
        and kernel_evidence.get("passed") is True
        and int(kernel_evidence.get("restart_count", -1)) == 0
        and kernel_evidence.get("postmaster_pid_unchanged") is True
    )
    runtime_rows = (
        runtime_evidence.get("stages", [])
        if isinstance(runtime_evidence, dict) else []
    )
    runtime_by_stage = {
        str(row.get("stage")): row
        for row in runtime_rows
        if isinstance(row, dict)
    }
    runtime_gate_values = (
        runtime_evidence.get("gates", {})
        if isinstance(runtime_evidence, dict) else {}
    )
    runtime_stage_evidence_valid = bool(
        isinstance(runtime_evidence, dict)
        and set(runtime_by_stage) == set(STAGES)
        and all(
            "shared_buffers_before_mb" in runtime_by_stage[stage]
            and "shared_buffers_after_mb" in runtime_by_stage[stage]
            for stage in STAGES
        )
    )

    stage_rows = recommendations.get("stages")
    if not isinstance(stage_rows, list):
        raise ValueError("recommendations stages must be a list")
    sysbench_rows = {
        str(row["stage"]): row
        for row in stage_rows
        if isinstance(row, dict) and row.get("benchmark") == "sysbench"
    }
    if set(sysbench_rows) != set(STAGES):
        raise ValueError("recommendations must contain Sysbench S1-S5")

    tunable_pool = float(memory_budget["tunable_pool_mb"])
    memory_target_max = int(math.floor(tunable_pool / memory_grid_mb)) * memory_grid_mb
    model_documents: Dict[str, Mapping[str, object]] = {}
    candidate_sets: Dict[str, List[Mapping[str, object]]] = {}
    references: Dict[str, float] = {}
    for stage in STAGES:
        model_path = model_dir / stage / "model-result.json"
        model = _read_json(model_path)
        candidates = _valid_candidates(model)
        model_documents[stage] = model
        candidate_sets[stage] = candidates
        references[stage] = _reference_tps(candidates)

    # The numerical choices are deliberately taken from existing V3
    # candidates.  The state machine adds no new SB/WM point.
    selected: Dict[str, Mapping[str, object]] = {}
    selected["S1"] = max(
        candidate_sets["S1"],
        key=lambda row: float(row["predicted_tps"]),
    )
    selected["S2"] = _pick(
        candidate_sets["S2"],
        shared_buffers_mb=4096,
        reference_tps=references["S2"],
        tolerance=tps_tolerance,
        maximize_dynamic=True,
    )
    selected["S3"] = _pick(
        candidate_sets["S3"],
        shared_buffers_mb=4096,
        reference_tps=references["S3"],
        tolerance=tps_tolerance,
        maximize_dynamic=False,
    )
    selected["S4"] = _pick(
        candidate_sets["S4"],
        shared_buffers_mb=4096,
        reference_tps=references["S4"],
        tolerance=tps_tolerance,
        maximize_dynamic=False,
    )
    selected["S5"] = _pick(
        candidate_sets["S5"],
        shared_buffers_mb=5120,
        reference_tps=references["S5"],
        tolerance=tps_tolerance,
        maximize_dynamic=False,
    )

    transitions: List[Dict[str, object]] = []
    previous_sb = baseline_shared_buffers_mb
    previous_wm: Optional[Mapping[int, int]] = None
    for stage in STAGES:
        recommendation = sysbench_rows[stage]
        stage_queries = tuple(
            int(query) for query in recommendation["query_sha256"]
        )
        candidate = selected[stage]
        after_wm = _candidate_assignments(candidate)
        before_wm = _stage_before_work_mem(
            stage_queries, previous_wm, baseline_work_mem_mb,
        )
        active_count = len(stage_queries)
        if stage == "S4":
            admitted = max(1, active_count - 1)
        elif stage == "S5":
            admitted = max(1, active_count - 1)
        else:
            admitted = active_count
        queued = max(0, active_count - admitted)
        managed_mb = (
            int(candidate["shared_buffers_mb"])
            + float(candidate["ap_dynamic_peak_mb"])
        )
        if managed_mb > memory_target_max + 1e-9:
            raise ValueError(
                "%s planned memory exceeds memory_target_max: %.3f > %d"
                % (stage, managed_mb, memory_target_max)
            )
        transitions.append({
            "stage": stage,
            "state": PPT_ACTIONS[stage]["state"],
            "trigger": PPT_ACTIONS[stage]["trigger"],
            "action": PPT_ACTIONS[stage]["action"],
            "shared_buffers_before_mb": previous_sb,
            "shared_buffers_after_mb": int(candidate["shared_buffers_mb"]),
            "shared_buffers_delta_mb": (
                int(candidate["shared_buffers_mb"]) - previous_sb
            ),
            "work_mem_before": _assignment_rows(before_wm),
            "work_mem_after": _assignment_rows(after_wm),
            "work_mem_changed": before_wm != after_wm,
            "requested_ap_clients": active_count,
            "admitted_ap_clients": admitted,
            "queued_ap_clients": queued,
            "candidate": {
                "shared_buffers_mb": int(candidate["shared_buffers_mb"]),
                "work_mem": [
                    list(item) for item in candidate["work_mem"]
                ],
                "predicted_tps": float(candidate["predicted_tps"]),
                "reference_best_predicted_tps": references[stage],
                "delta_from_reference_fraction": (
                    float(candidate["predicted_tps"]) / references[stage] - 1.0
                ),
                "ap_dynamic_peak_mb": float(
                    candidate["ap_dynamic_peak_mb"]
                ),
                "managed_memory_mb": managed_mb,
            },
            "constraints": {
                "memory_target_max_mb": memory_target_max,
                "granule_mb": memory_grid_mb,
                "memory_invariant_holds": (
                    managed_mb <= memory_target_max + 1e-9
                ),
                "restart_required_by_current_runner": True,
        "runtime_action_executed": False,
        "evidence_status": "planned_existing_candidate_only",
            },
        })
        previous_sb = int(candidate["shared_buffers_mb"])
        previous_wm = after_wm

    gates = {
        "exactly_five_ppt_stages": True,
        "only_existing_v3_candidates": True,
        "memory_target_max_respected_in_plan": all(
            bool(row["constraints"]["memory_invariant_holds"])
            for row in transitions
        ),
        "sb_before_after_recorded": all(
            "shared_buffers_before_mb" in row
            and "shared_buffers_after_mb" in row
            for row in transitions
        ),
        "wm_before_after_recorded": all(
            "work_mem_before" in row and "work_mem_after" in row
            for row in transitions
        ),
        "online_sb_resize_executed": kernel_smoke_passed,
        "session_work_mem_transition_executed": False,
        "runtime_backpressure_executed": False,
        "zero_restart_runtime_evidence": kernel_smoke_passed,
        "tps_jitter_within_3_percent": False,
        "io_spill_measured": False,
        "spill_zero_in_all_runs": False,
    }
    if runtime_stage_evidence_valid:
        for row in transitions:
            runtime_row = runtime_by_stage[row["stage"]]
            row["constraints"]["runtime_action_executed"] = True
            row["constraints"]["restart_required_by_current_runner"] = False
            row["constraints"]["evidence_status"] = (
                "continuous_online_runtime_evidence"
            )
            if (
                int(runtime_row["shared_buffers_before_mb"])
                != int(row["shared_buffers_before_mb"])
                or int(runtime_row["shared_buffers_after_mb"])
                != int(row["shared_buffers_after_mb"])
            ):
                row["constraints"]["runtime_action_executed"] = False
        gates["online_sb_resize_executed"] = bool(
            kernel_smoke_passed
            and runtime_gate_values.get("online_sb_resize_executed") is True
            and all(
                row["constraints"]["runtime_action_executed"]
                for row in transitions
            )
        )
        gates["session_work_mem_transition_executed"] = bool(
            runtime_gate_values.get("session_work_mem_transition_executed")
            is True
        )
        gates["runtime_backpressure_executed"] = bool(
            runtime_gate_values.get("runtime_backpressure_executed") is True
        )
        gates["zero_restart_runtime_evidence"] = bool(
            kernel_smoke_passed
            and runtime_gate_values.get("zero_restart_runtime_evidence") is True
        )
        gates["tps_jitter_within_3_percent"] = bool(
            runtime_gate_values.get("tps_jitter_within_3_percent") is True
        )
        gates["io_spill_measured"] = bool(
            runtime_gate_values.get("io_spill_measured") is True
        )
        gates["spill_zero_in_all_runs"] = bool(
            runtime_gate_values.get("spill_zero_in_all_runs") is True
        )
    if runtime_stage_evidence_valid:
        status = "ppt_five_stage_runtime_acceptance_passed"
    elif kernel_smoke_passed:
        status = "kernel_online_resize_smoke_passed_stage_acceptance_pending"
    else:
        status = "planned_replay_not_runtime_acceptance"
    return {
        "schema": "huawei7.sysbench-ppt-dynamic-acceptance/v1",
        "benchmark": "sysbench",
        "status": status,
        "ppt_alignment": {
            "stage_order": list(STAGES),
            "same_stage_count": True,
            "new_model_stage": False,
            "cpu_model_added": False,
            "tpcc_model_added": False,
            "new_trace_collection": False,
            "new_parameter_fit": False,
        },
        "inputs": {
            "recommendations": {
                "path": str(recommendations_path.resolve()),
                "sha256": _sha256(recommendations_path),
            },
            "memory_budget": {
                "path": str(memory_budget_path.resolve()),
                "sha256": _sha256(memory_budget_path),
            },
            "model_dir": str(model_dir.resolve()),
            "effect_evidence": _effect_references(effect_dirs),
            "kernel_online_resize_evidence": kernel_evidence,
            "continuous_runtime_evidence": (
                {
                    "path": str(runtime_evidence_path.resolve()),
                    "sha256": _sha256(runtime_evidence_path),
                }
                if runtime_evidence_path is not None else None
            ),
        },
        "policy": {
            "baseline_shared_buffers_mb": baseline_shared_buffers_mb,
            "baseline_work_mem_mb": baseline_work_mem_mb,
            "memory_target_max_mb": memory_target_max,
            "granule_mb": memory_grid_mb,
            "tps_tolerance_fraction": tps_tolerance,
            "candidate_policy": (
                "S1 max TPS; S2 lower SB/max AP grant; "
                "S3/S4 lower AP grant at held SB; "
                "S5 raise SB with bounded AP grant"
            ),
            "work_mem_policy": "new_sessions_only",
            "active_session_wm_reduction_required": False,
        },
        "transitions": transitions,
        "acceptance_gates": gates,
        "acceptance_passed": bool(all(bool(value) for value in gates.values())),
        "limitations": [
            "This artifact is a deterministic five-stage transition plan "
            "from existing V3 candidates; it is not a kernel execution log.",
            "The acceptance contract requires work_mem to change for new AP "
            "sessions; it does not require mutation of an active query's grant.",
            "The runtime queue evidence is controller-level admission "
            "evidence, not a kernel queue implementation.",
            "TPS/IO/spill evidence is only promoted when a continuous "
            "runtime evidence document is supplied.",
        ],
    }


def render_markdown(document: Mapping[str, object]) -> str:
    status = str(document["status"])
    if status == "ppt_five_stage_runtime_executed_active_session_wm_gate_pending":
        status_line = (
            "**连续五阶段 runtime 已执行；活跃会话 work_mem 优雅下降仍未实现**"
        )
    elif status == "ppt_five_stage_runtime_acceptance_passed":
        status_line = "**连续五阶段 runtime acceptance 已通过**"
    elif status == "kernel_online_resize_smoke_passed_stage_acceptance_pending":
        status_line = (
            "**kernel online-SB smoke 已通过；连续五阶段 runtime acceptance "
            "仍未通过**"
        )
    else:
        status_line = "**planned replay，不是 runtime acceptance**"
    lines = [
        "# Sysbench PPT 五阶段动态内存验收轨迹",
        "",
        "> 当前状态：" + status_line + "。"
        "本报告把 SB/WM 的前后变化显式串起来，但不把静态候选或重启探针"
        "冒充成 PPT 的在线无重启实现。",
        "",
        "## 五阶段轨迹",
        "",
        "| stage | state | SB before → after | WM before → after | AP admitted/queued | action |",
        "|---|---|---:|---|---:|---|",
    ]
    for row in document["transitions"]:
        before_wm = ";".join(
            "q%d=%d" % (query, memory)
            for query, memory in row["work_mem_before"]
        )
        after_wm = ";".join(
            "q%d=%d" % (query, memory)
            for query, memory in row["work_mem_after"]
        )
        lines.append(
            "| %s | %s | %d → %d | %s → %s | %d/%d | %s |"
            % (
                row["stage"], row["state"],
                row["shared_buffers_before_mb"],
                row["shared_buffers_after_mb"],
                before_wm or "-",
                after_wm or "-",
                row["admitted_ap_clients"],
                row["queued_ap_clients"],
                row["action"],
            )
        )
    lines += [
        "",
        "## 约束与验收状态",
        "",
        "| gate | result |",
        "|---|---|",
    ]
    for key, value in document["acceptance_gates"].items():
        lines.append("| %s | %s |" % (key, "PASS" if value else "NOT PROVEN"))
    lines += [
        "",
        "## 关键边界",
        "",
        "- 轨迹的每个 SB/WM 数值均来自已有 V3 候选点；没有新增模型点。",
        "- `memory_target_max` 和 `granule` 只用于检查规划中的内存守恒。",
        "- 如果提供连续 runtime evidence，SB/WM/队列/TPS 只按该证据"
        "更新 gate，不会修改 V3 模型候选。",
        "- 当前验收口径是新会话 Work_mem 生效；不要求对活跃会话的"
        " Work_mem 做强制下降。",
        "- S4/S5 的 admitted/queued 是 controller-level admission 证据，"
        "不是 openGauss 内核队列。",
    ]
    return "\n".join(lines) + "\n"


def render_svg(document: Mapping[str, object]) -> str:
    """Render a dependency-free PPT-like transition chart."""

    width, height = 1200, 720
    left, right = 90, 1140
    top, bottom = 80, 650
    stages = list(document["transitions"])
    xs = [
        left + index * (right - left) / max(1, len(stages) - 1)
        for index in range(len(stages))
    ]
    sb_before = [int(row["shared_buffers_before_mb"]) for row in stages]
    sb_after = [int(row["shared_buffers_after_mb"]) for row in stages]
    wm_before = [
        sum(int(memory) for _query, memory in row["work_mem_before"])
        for row in stages
    ]
    wm_after = [
        sum(int(memory) for _query, memory in row["work_mem_after"])
        for row in stages
    ]
    queued = [int(row["queued_ap_clients"]) for row in stages]
    sb_max = max(sb_before + sb_after + [1])
    wm_max = max(wm_before + wm_after + [1])

    def y(value: float, low: float, high: float, y0: float, y1: float) -> float:
        if high <= low:
            return (y0 + y1) / 2.0
        return y1 - (value - low) / (high - low) * (y1 - y0)

    def polyline(values, low, high, y0, y1, color):
        points = " ".join(
            "%.1f,%.1f" % (x, y(value, low, high, y0, y1))
            for x, value in zip(xs, values)
        )
        return '<polyline points="%s" fill="none" stroke="%s" stroke-width="4"/>' % (
            points, color,
        )

    def dots(values, low, high, y0, y1, color):
        return "".join(
            '<circle cx="%.1f" cy="%.1f" r="6" fill="%s"/>'
            % (x, y(value, low, high, y0, y1), color)
            for x, value in zip(xs, values)
        )

    def text(value, x, y_value, size=16, weight="normal", color="#1f2933"):
        return (
            '<text x="%.1f" y="%.1f" font-family="Arial,sans-serif" '
            'font-size="%d" font-weight="%s" fill="%s">%s</text>'
            % (
                x, y_value, size, weight, color,
                html.escape(str(value)),
            )
        )

    sb_top, sb_bottom = 120, 320
    wm_top, wm_bottom = 410, 590
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d">' % (width, height, width, height),
        '<rect width="100%%" height="100%%" fill="#ffffff"/>',
        text("Sysbench PPT 五阶段动态内存轨迹（planned replay）", 40, 42, 24, "bold"),
        text(
            "当前不是 runtime acceptance；红线表示验收能力尚未由实际运行证明",
            40, 68, 14, "normal", "#9b2c2c",
        ),
        '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#d9e2ec"/>'
        % (left, sb_top, right, sb_top),
        '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#d9e2ec"/>'
        % (left, sb_bottom, right, sb_bottom),
        '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#d9e2ec"/>'
        % (left, wm_top, right, wm_top),
        '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#d9e2ec"/>'
        % (left, wm_bottom, right, wm_bottom),
        text("SB (MB)", 18, 125, 16, "bold"),
        text("query-level WM total (MB)", 18, 415, 16, "bold"),
        polyline(sb_before, 0, sb_max, sb_top, sb_bottom, "#7b8794"),
        polyline(sb_after, 0, sb_max, sb_top, sb_bottom, "#1565c0"),
        dots(sb_before, 0, sb_max, sb_top, sb_bottom, "#7b8794"),
        dots(sb_after, 0, sb_max, sb_top, sb_bottom, "#1565c0"),
        polyline(wm_before, 0, wm_max, wm_top, wm_bottom, "#d97706"),
        polyline(wm_after, 0, wm_max, wm_top, wm_bottom, "#059669"),
        dots(wm_before, 0, wm_max, wm_top, wm_bottom, "#d97706"),
        dots(wm_after, 0, wm_max, wm_top, wm_bottom, "#059669"),
        '<line x1="%d" y1="%d" x2="%d" y2="%d" '
        'stroke="#c53030" stroke-width="2" stroke-dasharray="7,5"/>'
        % (left, 350, right, 350),
        text("S1→S2→S3→S4→S5", left, 690, 16, "bold"),
        text("SB before", 760, 95, 14, "normal", "#7b8794"),
        '<line x1="830" y1="90" x2="870" y2="90" stroke="#7b8794" stroke-width="4"/>',
        text("SB after", 900, 95, 14, "normal", "#1565c0"),
        '<line x1="965" y1="90" x2="1005" y2="90" stroke="#1565c0" stroke-width="4"/>',
        text("WM before", 760, 385, 14, "normal", "#d97706"),
        '<line x1="850" y1="380" x2="890" y2="380" stroke="#d97706" stroke-width="4"/>',
        text("WM after", 920, 385, 14, "normal", "#059669"),
        '<line x1="1005" y1="380" x2="1045" y2="380" stroke="#059669" stroke-width="4"/>',
    ]
    for x, row in zip(xs, stages):
        parts.append(text(row["stage"], x - 14, 620, 16, "bold"))
        parts.append(
            text(
                "Q%d/%d" % (
                    int(row["admitted_ap_clients"]),
                    int(row["queued_ap_clients"]),
                ),
                x - 18, 640, 12, "normal", "#9b2c2c",
            )
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recommendations", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--memory-budget", type=Path, required=True)
    parser.add_argument(
        "--effect-dir", type=Path, action="append",
        help="repeatable directory containing existing stage summaries",
    )
    parser.add_argument("--kernel-evidence", type=Path)
    parser.add_argument("--runtime-evidence", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--memory-grid-mb", type=int, default=64)
    parser.add_argument("--baseline-shared-buffers-mb", type=int, default=512)
    parser.add_argument("--baseline-work-mem-mb", type=int, default=32)
    parser.add_argument("--tps-tolerance", type=float, default=0.03)
    args = parser.parse_args()
    recommendations = _read_json(args.recommendations)
    budget = _read_json(args.memory_budget)
    kernel_evidence = (
        _read_json(args.kernel_evidence)
        if args.kernel_evidence is not None else None
    )
    runtime_evidence = (
        _read_json(args.runtime_evidence)
        if args.runtime_evidence is not None else None
    )
    document = build_trajectory(
        recommendations,
        recommendations_path=args.recommendations,
        model_dir=args.model_dir,
        memory_budget=budget,
        memory_budget_path=args.memory_budget,
        memory_grid_mb=args.memory_grid_mb,
        baseline_shared_buffers_mb=args.baseline_shared_buffers_mb,
        baseline_work_mem_mb=args.baseline_work_mem_mb,
        tps_tolerance=args.tps_tolerance,
        effect_dirs=args.effect_dir,
        kernel_evidence=kernel_evidence,
        runtime_evidence=runtime_evidence,
        runtime_evidence_path=args.runtime_evidence,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "dynamic-acceptance.json"
    json_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    markdown_path = args.out_dir / "DYNAMIC_ACCEPTANCE.md"
    markdown_path.write_text(
        render_markdown(document), encoding="utf-8",
    )
    svg_path = args.out_dir / "dynamic-acceptance.svg"
    svg_path.write_text(render_svg(document), encoding="utf-8")
    print(json.dumps({
        "json": str(json_path.resolve()),
        "markdown": str(markdown_path.resolve()),
        "svg": str(svg_path.resolve()),
        "status": document["status"],
        "acceptance_passed": document["acceptance_passed"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
