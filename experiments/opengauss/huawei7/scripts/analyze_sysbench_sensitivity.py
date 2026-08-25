#!/usr/bin/env python3
"""Analyze Sysbench SB/work_mem sensitivity from existing V3 artifacts.

This is a reporting tool only.  It does not collect new traces, add a model
stage, or fit new parameters.  It reads the already-produced candidate
curves and the already-recorded effect-test episodes, then exposes the
resource/performance frontier under several declared TPS tolerances.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence

from huawei7.provenance import sha256


DEFAULT_STAGES = ("S1", "S2", "S3", "S4", "S5")
DEFAULT_TOLERANCES = (0.001, 0.0025, 0.005, 0.01, 0.03)
# This is the existing V3 pipeline's practical tolerance.  The smaller
# tolerances below are sensitivity bands only; they are not new tuning
# targets and must not silently become a frozen recommendation.
PRIMARY_TOLERANCE = 0.03


def _read(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object: %s" % path)
    return value


def _total_work_mem(row: Mapping[str, object]) -> int:
    return sum(int(memory) for _query, memory in row.get("work_mem", ()))


def _resource_key(row: Mapping[str, object]):
    return (
        int(row["shared_buffers_mb"]),
        float(row["ap_dynamic_peak_mb"]),
        _total_work_mem(row),
        -float(row["predicted_tps"]),
        tuple(tuple(item) for item in row.get("work_mem", ())),
    )


def _select_near_optimal(
    candidates: Sequence[Mapping[str, object]], tolerance: float,
) -> Mapping[str, object]:
    valid = [
        row for row in candidates
        if row.get("valid") is True and row.get("predicted_tps") is not None
    ]
    if not valid:
        raise ValueError("candidate set is empty")
    reference = max(valid, key=lambda row: float(row["predicted_tps"]))
    reference_tps = float(reference["predicted_tps"])
    eligible = [
        row for row in valid
        if float(row["predicted_tps"]) >= reference_tps * (1.0 - tolerance)
    ]
    return min(eligible, key=_resource_key)


def _sensitivity_for_stage(
    result: Mapping[str, object], *, tolerances: Sequence[float],
) -> Dict[str, object]:
    candidates = [
        row for row in result.get("candidates", ())
        if isinstance(row, dict) and row.get("valid") is True
    ]
    if not candidates:
        raise ValueError("Sysbench result has no valid candidates")
    reference = max(candidates, key=lambda row: float(row["predicted_tps"]))
    reference_tps = float(reference["predicted_tps"])
    by_sb: Dict[int, List[Mapping[str, object]]] = defaultdict(list)
    for row in candidates:
        by_sb[int(row["shared_buffers_mb"])].append(row)
    sb_curve = []
    for sb, rows in sorted(by_sb.items()):
        best_at_sb = max(rows, key=lambda row: float(row["predicted_tps"]))
        lowest_resource_at_sb = min(rows, key=_resource_key)
        sb_curve.append({
            "shared_buffers_mb": sb,
            "best_predicted_tps": float(best_at_sb["predicted_tps"]),
            "delta_from_reference_fraction": (
                float(best_at_sb["predicted_tps"]) / reference_tps - 1.0
            ),
            "best_work_mem": [
                list(item) for item in best_at_sb["work_mem"]
            ],
            "lowest_resource_predicted_tps": float(
                lowest_resource_at_sb["predicted_tps"]
            ),
            "lowest_resource_delta_from_reference_fraction": (
                float(lowest_resource_at_sb["predicted_tps"])
                / reference_tps - 1.0
            ),
            "lowest_resource_work_mem": [
                list(item) for item in lowest_resource_at_sb["work_mem"]
            ],
            "lowest_resource_ap_dynamic_peak_mb": (
                float(lowest_resource_at_sb["ap_dynamic_peak_mb"])
            ),
            "candidate_count": len(rows),
        })
    frontier = []
    for tolerance in tolerances:
        selected = _select_near_optimal(candidates, tolerance)
        frontier.append({
            "tps_tolerance_fraction": tolerance,
            "selected": {
                "shared_buffers_mb": int(selected["shared_buffers_mb"]),
                "work_mem": [list(item) for item in selected["work_mem"]],
                "total_work_mem_mb": _total_work_mem(selected),
                "ap_dynamic_peak_mb": float(selected["ap_dynamic_peak_mb"]),
                "ap_execution_seconds": selected.get("ap_execution_seconds"),
                "predicted_tps": float(selected["predicted_tps"]),
                "delta_from_reference_fraction": (
                    float(selected["predicted_tps"]) / reference_tps - 1.0
                ),
            },
        })
    return {
        "stage": result.get("stage"),
        "reference_best": {
            "shared_buffers_mb": int(reference["shared_buffers_mb"]),
            "work_mem": [list(item) for item in reference["work_mem"]],
            "predicted_tps": reference_tps,
            "ap_dynamic_peak_mb": float(reference["ap_dynamic_peak_mb"]),
        },
        "sb_curve": sb_curve,
        "resource_frontier": frontier,
        "candidate_count": len(candidates),
        "evidence_note": (
            "SB curve uses the existing pipeline candidate points and its "
            "existing empirical interpolation; no new trace or fit was added."
        ),
    }


def build_report(
    results_dir: Path, effect_dir: Path, backup_dir: Path,
) -> Dict[str, object]:
    stages = []
    for stage in DEFAULT_STAGES:
        path = results_dir / stage / "model-result.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        result = _read(path)
        result_with_stage = dict(result, stage=stage)
        sensitivity = _sensitivity_for_stage(
            result_with_stage, tolerances=DEFAULT_TOLERANCES,
        )
        effect_path = effect_dir / ("sysbench-%s" % stage) / "stage_summary.json"
        effect = _read(effect_path) if effect_path.is_file() else None
        stages.append({
            "stage": stage,
            "model_result": {
                "path": str(path.resolve()),
                "sha256": sha256(path),
                "schema": result.get("schema"),
                "selection_rule": result.get("selection_rule"),
                "current_best": result.get("best"),
            },
            "sensitivity": sensitivity,
            "effect_test": (
                {
                    "path": str(effect_path.resolve()),
                    "sha256": sha256(effect_path),
                    "actual_tps": effect.get("throughput_tps"),
                    "predicted_tps": effect.get("predicted_tps"),
                    "absolute_prediction_error_fraction": (
                        effect.get("absolute_prediction_error_fraction")
                    ),
                    "valid": effect.get("valid"),
                }
                if effect is not None else None
            ),
        })
    return {
        "schema": "huawei7.sysbench-sensitivity-analysis/v1",
        "benchmark": "sysbench",
        "stages": stages,
        "tps_tolerance_fractions": list(DEFAULT_TOLERANCES),
        "primary_tps_tolerance_fraction": PRIMARY_TOLERANCE,
        "backup": {
            "path": str(backup_dir.resolve()),
            "readme": str((backup_dir / "README.txt").resolve()),
        },
        "method": {
            "new_model_stage": False,
            "new_trace_collection": False,
            "new_parameter_fit": False,
            "selection_only": True,
            "resource_order": [
                "shared_buffers_mb",
                "ap_dynamic_peak_mb",
                "total_work_mem_mb",
                "predicted_tps",
            ],
            "interpretation": (
                "A candidate is only resource-preferred after it remains "
                "inside an explicitly declared TPS tolerance of the existing "
                "maximum-TPS candidate."
            ),
        },
    }


def _markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Sysbench 细粒度敏感性分析",
        "",
        "本报告只读取现有 V3 候选和已有实际运行证据；没有新增模型阶段、"
        "没有新采集、没有重新拟合。",
        "",
        "## 结论",
        "",
        "- 现有五个阶段的 SB 曲线均在 SB=2048MB 左右进入平台期。",
        "- 当前 SB=5120MB 是 `max predicted TPS` 选择规则的结果，不代表 "
        "5120MB 是唯一有效配置。",
        "- 资源感知选择必须先声明 TPS 容差，再在容差内最小化 SB 和 AP 内存；"
        "这里以当前 V3 已有的 3% practical tolerance 作为主参考。",
        "",
        "| stage | current SB | reference TPS | SB=2048 best TPS delta | 3% resource choice |",
        "|---|---:|---:|---:|---|",
    ]
    for stage in report["stages"]:
        sensitivity = stage["sensitivity"]
        curve = {
            row["shared_buffers_mb"]: row
            for row in sensitivity["sb_curve"]
        }
        sb2048 = curve.get(2048)
        frontier = next(
            row for row in sensitivity["resource_frontier"]
            if abs(
                row["tps_tolerance_fraction"]
                - report["primary_tps_tolerance_fraction"]
            ) < 1e-12
        )
        current = sensitivity["reference_best"]
        selected = frontier["selected"]
        lines.append(
            "| %s | %d | %.2f | %.2f%% | SB=%d, WM=%s, TPS=%.2f |"
            % (
                stage["stage"], current["shared_buffers_mb"],
                current["predicted_tps"],
                100.0 * sb2048["delta_from_reference_fraction"],
                selected["shared_buffers_mb"],
                selected["work_mem"], selected["predicted_tps"],
            )
        )
    lines += [
        "",
        "## 注意",
        "",
        "- 这里的 SB=2048/3072/... 曲线是已有 TP empirical 点之间的现有插值，"
        "不是新增实测点。",
        "- 低 work_mem 候选对 Sysbench TP TPS 可能等价，但 AP 查询完成时间 "
        "尚未在本次短实际测试中完成验证，因此不能直接把低 WM 作为最终 AP 配置。",
        "- 0.1%/0.25%/0.5%/1% 只是展示候选点何时进入前沿，不能把某个"
        "小容差当作新增精度；",
        "- 低 work_mem 选择仍需独立的 AP 完成时间证据。当前短测中 AP 查询"
        "在测量边界被取消，不能据此冻结低 WM；",
        "- 下一步若要冻结新推荐，只能在当前流程已有的资源前沿上选择，不能用 "
        "新的短测结果反向调参数。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--effect-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(
        args.results_dir.resolve(), args.effect_dir.resolve(),
        args.backup_dir.resolve(),
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "sensitivity-report.json"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.out_dir / "REPORT.md").write_text(
        _markdown(report), encoding="utf-8",
    )
    print(json.dumps({
        "json": str(json_path.resolve()),
        "sha256": sha256(json_path),
        "markdown": str((args.out_dir / "REPORT.md").resolve()),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
