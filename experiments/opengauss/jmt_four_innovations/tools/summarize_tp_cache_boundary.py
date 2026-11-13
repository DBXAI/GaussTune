#!/usr/bin/env python3
"""Document the real Trace -> simulated curve -> B_high -> joint-search chain."""
import argparse
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import statistics

ROOT=Path(__file__).resolve().parents[1]


def write_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",type=Path,required=True)
    parser.add_argument("--s5-directory",default="s5_small_domain_v2")
    args=parser.parse_args()
    run=args.run.resolve()
    cache=json.loads((run/"replay_v3/TP_CACHE_BOUNDARY.json").read_text())
    rec=json.loads((run/"five_stage_bounded/FIVE_STAGE_RECOMMENDATIONS.json").read_text())
    if not rec["all_five_stages_have_numeric_recommendations"]:
        raise ValueError("new bounded recommendation incomplete")
    observations={}
    for topology,name in (("TP128","tp_small_domain"),("S5_128_plus_16",args.s5_directory)):
        matrix=json.loads((run/name/"MATRIX.json").read_text())
        if not matrix["complete"] or not matrix["restoration_verified"]:
            raise ValueError("current TP calibration incomplete/unrestored")
        collected={}
        for row in matrix["rows"]:
            if topology=="TP128":
                hit=row["native_hit_ratio"]
            else:
                raw=json.loads(Path(row["window_path"]).read_text())
                before=raw["native_snapshot_before"]["value"]
                after=raw["native_snapshot_after"]["value"]
                hits=after["blks_hit"]-before["blks_hit"]
                reads=after["blks_read"]-before["blks_read"]
                if min(hits,reads)<0 or hits+reads<=0:
                    raise ValueError("native counters reset/no activity")
                hit=hits/(hits+reads)
            collected.setdefault(row["shared_buffers_mib"],[]).append(hit)
        comparisons=[]
        for sb,values in sorted(collected.items()):
            point=next(r for r in cache["topologies"][topology]["curve"]
                       if r["shared_buffers_mib"]==sb)
            mean=statistics.mean(values)
            error=abs(mean-point["p_sb"])
            comparisons.append({"shared_buffers_mib":sb,
                "frozen_simulated_P_SB":point["p_sb"],"native_repeat_P_SB":values,
                "observed_mean_P_SB":mean,"absolute_hit_error":error,
                "absolute_percentage_point_error":error*100,
                "threshold_abs":.002,"threshold_miss_relative":.20,
                "within_predeclared_tolerance":error<=max(.002,.2*(1-mean)),
                "native_counter_publication_is_asynchronous":True,
                "not_an_exact_per_access_or_physical_IO_holdout":True})
        observations[topology]=comparisons
    write_json(run/"INDEPENDENT_CAPACITY_COMPARISON.json",{
        "created_at":datetime.now().astimezone().isoformat(),
        "forecast_was_frozen_before_new_capacity_measurements":True,
        "new_observations_used_to_refit_curve_or_change_B_high":False,
        "topologies":observations,
        "all_within_predeclared_tolerance":all(
            row["within_predeclared_tolerance"] for rows in observations.values() for row in rows),
        "scope":"later real independent-capacity runs; asynchronously published native SB counters",
        "physical_OS_cache_and_disk_attribution_qualified":False})
    b1=cache["topologies"]["TP128"]["boundary"]["B_high_mib"]
    b5=cache["topologies"]["S5_128_plus_16"]["boundary"]["B_high_mib"]
    table=["# PPT 创新一已接通：真实 Trace → 多级缓存模拟 → B_high → 搜索剪枝\n\n",
        "## 结论\n\n",
        ("**TP128（S1～S4）的模拟上界为 %d MiB；S5 的 128 预热＋新增16为 %d MiB。**"%(b1,b5))+
        "这不是旧 TPS 平台，也不是固定填写的 SB；是同一条真实 TP 轨迹在多个虚拟 SB 下回放的结果。\n\n",
        "饱和判据：P_SB ≥ 该曲线最大 P_SB 的 99%，且所有更大已测模拟点也满足。"
        "判据继承原代码默认的 0.99；不是把“达到 99% 最大命中率”误写成“达到绝对 99% 命中率”。"
        "因此 B_high 是判据、网格和当前访问分布下的工程边界，不是数学上收益严格为零的精确点。\n\n",
        "## 模拟命中率曲线\n\n",
        "|SB MiB|TP128 P_SB|S5 P_SB|\n|---:|---:|---:|\n"]
    t1=cache["topologies"]["TP128"];t5=cache["topologies"]["S5_128_plus_16"]
    for a,b in zip(t1["curve"],t5["curve"]):
        table.append("|%d|%.4f%%|%.4f%%|\n"%(a["shared_buffers_mib"],100*a["p_sb"],100*b["p_sb"]))
    table+=["\n1 GiB 尚未达到约 98.996% 的相对饱和阈值，1.5 GiB 达到；"
            "两者之间没有强行插值一个更精确的“真拐点”。16 MiB 的微型缓存无可替换槽，"
            "该点标记失败并排除，未把部分回放计数当成有效命中率。\n\n",
            "## 实际容量和新独立容量验证\n\n",
            "源轨迹实际 SB=2048 MiB；回放预测与同一轨迹真实命中标记比较：\n\n",
            "|拓扑|预测 P_SB|观测 P_SB|绝对误差（百分点）|\n|---|---:|---:|---:|\n"]
    for label,t in (("TP128",t1),("128+16",t5)):
        v=t["actual_capacity_validation"]
        table.append("|%s|%.6f%%|%.6f%%|%.6f|\n"%(
            label,v["predicted_P_SB"]*100,v["sampled_observed_P_SB"]*100,
            v["absolute_hit_error"]*100))
    table+=["\n以下 512/1024/1536 MiB 均在模拟预测冻结之后，真正调整 SB，"
            "各跑三次；没有用结果回填或调整模拟曲线。这里采用原生发布计数器，"
            "其时间窗口非逐访问精确同步，不能升级为物理 IO 归因证明。\n\n",
            "|拓扑|SB MiB|冻结预测 P_SB|后续实测平均 P_SB|误差（百分点）|\n",
            "|---|---:|---:|---:|---:|\n"]
    for topology,rows in observations.items():
        for row in rows:
            table.append("|%s|%d|%.5f%%|%.5f%%|%.5f|\n"%(
                topology,row["shared_buffers_mib"],row["frozen_simulated_P_SB"]*100,
                row["observed_mean_P_SB"]*100,row["absolute_percentage_point_error"]))
    table+=["\n## 上界真正进入五阶段推荐\n\n",
        "原 2/4/6/8 GiB 全在模拟上界之外，新入口已实际拒绝这组旧域，"
        "没有回退到 2 GiB。随后补齐上界内 512/1024/1536 MiB 的 TP128 与真实 128+16 响应校准，"
        "再交给同一个联合搜索引擎。\n\n",
        "|阶段|模拟 B_high MiB|新推荐 SB MiB|WM（MiB）|模型 TPS（非新实测）|\n",
        "|---|---:|---:|---|---:|\n"]
    for stage in rec["stages"]:
        sel=stage["result"]["selected"]
        table.append("|%s|%d|%d|%s|%.2f|\n"%(
            stage["stage"],stage["B_high_mib"],sel["shared_buffers_mb"],
            "，".join("Q%d=%d"%(q,wm) for q,wm in sel["work_mem"]),sel["predicted_tps"]))
    table+=["\nAP 输入固定为 %s 已计算好的资源预测，以隔离检查创新一的接通；"
            "没有续期原直接采样 TTL，也不宣称此处重新刷新了当前基数统计。"
            "CPU/IO 阶段化桥接、Q21 spill 等已知问题仍在，不能把新模型 TPS 当成准确真值。\n\n"
            % rec["frozen_AP_preparation_time"],
        "## 轻量性与验证边界\n\n",
        "- 真实内核按页面空间哈希采样 2000 ppm（0.2%），保留被选页的全部事件，"
        "没有逐页面全量重放生产缓存。\n",
        "- 标准化后 %d 个事件；C++ 多容量主回放 %.3f 秒，"
        "与现有 Python 回放对真实前缀做计数逐项一致性检查。"
        "该时长不包括真实采集、标准化和参考验证。\n"%(cache["events"],cache["replay_seconds"]),
        "- 新五阶段联合搜索 %.4f 秒，沿用有界工作量限制。\n"%rec["five_stage_search_seconds"],
        "- P_OS/P_Disk 使用显式 8 GiB OS cache 的诊断假设、冷起点与轨迹预热；"
        "**不把这两个比例称为已经独立验准的物理 IO**。B_high 只取 P_SB，不受该 OS 容量影响。\n",
        "- 没有重启原实例；采集器、两种 TP 校准均验证原配置与活动 SB 恢复。\n",
        "- 当前 TP 数据仍为约 5.1GB、16×100万行，不是 PPT 20GB 数据。\n\n",
        "## 展示入口\n\n",
        "1. `tools/collect_tp_cache_curve_trace.py`：真实轨迹和 128→128+16。\n",
        "2. `tools/replay_tp_cache_boundary.py`：多级回放及模拟曲线。\n",
        "3. `src/jmt/innovation_1_cache/cache_boundary.py`：只用模拟 P_SB 求 B_high。\n",
        "4. `src/jmt/innovation_3_search/current_joint.py`：在调用预测器前强制排除 SB>B_high。\n",
        "5. `tools/recommend_current_five_stages.py --tp-cache-boundary ...`：正常路径，缺少缓存边界不能运行；"
        "`--legacy-unbounded-preview` 仅保留显式历史诊断。\n",
        "6. `tools/recommend_cache_bounded_frozen_resources.py`：本次固定 AP 输入的受控连接实验。\n"]
    (run/"CACHE_BOUNDARY_REPORT.md").write_text("".join(table))
    (ROOT/"TP_CACHE_BOUNDARY_CURRENT.md").write_text("".join(table))
    with (run/"CACHE_CURVE.csv").open("w",newline="") as stream:
        writer=csv.writer(stream);writer.writerow(["topology","SB_MiB","P_SB","P_OS","P_Disk"])
        for name,t in cache["topologies"].items():
            for row in t["curve"]:
                writer.writerow([name,row["shared_buffers_mib"],row["p_sb"],row["p_os"],row["p_disk"]])
    # Small repo-native SVG; no image-generation dependency.
    width,height=1000,520;left,right,top,bottom=85,960,55,440
    def x(sb):return left+(math.log2(sb)-5)/8*(right-left)
    def y(hit):return bottom-hit*(bottom-top)
    svg=['<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="520" viewBox="0 0 1000 520">',
         '<rect width="1000" height="520" fill="white"/>',
         '<style>text{font:14px sans-serif;fill:#263238}</style>',
         '<text x="85" y="28" style="font-size:20px">Real TP trace replay: P_SB vs virtual SB</text>']
    for tick in (0,.25,.5,.75,1):
        svg.append('<line x1="85" y1="%f" x2="960" y2="%f" stroke="#e4e9ee"/>'%(y(tick),y(tick)))
        svg.append('<text x="28" y="%f">%.0f%%</text>'%(y(tick)+5,tick*100))
    for sb in (32,64,128,256,512,1024,1536,2048,4096,8192):
        svg.append('<text x="%f" y="466" text-anchor="middle">%d</text>'%(x(sb),sb))
    for label,t,color in (("TP128",t1,"#1976d2"),("128+16",t5,"#e65100")):
        points=" ".join("%f,%f"%(x(r["shared_buffers_mib"]),y(r["p_sb"])) for r in t["curve"])
        svg.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2.5"/>'%(points,color))
    for b in sorted({b1,b5}):
        svg.append('<line x1="%f" y1="50" x2="%f" y2="440" stroke="#00897b" stroke-dasharray="6 4"/>'%(x(b),x(b)))
        svg.append('<text x="%f" y="78">B_high = %d MiB</text>'%(x(b)+8,b))
    svg.extend(['<text x="85" y="495">SB (MiB, log2 axis)</text>',
                '<text x="650" y="495" fill="#1976d2">Blue: TP128; Orange: 128+16</text>','</svg>'])
    (run/"CACHE_CURVE.svg").write_text("\n".join(svg))
    write_json(ROOT/"CURRENT_TP_CACHE_BOUNDARY.json",{
        "created_at":datetime.now().astimezone().isoformat(),
        "report":"TP_CACHE_BOUNDARY_CURRENT.md",
        "boundary":str((run/"replay_v3/TP_CACHE_BOUNDARY.json").relative_to(ROOT)),
        "five_stage_result":str((run/"five_stage_bounded/FIVE_STAGE_RECOMMENDATIONS.json").relative_to(ROOT)),
        "B_high_by_topology_mib":{k:v["boundary"]["B_high_mib"] for k,v in cache["topologies"].items()},
        "first_innovation_connected_to_search":True,
        "new_configuration_mixed_TPS_verified":False,
        "automatic_recommendation_qualified":False})
    print("".join(table))


if __name__=="__main__":
    main()
