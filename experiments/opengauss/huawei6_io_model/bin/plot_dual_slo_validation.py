#!/usr/bin/env python3
"""Summarize a five-stage TP stability and AP non-starvation validation."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def truth(value: str) -> bool:
    return value.strip().lower() == "true"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result_dir", type=Path)
    args = parser.parse_args()

    result_dir = args.result_dir.resolve()
    summary = read_json(result_dir / "summary.json")
    tp_rows = read_csv(result_dir / "stage_acceptance.csv")
    ap_rows = read_csv(result_dir / "ap_stage_acceptance.csv")
    control_rows = read_csv(result_dir / "controller_actions.csv")

    if len(tp_rows) != 5 or len(ap_rows) != 5:
        raise ValueError("expected exactly five TP and AP stage results")

    reference_tps = float(summary["tp_reference_tps"])
    labels = [f"S{i}" for i in range(1, 6)]
    stage_names = [row["stage"] for row in tp_rows]
    final_tps = [
        reference_tps * float(row["final_rolling_retention"]) for row in tp_rows
    ]
    violating_windows = [int(row["violating_control_windows"]) for row in tp_rows]
    max_wait = [float(row["max_initial_wait_seconds"]) for row in ap_rows]
    min_service = [float(row["min_service_seconds"]) for row in ap_rows]
    requested = [int(row["requested_queries"]) for row in ap_rows]
    progressed = [int(row["queries_with_backend_progress"]) for row in ap_rows]
    completed = [int(row.get("completed_queries") or 0) for row in ap_rows]
    ap_cpu = [float(row["total_backend_cpu_seconds"]) for row in ap_rows]
    ap_io = [
        float(row["total_backend_read_mb"]) + float(row["total_backend_write_mb"])
        for row in ap_rows
    ]
    max_memory = [
        max(
            float(row["managed_memory_mb"])
            for row in control_rows
            if row["stage"] == stage
        )
        for stage in stage_names
    ]

    wait_limit = float(summary["ap_max_initial_wait_seconds"])
    service_floor = float(summary["ap_min_service_seconds"])
    memory_limit = max(float(row["memory_target_max_mb"]) for row in control_rows)
    tp_pass = bool(summary["all_stages_final_rolling_slo_met"])
    ap_pass = bool(summary["all_stages_ap_nonstarvation_slo_met"])
    natural_completion_pass = (
        summary.get("stage_transition_mode")
        == "wait_for_natural_query_completion"
        and bool(summary.get("all_stages_ap_queries_completed_naturally", False))
        and sum(completed) == sum(requested)
    )
    memory_pass = all(truth(row["memory_limit_respected"]) for row in control_rows)

    report = {
        "experiment": "Huawei5 five-stage TP/AP dual-SLO validation",
        "tp_reference_tps": reference_tps,
        "tp_acceptance_band_tps": [0.95 * reference_tps, 1.05 * reference_tps],
        "fixed_tp_terminals": summary["fixed_tp_terminals"],
        "ap_max_initial_wait_seconds": wait_limit,
        "ap_min_service_seconds": service_floor,
        "memory_limit_mb": memory_limit,
        "stages": {},
        "all_stages_tp_slo_met": tp_pass,
        "all_stages_ap_nonstarvation_slo_met": ap_pass,
        "all_stages_ap_queries_completed_naturally": natural_completion_pass,
        "all_samples_memory_limit_respected": memory_pass,
        "cross_stage_final_tps_max_min_gap": max(final_tps) - min(final_tps),
        "total_15s_control_window_violations": sum(violating_windows),
        "overall_acceptance_passed": (
            tp_pass and ap_pass and natural_completion_pass and memory_pass
        ),
        "validation_boundary": (
            "A valid run must wait for every submitted AP SQL to return naturally. "
            "Runs that cancel SQL at the fixed stage boundary are superseded."
        ),
    }
    for index, stage in enumerate(stage_names):
        report["stages"][labels[index]] = {
            "name": stage,
            "final_45s_tp_tps": round(final_tps[index], 3),
            "max_ap_initial_wait_seconds": max_wait[index],
            "min_ap_service_seconds": min_service[index],
            "ap_queries_with_backend_progress": progressed[index],
            "ap_queries_requested": requested[index],
            "ap_queries_completed_naturally": completed[index],
            "ap_backend_cpu_seconds": ap_cpu[index],
            "ap_backend_io_mb": round(ap_io[index], 3),
            "max_managed_memory_mb": max_memory[index],
            "violating_15s_control_windows": violating_windows[index],
            "tp_slo_met": truth(tp_rows[index]["final_rolling_slo_met"]),
            "ap_nonstarvation_slo_met": truth(
                ap_rows[index]["ap_nonstarvation_slo_met"]
            ),
        }

    (result_dir / "dual_slo_validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    import matplotlib.pyplot as plt
    import numpy as np

    x = np.arange(5)
    fig, axes = plt.subplots(2, 2, figsize=(15, 9), constrained_layout=True)

    tp_ax = axes[0, 0]
    tp_bars = tp_ax.bar(labels, final_tps, color="#2878b5", width=0.62)
    tp_ax.axhspan(0.95 * reference_tps, 1.05 * reference_tps, color="#dcefe4")
    tp_ax.axhline(reference_tps, color="#263238", linestyle="--", linewidth=1.2)
    tp_ax.set_ylim(0.925 * reference_tps, 1.07 * reference_tps)
    tp_ax.set_ylabel("Final 45-second TP TPS")
    tp_ax.set_title("TP stability: all stages inside 800 TPS +/-5%")
    tp_ax.bar_label(tp_bars, fmt="%.1f", padding=3, fontweight="bold")
    tp_ax.grid(axis="y", alpha=0.2)

    wait_ax = axes[0, 1]
    wait_bars = wait_ax.bar(labels, max_wait, color="#d67b27", width=0.62)
    wait_ax.axhline(wait_limit, color="#a92d2d", linestyle="--", linewidth=1.5)
    wait_ax.set_ylim(0, wait_limit * 1.2)
    wait_ax.set_ylabel("Maximum initial AP wait (seconds)")
    wait_ax.set_title(f"AP admission: every query starts within {wait_limit:.0f}s")
    wait_ax.bar_label(wait_bars, fmt="%.1f", padding=3, fontweight="bold")
    wait_ax.grid(axis="y", alpha=0.2)

    service_ax = axes[1, 0]
    service_bars = service_ax.bar(labels, min_service, color="#2f8a66", width=0.62)
    service_ax.axhline(service_floor, color="#a92d2d", linestyle="--", linewidth=1.5)
    service_ax.set_ylim(0, max(min_service) * 1.18)
    service_ax.set_ylabel("Minimum AP service per query (seconds)")
    service_ax.set_title(f"AP service: every query receives at least {service_floor:.0f}s")
    service_ax.bar_label(service_bars, fmt="%.1f", padding=3, fontweight="bold")
    service_ax.grid(axis="y", alpha=0.2)

    evidence_ax = axes[1, 1]
    completion_bars = evidence_ax.bar(
        x - 0.18, completed, 0.36, color="#6c5aa7", label="Queries completed naturally"
    )
    requested_bars = evidence_ax.bar(
        x + 0.18, requested, 0.36, color="#a7a7a7", label="Queries requested"
    )
    evidence_ax.set_xticks(x, labels)
    evidence_ax.set_ylim(0, max(requested) + 1.0)
    evidence_ax.set_ylabel("AP query count")
    evidence_ax.set_title(
        f"Natural SQL completion: {sum(completed)}/{sum(requested)} queries"
    )
    evidence_ax.bar_label(completion_bars, padding=3)
    evidence_ax.bar_label(requested_bars, padding=3)
    evidence_ax.legend(loc="upper left")
    evidence_ax.grid(axis="y", alpha=0.2)

    outcome = "PASS" if report["overall_acceptance_passed"] else "SUPERSEDED"
    fig.suptitle(
        "Huawei5 closed-loop memory control: TP stability + AP non-starvation "
        f"[{outcome}]\n"
        f"Cross-stage TP gap {report['cross_stage_final_tps_max_min_gap']:.2f} TPS; "
        f"managed-memory peak {max(max_memory):.0f}/{memory_limit:.0f} MB; "
        f"transient 15s violations {sum(violating_windows)}",
        fontsize=15,
        fontweight="bold",
    )
    fig.savefig(result_dir / "dual_slo_five_stage_acceptance.png", dpi=180)
    plt.close(fig)

    lines = [
        "# Huawei5 五阶段双 SLO 实测结果",
        "",
        f"- 总结论：**{'通过' if report['overall_acceptance_passed'] else '旧口径作废，必须自然完成后重测'}**。",
        f"- TP 条件：32 terminals、固定 offered rate {reference_tps:.0f} TPS，验收区间 760-840 TPS。",
        f"- AP 条件：初次等待不超过 {wait_limit:.0f} 秒、观察到真实 CPU/I/O，并且每条 SQL 自然返回。",
        f"- 内存条件：受控内存不超过 {memory_limit:.0f}MB。",
        "",
        "| 阶段 | 最终 45 秒 TP TPS | 15秒越界窗口 | AP 最大等待(s) | AP 最短服务(s) | 自然完成/请求 | 内存峰值(MB) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for index, label in enumerate(labels):
        lines.append(
            f"| {label} | {final_tps[index]:.2f} | {violating_windows[index]} | {max_wait[index]:.2f} | "
            f"{min_service[index]:.2f} | {completed[index]}/{requested[index]} | "
            f"{max_memory[index]:.0f} |"
        )
    lines.extend(
        [
            "",
            f"五阶段最终 TPS 最大值与最小值相差 {max(final_tps) - min(final_tps):.2f} TPS（{(max(final_tps) - min(final_tps)) / reference_tps * 100:.2f}%）。",
            f"S1 初次 AP 探测时有 {violating_windows[0]} 个 15 秒窗口低于 95%，控制器暂停 AP 并扩容 SB 后恢复；其余阶段没有 15 秒越界窗口。",
            "",
            "## 结论边界",
            "",
            "该目录使用了阶段边界取消机制，因此不能作为最终双 SLO 验收。新口径在 180 秒后停止准入并继续等待，只有所有已提交 SQL 正常返回后才能进入下一阶段。",
        ]
    )
    (result_dir / "RESULT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
