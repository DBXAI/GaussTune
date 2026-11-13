#!/usr/bin/env python3
"""Publish the current miss-plateau upper bound and constrained trial result."""
import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.innovation_1_cache.cache_boundary import load_boundary, file_sha


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    run = args.run.resolve()
    cache_path = run / "boundary_v2/TP_CACHE_BOUNDARY.json"
    result_path = run / "five_stage_bounded_v2/FIVE_STAGE_RECOMMENDATIONS.json"
    result = json.loads(result_path.read_text())
    cache = load_boundary(cache_path, machine_fingerprint=result["machine_fingerprint"],
                          profile="current_16x1M_sysbench_readonly_omm_local_socket",
                          require_miss_plateau=True)
    if not result["all_five_stages_have_numeric_recommendations"]:
        raise ValueError("five-stage recomputation incomplete")
    bounds = {name: t["boundary"]["B_high_mib"] for name, t in cache["topologies"].items()}
    report = [
        "# 当前 SB 上界：第一个 SB miss 不再明显下降区间的右端点\n\n",
        "本次按用户指定的新定义，替换原“最大SB命中率的99%”规则。"
        "规则改动发生在查看本次轨迹之后，不能冒称阈值的事前独立验证。\n\n",
        "## 判据与结果\n\n",
        "令 m(B)=1−P_SB(B)，对相邻容量计算：\n\n"
        "```text\n"
        "每增加1 GiB减少的SB miss/万次访问\n"
        "  = max(0, m(B_left) − m(B_right)) × 10000 / ((B_right−B_left)/1024)\n"
        "```\n\n",
        "默认“无明显下降”阈值为 **不超过1次/万次逻辑访问/GiB**，"
        "由显式参数控制；并不是剩余miss相对下降不超过1%。"
        "取第一个满足条件区间的**右端点**，所以3→4 GiB满足就选4，不选3。"
        "若未观测到下降阶段，或者后面又出现显著收益，则拒绝把它作为自动硬剪枝证据。\n\n",
        "|拓扑|区间 GiB|每增1 GiB减少的SB miss/万次访问|是否无明显下降|\n"
        "|---|---:|---:|---|\n",
    ]
    for name, topology in cache["topologies"].items():
        for interval in topology["boundary"]["intervals"]:
            if 2048 <= interval["left_SB_mib"] <= 3072:
                report.append("|%s|%g→%g|%.6f|%s|\n" % (
                    name, interval["left_SB_mib"]/1024., interval["right_SB_mib"]/1024.,
                    interval["miss_reduction_per_10000_per_gib"],
                    "是" if interval["negligible_miss_reduction"] else "否"))
    report.append("\n**模拟上界：%s。** 没有将4096写成选择常量；"
                  "其它曲线也按相同参数和端点规则计算。\n\n" % "；".join(
                      "%s=%g GiB" % (name, value/1024.) for name, value in bounds.items()))
    report.extend([
        "## 接入联合搜索后的重新计算\n\n",
        "上界4 GiB只是允许的最大候选，不是强制将SB设成4 GiB。"
        "本次复用同机、同TP脚本/拓扑、已完成且恢复的原始校准窗口，"
        "合并不重叠SB域，保留每个点的前两次训练/第三次留出。"
        "采集时间和10/20秒原始窗口保持不变，没有把它们伪装成一次新的同步采集，"
        "也没有新增TP测量或插值3 GiB的TPS。\n\n",
        "|阶段|上界 MiB|重新选出的SB MiB|WM MiB|模型TPS，非新实测|\n"
        "|---|---:|---:|---|---:|\n",
    ])
    best_by_sb = []
    for stage in result["stages"]:
        selected = stage["result"]["selected"]
        bound = bounds[stage["cache_topology"]]
        if stage["B_high_mib"] != bound or selected["shared_buffers_mb"] > bound:
            raise ValueError("recommendation violated the new bound")
        if any(c["shared_buffers_mb"] > bound for c in stage["result"]["candidates"]):
            raise ValueError("out-of-bound point reached predictor")
        report.append("|%s|%d|%d|%s|%.2f|\n" % (
            stage["stage"], bound, selected["shared_buffers_mb"],
            "，".join("Q%d=%d" % (q, wm) for q, wm in selected["work_mem"]),
            selected["predicted_tps"]))
        best = {}
        for candidate in stage["result"]["candidates"]:
            sb = candidate["shared_buffers_mb"]
            best[sb] = max(best.get(sb, 0.), candidate["predicted_tps"])
        best_by_sb.extend({"stage": stage["stage"], "SB_mib": sb, "best_model_TPS": tps}
                          for sb, tps in sorted(best.items()))
    report.extend([
        "\n实际参加搜索的SB域为 **512、1024、1536、2048、4096 MiB**；"
        "原6144/8192 MiB点被剪枝，2/4 GiB确实参与了比较，不是固定填写推荐值。"
        "3 GiB已有模拟miss结果，但缺少本轮合并响应表的真实TPS点，未虚构补入。\n\n",
        "## 计算开销与验证边界\n\n",
        "- 上界规则只扫描有序容量点，无训练、无查询编号校正系数；"
        "本次规则计算 %.6f秒，不含原始轨迹采集/回放及证据哈希检查。\n"
        % cache["boundary_derivation_seconds"],
        "- 五阶段联合搜索 %.6f秒。\n" % result["five_stage_search_seconds"],
        "- AP资源预测固定于%s；直接采样TTL没有续期，未重新宣称当前实时基数刷新。\n"
        % result["frozen_AP_preparation_time"],
        "- CPU/IO桥接、Q21 spill和物理内存安全仍未完成独立验收。"
        "**这次是上界规则及搜索连接的重新试算，不是新配置混合TPS准确率验证或全域最优证明。**\n",
        "- 未修改实际SB或重启数据库；原始v1曲线、1.5 GiB试算及历史结果均保留。\n\n",
        "## 代码与复现\n\n",
        "1. `src/jmt/innovation_1_cache/cache_boundary.py`：`derive_miss_plateau_boundary()`。\n",
        "2. `tools/derive_tp_miss_plateau_boundary.py`：对已验真的冻结曲线应用新规则。\n",
        "3. `tools/replay_tp_cache_boundary.py`：新采集默认直接输出v2 miss边界。\n",
        "4. `src/jmt/innovation_3_search/search.py`：当前`find_b_high()`要求显式SB命中率；"
        "旧99%逻辑移至`find_b_high_legacy_hit_fraction()`，仅历史路径使用。\n",
        "5. 两个当前推荐入口均要求v2边界，拒绝旧99%产物，"
        "再由`current_joint.py`在调用预测器之前排除SB>B_high。\n",
    ])
    text = "".join(report)
    (run / "MISS_PLATEAU_REPORT.md").write_text(text)
    for name in ("TP_MISS_PLATEAU_CURRENT.md", "TP_CACHE_BOUNDARY_CURRENT.md",
                 "FIVE_STAGE_CURRENT_RECOMMENDATIONS.md"):
        (ROOT / name).write_text(text)
    with (run / "BEST_TPS_BY_SB.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("stage", "SB_mib", "best_model_TPS"))
        writer.writeheader()
        writer.writerows(best_by_sb)
    now = datetime.now().astimezone().isoformat()
    pointer = ROOT / "CURRENT_FIVE_STAGE_RECOMMENDATIONS.json"
    previous = run / "HISTORICAL_RECOMMENDATION_POINTER.json"
    if not previous.exists():
        previous.write_bytes(pointer.read_bytes())
    write(pointer, {
        "schema": "jmt.current-five-stage-recommendation-pointer/v1", "created_at": now,
        "report": "TP_MISS_PLATEAU_CURRENT.md",
        "result": str(result_path.relative_to(ROOT)), "result_sha256": file_sha(result_path),
        "kind": "experimental_joint_preview_with_user_requested_SB_miss_upper_bound",
        "all_five_stages_have_numeric_recommendations": True, "SB_is_optimized": True,
        "B_high_by_topology_mib": bounds, "shared_pool_mib": result["shared_SB_AP_pool_mib"],
        "configuration_applied": False, "new_TP_measurements": False,
        "automatic_recommendation_qualified": False,
    })
    write(ROOT / "CURRENT_TP_CACHE_BOUNDARY.json", {
        "created_at": now, "report": "TP_MISS_PLATEAU_CURRENT.md",
        "boundary": str(cache_path.relative_to(ROOT)), "boundary_sha256": file_sha(cache_path),
        "five_stage_result": str(result_path.relative_to(ROOT)),
        "B_high_by_topology_mib": bounds, "first_innovation_connected_to_search": True,
        "upper_bound_rule": cache["topologies"]["TP128"]["boundary"]["criterion"],
        "max_miss_reduction_per_10000_per_gib":
            cache["topologies"]["TP128"]["boundary"]["max_miss_reduction_per_10000_per_gib"],
        "rule_adopted_after_trace_inspection_at_user_request": True,
        "hard_upper_bound_saturation_qualified": False,
        "new_configuration_mixed_TPS_verified": False,
        "automatic_recommendation_qualified": False,
        "legacy_1536_mib_result_is_historical_only": True,
    })
    status_path = ROOT / "CANDIDATE_STATUS.json"
    status = json.loads(status_path.read_text())
    status.update({
        "last_updated_at": now, "latest_run_report": "TP_MISS_PLATEAU_CURRENT.md",
        "latest_run_directory": str(run.relative_to(ROOT)),
        "latest_recommendation_pointer": pointer.name,
        "latest_recommendation_kind": "experimental_preview_with_SB_miss_plateau_v2",
        "cache_upper_bound_rule_user_updated": True,
        "cache_upper_bound_rule": cache["topologies"]["TP128"]["boundary"]["criterion"],
        "cache_B_high_by_topology_mib": bounds,
        "cache_search_upper_bound_rule_verified": True,
        "cache_hard_upper_bound_saturation_qualified": False,
        "cache_upper_bound_review": "TP_MISS_PLATEAU_CURRENT.md",
        "latest_offline_regression": dict(json.loads((run / "REGRESSION_FULL.json").read_text()),
                                          path=str((run / "REGRESSION_FULL.json").relative_to(ROOT))),
    })
    write(status_path, status)
    print(json.dumps({
        "B_high_by_topology_mib": bounds,
        "selected_SB_by_stage_mib": {
            stage["stage"]: stage["result"]["selected"]["shared_buffers_mb"]
            for stage in result["stages"]},
        "report": "TP_MISS_PLATEAU_CURRENT.md",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
