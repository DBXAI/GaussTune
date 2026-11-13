"""Publish the new per-SB planning run; do not overwrite prediction labels."""
from datetime import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = Path(__file__).resolve().parent


def read(p):
    return json.loads(p.read_text())


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def relative(p):
    return str(p.relative_to(ROOT))


def write(p, value):
    p.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


current_path = RUN / "current_instance_v2/SB_SPILL_FRONTIER.json"
host_path = RUN / "host_capacity_v2/SB_SPILL_FRONTIER.json"
memory_path = RUN / "memory_v1/MEMORY_BUDGET.json"
current, host, memory = read(current_path), read(host_path), read(memory_path)
curve_pointer = read(ROOT / "CURRENT_AP_SPILL_CURVES.json")
curve_path = ROOT / curve_pointer["result"]
assert sha(curve_path) == curve_pointer["result_sha256"]
curves = read(curve_path)
observation = read(Path(memory["observation_path"]))
explanation = read(RUN / "host_capacity_v2/FRONTIER_EXPLANATION.json")
regression = read(RUN / "FULL_MODEL_REGRESSION.json")
now = datetime.now().astimezone().isoformat()

write(ROOT / "CURRENT_SB_SPILL_FRONTIER.json", {
    "schema": "jmt.current-SB-spill-frontier-pointer/v1",
    "result": relative(current_path), "result_sha256": sha(current_path),
    "report": "INNOVATION3_CURRENT.md", "budget_scenario": "current_instance",
    "host_capacity_planning_scenario": relative(host_path),
    "host_capacity_planning_scenario_sha256": sha(host_path),
    "host_capacity_scenario_is_not_currently_executable": True,
    "memory_budget_pointer": "CURRENT_MEMORY_BUDGET.json",
    "independent_unpruned_audit": relative(RUN / "FINAL_AUDIT_CURRENT_UNPRUNED.json"),
    "host_independent_unpruned_audit": relative(RUN / "FINAL_AUDIT_HOST_UNPRUNED.json"),
    "SB_upper_bound_mib": 4096, "objective": "per-SB minimum total AP logical spill",
    "snapshot_planning_passed": True, "greedy_continuous_five_stage_admission_verified": False,
    "greedy_pinned_counterexample_first_blocked_stage_at_SB4096": "S3",
    "final_SB_or_TPS_optimum_selected": False, "configuration_applied": False,
})
write(ROOT / "CURRENT_MEMORY_BUDGET.json", {
    "schema": "jmt.current-memory-budget-pointer/v1",
    "result": relative(memory_path), "result_sha256": sha(memory_path),
    "observation_at": observation["created_at"], "report": "INNOVATION3_CURRENT.md",
    "host_capacity_SB_AP_pool_mib": memory["scenarios"]["host_after_reserve"]["shared_pool_mib"],
    "current_instance_SB_AP_pool_mib": memory["scenarios"]["current_instance"]["shared_pool_mib"],
    "current_instance_AP_dynamic_cap_mib": memory["scenarios"]["current_instance"]["AP_dynamic_hard_cap_mib"],
    "fixed_16_GiB_pool_used": False,
    "host_capacity_scenario_requires_native_configuration_and_protector_changes": True,
    "normal_planning_requires_fresh_census_not_a_permanent_memory_constant": True,
    "physical_safety_validated": False, "configuration_applied": False,
})
lines = [
    "# 创新三：逐SB最小spill链路已跑通；不等于连续五阶段真机验收",
    "",
    "整理时间：" + now + "。",
    "正式搜索时间：" + current["created_at"] + " / " + host["created_at"] + "。",
    "创新二预测仍为 " + curves["forecast_prepared_at"] + " 的算子派生曲线，未改写、未重训、未续期。",
    "",
    "## 1. 本轮具体接通与检查",
    "",
    "1. 重新采集机器/TP-only公共内存，替代已超过15分钟有效窗口的旧内存观测。",
    "2. 从创新一读取TP128和128+16的SB miss平台边界，均为4096MiB。"
    "13个已有Trace容量点中，6144/8192MiB在DP前剔除，留下11点。",
    "3. 接入创新二491个算子派生WM点，只消费曲线，不在第三项修改预测或加入query系数。",
    "4. 每个SB按pool−SB及原生动态保护计算AP预算；当前阶段AP集合求Σ逻辑临时读+写最小的DP。",
    "5. 两场景各55点全有可行解。另用未剪枝、未调用生产solver的参考DP检查110点，全部一致；"
    "较小S1/S2还做了全部候选笛卡尔枚举核对。",
    "6. 正常入口新增源文件哈希、当前机器、catalog、数据/统计水印复核。"
    "以前只检查TTL是不足的：未到期也可能已经换数据、统计或执行器。",
    "7. 修复运行中WM pins的bool被Python当成整数1的问题；非法pins现在拒绝。",
    "",
    "## 2. 搜索域与剪枝",
    "",
    "SB候选为32/64/128/256/512/768/1024/1536/2048/3072/4096MiB，均有创新一模拟证据。"
    "小容量模拟点不表示其TP表现已合格或已做本轮原生resize验收。",
    "",
    "| Query | 创新二候选数 | 完整且满足显式试验策略 | 安全Pareto剪枝后 |",
    "|---|---:|---:|---:|",
]
for q, row in explanation["query_candidate_pruning"].items():
    lines.append("| Q%s | %d | %d | %d |" % (
        q, row["curve_points"], row["complete_admissible_points"], row["after_query_Pareto"]))
lines += [
    "",
    "Q9/512临时文件上限失败、Q18/512超时的同SQL证据，只作为下一次试验保守排除≤512MiB的显式策略。"
    "不是失败单调性或该区间实际spill已验证的证明。",
    "491点先按上述策略保留365点，再做每query记账内存/IO支配剪枝保留129点；"
    "DP最优性仅限该候选域及向上取整的预算口径。",
    "",
    "默认每query记账=ceil(max(session WM,预测结构峰值)/16)*16MiB；"
    "16MiB是预算单元，不是WM候选间距。未知IO不能当零。",
    "AP需求施加的是SB的可行上约束，不能直接冒充TP缓存下界。"
    "本轮没有捏造已合格TP B_low，也没有把最小AP spill的SB称为最高TPS。",
    "",
    "## 3. 当前实例场景：数值通，但原生动态保护卡住了SB与WM的联动",
    "",
    "内存观测时间：" + observation["created_at"] + "。"
    "当前max_process_memory仍为20GiB，max_dynamic_memory为%dMiB；"%memory["current_max_dynamic_memory_mib"],
    "TP/公共动态预留%dMiB，所以AP动态硬预算为%dMiB。"%(
        memory["TP_and_common_dynamic_reserve_mib"],
        memory["scenarios"]["current_instance"]["AP_dynamic_hard_cap_mib"]),
    "所有11个候选SB下都触及同一动态上限，因此AP预算、WM分配和最小spill相同。"
    "这不是DP忘记减SB，也不是可以随便选任何SB：TP缓存收益还要由后续性能层评价。",
    "",
    "| 阶段 | 当前AP集合 | WM分配MiB | 预测spill读+写GiB |",
    "|---|---|---|---:|",
]
for stage in current["stages"]:
    row = next(r for r in stage["per_SB_minimum_spill"] if r["SB_mib"] == 4096)
    selection = row["selection"]
    lines.append("| %s | %s | %s | %.4f |" % (
        stage["stage"], ",".join("Q%d" % q for q in stage["AP_queries"]),
        ", ".join("Q%s=%d" % x for x in selection["work_mem_mib"].items()),
        selection["logical_temp_read_plus_write_bytes"] / 2**30))
lines += [
    "",
    "## 4. 按机器余量规划：能看到正确的SB–WM竞争趋势",
    "",
    "有效容量%.3fMiB；OS/安全预留%dMiB；数据库非SB固定预留%dMiB；TP/公共动态预留%dMiB。"
    % (memory["effective_capacity_mib"], memory["host_reserve_mib"],
       memory["database_fixed_reserve_mib"], memory["TP_and_common_dynamic_reserve_mib"]),
    "剩余SB+AP池%dMiB（%.3fGiB），不是固定16GiB。"
    % (host["shared_pool_mib"], host["shared_pool_mib"] / 1024),
    "**此场景尚不能直接用于当前实例**：需要扩大max_process_memory，"
    "并正确初始化/联动maxChunksPerProcess。SB尾页MADV_REMOVE本身不会自动提高原生动态上限。",
    "",
    "以下单位均为整query逻辑临时读+写GiB，不是实测物理磁盘IO：",
    "",
    "| SB GiB | 剩余AP预算GiB | S1 | S2 | S3 | S4 | S5 |",
    "|---:|---:|---:|---:|---:|---:|---:|",
]
for sb in (1024, 2048, 3072, 4096):
    rows = [next(r for r in s["per_SB_minimum_spill"] if r["SB_mib"] == sb) for s in host["stages"]]
    lines.append("| %d | %.3f | %s |" % (
        sb // 1024, rows[0]["AP_budget_mib"] / 1024,
        " | ".join("%.4f" % (r["selection"]["logical_temp_read_plus_write_bytes"] / 2**30) for r in rows)))
lines += [
    "",
    "例如SB=4GiB的配置规划快照：",
    "",
    "| 阶段 | WM分配MiB | 取整记账MiB / AP预算MiB |",
    "|---|---|---:|",
]
for stage in host["stages"]:
    row = next(r for r in stage["per_SB_minimum_spill"] if r["SB_mib"] == 4096)
    s = row["selection"]
    lines.append("| %s | %s | %d / %d |" % (
        stage["stage"], ", ".join("Q%s=%d" % x for x in s["work_mem_mib"].items()),
        s["rounded_reservation_mib"], row["AP_budget_mib"]))
lines += [
    "",
    "S4与S3的部分spill相同是因为加入的Q2可选零spill点且仍能放进预算；"
    "不表示遗漏Q2，它的结构内存已记账。S5和S3同AP集合、同预算会复用DP，TP拓扑仍分别保留。",
    "",
    "## 5. 额外检查：不能直接把独立阶段最优表串成连续运行",
    "",
    "本轮还做了明确的反事实准入检查：固定SB=4GiB，S1按当前阶段最优启动Q18；"
    "后续重叠query一直运行，WM保持启动值，不把未来S4集合强塞进前一阶段。",
    "",
    "| 场景 | S1启动Q18 WM | S2保持Q18后选择Q21 WM | 首个无法继续准入的阶段 |",
    "|---|---:|---:|---|",
]
for name, filename in (("当前实例", "CONTINUITY_CURRENT_SB4096.json"),
                       ("机器余量规划", "CONTINUITY_HOST_SB4096.json")):
    d = read(RUN / filename)
    lines.append("| %s | %d | %d | %s |" % (
        name, d["events"][0]["selection"]["work_mem_mib"]["18"],
        d["events"][1]["selection"]["work_mem_mib"]["21"], d["first_infeasible_stage"]))
lines += [
    "",
    "**原因：前期按当前集合最小spill会给Q18较多内存，后续新query加入时，不能直接降低正在运行的Q18 WM。**"
    "因此独立阶段都有可行解，不等于这种贪心连续启动策略也有可行解。",
    "这是特定策略的反例，不是证明所有在线策略都不可能。"
    "真实连续执行需要显式准入/等待、受约束的初始预留策略，或真正经过验证的运行中grant联动。"
    "本轮未伪装成已经完成，也未偷偷改变创新三的默认目标或当前AP集合。",
    "",
    "## 6. 耗时、验证范围、剩余限制",
    "",
    "- 当前实例搜索%.4fs，因预算相同和重复AP集合只需%d次DP。"%(
        current["compute_seconds"], current["DP_calls_after_duplicate_stage_cache"]),
    "- 机器余量规划搜索%.4fs，共%d次DP；不重新执行AP或训练DeepDB。"%(
        host["compute_seconds"], host["DP_calls_after_duplicate_stage_cache"]),
    "- 全部%d项回归通过；两场景各55点的未剪枝独立最优核对与标准库回放通过。"%regression["tests_run"],
    "- 正常入口实际拒绝了20:43的旧内存观测，不靠续时间戳或frozen模式冒充当前结果。",
    "- **Q21/512旧开发回放spill仍低估约29.16%；第三项求解正确不能替代第二项准确率验证。**",
    "- 未实测新配置AP生命周期/物理峰值/实际grant/混合TPS；没有重启、resize或应用推荐。",
    "- 所有IO下降均为模型结果，不是已观察到的性能收益。",
    "",
    "## 7. 结果与展示入口",
    "",
    "- 当前实例：`" + relative(current_path) + "`。",
    "- 机器余量规划：`" + relative(host_path) + "`。",
    "- 每query预算/IO、边界与剪枝解释：各目录`FRONTIER_EXPLANATION.json`。",
    "- 标准库冻结回放：`tools/replay_sb_spill_budgets.py`。",
    "- 不调用生产solver的核对：`tools/audit_sb_spill_frontier.py`。",
    "- 不改运行中WM的反例检查：`tools/audit_five_stage_continuity.py`。",
    "- 原始命令和复现说明：`" + relative(RUN / "REPRODUCE.md") + "`。",
]
(ROOT / "INNOVATION3_CURRENT.md").write_text("\n".join(lines) + "\n")
status = read(ROOT / "CANDIDATE_STATUS.json")
status.update({
    "last_updated_at": now, "latest_run_report": "INNOVATION3_CURRENT.md",
    "latest_run_directory": relative(RUN),
    "latest_offline_regression": regression, "latest_innovation3_pointer": "CURRENT_SB_SPILL_FRONTIER.json",
    "latest_innovation3_report": "INNOVATION3_CURRENT.md",
    "innovation3_five_stage_snapshot_per_SB_optimization_passed": True,
    "innovation3_all_110_points_match_independent_unpruned_optimum": True,
    "innovation3_live_curve_sources_catalog_machine_watermarks_rechecked": True,
    "innovation3_stale_memory_observation_rejected": True,
    "innovation3_current_instance_AP_cap_mib": current["AP_dynamic_hard_cap_mib"],
    "current_instance_AP_dynamic_budget_cap_mib": current["AP_dynamic_hard_cap_mib"],
    "host_capacity_pool_mib": host["shared_pool_mib"],
    "innovation3_greedy_pinned_SB4096_continuity_first_infeasible_stage": "S3",
    "innovation3_greedy_continuous_five_stage_admission_passed": False,
    "host_capacity_pool_currently_executable": False,
    "new_selected_WM_AP_execution_validated": False,
    "latest_recommendation_kind": "validated-per-SB-snapshot-spill-frontiers_NOT_a_continuous-live-or-TPS-optimal-recommendation",
    "latest_recommendation_pointer": "CURRENT_SB_SPILL_FRONTIER.json",
})
write(ROOT / "CANDIDATE_STATUS.json", status)
print(json.dumps({"report": "INNOVATION3_CURRENT.md", "snapshot_planning_passed": True,
                  "continuous_live_execution_verified": False, "configuration_applied": False},
                 ensure_ascii=False, indent=2))
