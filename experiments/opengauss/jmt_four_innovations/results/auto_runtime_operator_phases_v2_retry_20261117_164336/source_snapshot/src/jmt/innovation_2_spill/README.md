# 创新二：query × work_mem → AP 逻辑spill曲线

**本项只预测，不做SB/内存池预算决策，不分配WM，不使用未来S4统一规划。**
当前结果：仓库根目录 `SPILL_CURVES_AND_SB_FRONTIER_CURRENT.md`、
`CURRENT_AP_SPILL_CURVES.json`。

## 接口

- `ForecastSession(sql,catalog).forecast(blind_plan,work_mem_mib)`：纯预测；
  不连接DB，不执行目标query；输出算子、内存、逻辑临时读写和不确定性。
- `curves.build_spill_curves(forecasts_by_query,sql_sha256_by_query)`：汇集每个WM点，
  保留非单调曲线，输出R/W/R+W、结构峰值及分界点。无SB/pool/stage参数。
- 未知spill总量保持null，不补零，不据此宣称无spill。
- `wm_candidates.memory_anchors()`：提取Sort驻留需求、Hash构建需求、HashAgg
  spill-check需求、Materialize/WindowAgg需求，保留算子路径与来源计划；不按query编号设置参数。
- `wm_candidates.derive_candidates()`：从算子M派生0.5M/M/2M、阈值下邻点、
  Hash的M/2^k假设点；连续spill算子另加按自身M缩放的少量区间点。
  每个WM重新取盲EXPLAIN，发现新计划继续提取需求；对观测到的计划/算子转折有界细化。
  没有固定64MiB候选间距、没有4GiB模型上限，受实际引擎GUC合法域及计算次数限制。
- `curves.identify_boundaries()`：记录相邻已评估候选间的模式、batch、归并轮次、
  计划结构差异及尚未细化的区间；不声称覆盖连续域所有分界点。
- `scan_sample.py`、`join_sample.py`、`key_group_sample.py`：显式可失效的统计输入；
  重复块采样估完整谓词行数，单锚点Join估行数，完整键样本估分组频数。
  真实时钟、水印和TTL校验，不按query编号设置系数，不从行样本NDV直接除采样率。

## 实际入口

```bash
# 正式入口：算子派生候选 + 对应WM的真实盲计划 + 预测；不执行目标query。
python3 tools/predict_operator_spill_curves.py \
  --sql-dir <queries> --queries 2,9,13,18,21 --database h5_tpch \
  --join-sample <JOIN_SAMPLE_BUNDLE.json> --key-sample <KEY_GROUP_SAMPLE_BUNDLE.json> \
  --scan-sample <SCAN_SAMPLE_BUNDLE.json> --out <new_curves>
```

输出 `SPILL_CURVES.json/csv` 交给创新三；`QUERY_FORECASTS.json`保留算子证据。
`WM_CANDIDATES.json`逐点解释是哪个算子/需求/计划转折产生的候选，
以及计算上限、未处理提案、非穷尽覆盖限制。当前默认每query至多192次预测、
64次转折细化；连续型算子按自身M分成16段，不是全局固定WM网格。

WM取整分辨率是1MiB；第三项的DP预算记账粒度是另一件事，不决定候选WM。
排序驻留内存按元组/分配器/指针数组建模，NlogN属于CPU比较量，不能直接当内存。

`collect_ap_plan_grid.py` / `predict_ap_spill_curves.py` 保留为显式固定网格验证入口，
不是当前候选生成方法；其64..4096范围也不再限制正式候选。
另有 `export_ap_spill_curves.py` 支持核验后导出既有冻结预测，或显式历史回放。

## 历史兼容

`allocation.py`只剩兼容转发；DP算法已移到
`innovation_3_search/spill_allocator.py`。旧固定SB/cohort结果已撤销为主结果，
原JSON/源码快照保留用于审计；正常入口不再走旧 `run_innovation2_five_stages.py run`。

当前模型仍未通过独立跨WM验证；Q21/512的旧实测开发回放写spill仍低估约29.15%。
结构峰值不是实测物理内存上界，逻辑临时IO不是物理IO或延迟。
