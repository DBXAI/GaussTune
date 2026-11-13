#!/usr/bin/env python3
"""Summarize immutable mixed-load windows, without fitting to their labels."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    directory = args.result.resolve()
    result = json.loads((directory / "MIXED_TPS_RESULT.json").read_text())
    if not result["complete"] or not result["restoration_verified"]:
        raise ValueError("incomplete or unrestored run must not become current five-stage TPS")
    policy = json.loads((directory / "POLICY.json").read_text())
    frozen = directory / "FROZEN_RECOMMENDATION.json"
    if hashlib.sha256(frozen.read_bytes()).hexdigest() != result["recommendation_sha256"]:
        raise ValueError("frozen recommendation changed")
    recommendation = json.loads(frozen.read_text())
    enriched = []
    lines = [
        "# 当前魔改 openGauss：真实五阶段混合 TPS\n\n",
        "本报告是实际 sysbench + 原始完整 AP SQL 的同步内核窗口，不是 TP-only、"
        "预测值回放或旧窗口比例修正。\n\n",
        ("**范围：一次连续五阶段轨迹，每阶段 %d 个 %d 秒窗口；重复窗口不等于独立重复实验。"
         % (policy["repeats"], policy["window_seconds"])) +
        "AP 延续运行，Q2 在 S5 离开；S5 为已预热 128 + 在首窗口起点首次执行的 16 终端。"
        "本轮窗口结束后按计划取消 AP，不是完整 AP 生命周期验证。**\n\n",
        "|阶段|AP|TP|SB MiB|冻结预测 TPS|实测均值 TPS|实测窗口范围|预测相对实测偏差|\n",
        "|---|---|---|---:|---:|---:|---:|---:|\n"]
    for row, stage in zip(result["rows"], recommendation["stages"]):
        windows = [json.loads(path.read_text())
                   for path in sorted(directory.glob(row["stage"] + "_r*.WINDOW.json"))]
        if len(windows) != policy["repeats"]:
            raise ValueError("missing window")
        values = [w["observed_committed_TPS"] for w in windows]
        if values != row["repeat_TPS"]:
            raise ValueError("raw kernel-window TPS differs from stage summary")
        if any(w["AP_work_mem_mib"] != {str(q): wm for q, wm in row["work_mem"]}
               for w in windows):
            raise ValueError("AP work_mem differs from selected frozen configuration")
        cpu = [sum(w["AP_CPU_seconds"].values()) / w["scored_seconds"] for w in windows]
        tx = sum(w["committed_transactions"] for w in windows)
        tp_cpu_ms = sum(w["TP_CPU_seconds"] for w in windows) / tx * 1000
        selected = stage["result"]["selected"]
        demand = {"stage": row["stage"],
                  "frozen_predicted_AP_CPU_cores":
                      selected["prediction_details"]["ap_cpu_seconds_per_second_after_media_closure"],
                  "observed_window_AP_CPU_cores": cpu,
                  "observed_mean_AP_CPU_cores": statistics.mean(cpu),
                  "observed_TP_backend_CPU_ms_per_tx": tp_cpu_ms,
                  "per_window_query_CPU_seconds": [w["AP_CPU_seconds"] for w in windows],
                  "CPU_endpoint_scope":
                      "schedstat bracket endpoints, not exact kernel clock event boundaries",
                  "predicted_lifetime_average_vs_actual_phase_scope_differs": True}
        enriched.append({**row, "CPU_diagnosis": demand})
        lines.append("|%s|%s|%s|%d|%.2f|%.2f|%.2f–%.2f|%+.2f%%|\n" % (
            row["stage"], ",".join(map(str, row["AP_queries"])),
            "128+16" if row["stage"] == "S5" else "128",
            row["shared_buffers_mib"], row["frozen_predicted_TPS"],
            row["observed_mean_TPS"], row["observed_min_TPS"], row["observed_max_TPS"],
            row["prediction_minus_actual_relative"] * 100))
    resources = json.loads((directory / "RESOURCE_SAMPLES.json").read_text())
    lines += [
        "\n## 中间资源：不把全查询平均值当成瞬时阶段真值\n\n",
        "|阶段|旧模型闭环 AP CPU（核）|实测窗口平均 AP CPU（核）|TP 后端 CPU ms/事务|\n",
        "|---|---:|---:|---:|\n"]
    for row in enriched:
        d = row["CPU_diagnosis"]
        lines.append("|%s|%.3f|%.3f|%.4f|\n" % (
            row["stage"], d["frozen_predicted_AP_CPU_cores"],
            d["observed_mean_AP_CPU_cores"], d["observed_TP_backend_CPU_ms_per_tx"]))
    lines += [
        "\n这一对比暴露了生命周期平均 CPU/IO 桥接与当前算子阶段负载之间的差异；"
        "它不是同口径 CPU 真值误差，也不能单凭本轮证明 TPS 偏差的唯一原因。"
        "模型尚未显式覆盖 sysbench 全客户端线程树、后台 CPU 与有效 CPU 容量变化。"
        "不能乘一个阶段/查询系数把它“修准”。下一步应使用可复用算子阶段资源需求"
        "与全机有效 CPU 预算做轻量闭环，再做独立窗口验证。\n\n",
        "## 实验和恢复\n\n",
        "- 测量前 TP-only 同 SB 控制：%.2f TPS；它不是任何阶段的混合 TPS，"
        "也不是同时采集的因果对照。\n" % result["TP_only_control_before_TPS"],
        "- 原配置字节级恢复、原 postmaster 未重启；活动 SB 恢复到原值。\n",
        "- 实测使用原始完整 SQL，query_dop=1，vector engine off，"
        "只读事务；没有重置计数器、修改表、删数据或 drop cache。\n",
        "- 同一个 [start,end) 单调时钟内核窗口记录 TP 提交和整盘 IO；"
        "整盘 IO 不是 TP/AP 独占物理归因，探针开销也未完成成对独立标定。\n",
        "- 样本最低 MemAvailable %.2f GiB、最低磁盘可用 %.2f GiB；"
        "这只是采样范围，不是完整生命周期物理内存安全证明。\n" % (
            min(x["MemAvailable_bytes"] for x in resources) / 2**30,
            min(x["disk_free_bytes"] for x in resources) / 2**30),
        "- 三个连续窗口不是三个独立完整实验；没有完成候选 SB/WM 的随机顺序对照，"
        "**因此不能把本次推荐称为实测最优配置。**\n\n",
        "## 文件\n\n",
        "- `results/%s/MIXED_TPS_RESULT.json`\n" % directory.name,
        "- `results/%s/S*_r*.WINDOW.json`：每窗口真实提交、CPU、IO、AP 成员和时间。\n" % directory.name,
        "- `results/%s/DRIVER_VALIDATION.json`：sysbench 间隔错误检查，"
        "计划停止缺失的最终汇总不伪装为 0 错误。\n" % directory.name,
        "- `results/%s/SB_RESTORE.json`：恢复与新增 resize commit 证据。\n" % directory.name]
    report = "".join(lines)
    (directory / "MIXED_TPS_REPORT.md").write_text(report)
    (ROOT / "MIXED_TPS_CURRENT.md").write_text(report)
    write_json(directory / "INTERMEDIATE_RESOURCE_COMPARISON.json", {
        "schema": "jmt.mixed-window-resource-diagnosis/v1", "rows": enriched,
        "after_execution_diagnostic_not_refit": True,
        "full_AP_lifecycle_validation": False, "automatic_recommendation_qualified": False})
    relative = directory.relative_to(ROOT)
    write_json(ROOT / "CURRENT_MIXED_TPS.json", {
        "created_at": datetime.now().astimezone().isoformat(),
        "report": "MIXED_TPS_CURRENT.md",
        "result": str(relative / "MIXED_TPS_RESULT.json"),
        "result_sha256": hashlib.sha256((directory / "MIXED_TPS_RESULT.json").read_bytes()).hexdigest(),
        "all_five_stages_measured": True, "restoration_verified": True,
        "scope": "continuous_trajectory_AP_overlap_windows_not_full_lifecycles",
        "configuration_optimality_validated": False,
        "automatic_recommendation_qualified": False})
    status_path = ROOT / "CANDIDATE_STATUS.json"
    status = json.loads(status_path.read_text())
    status.update({"last_updated_at": datetime.now().astimezone().isoformat(),
                   "real_five_stage_mixed_TPS_windows_completed": True,
                   "latest_real_mixed_TPS_pointer": "CURRENT_MIXED_TPS.json",
                   "full_five_stage_AP_lifecycle_validated": False,
                   "recommended_configuration_empirically_optimal": False})
    write_json(status_path, status)
    print(report)


if __name__ == "__main__":
    main()
