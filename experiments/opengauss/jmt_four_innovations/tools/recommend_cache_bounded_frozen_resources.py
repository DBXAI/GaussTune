#!/usr/bin/env python3
"""Re-run the SAME joint engine with trace B_high and frozen AP forecasts.

Controlled component experiment: AP forecasts are frozen at a declared earlier
timestamp; cardinality TTL is NOT renewed and mixed-TPS accuracy is NOT claimed.
This avoids recalibrating unrelated components while repairing innovation 1.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"vendor"),str(ROOT/"tools")]
from jmt.innovation_1_cache.cache_boundary import load_boundary,file_sha
from jmt.innovation_3_search.current_joint import evaluate_current_joint
from jmt.innovation_3_search.search import QueryOption
from jmt.innovation_3_search.stage_spec import EXPECTED
from jmt.innovation_4_performance.preview_cost import solve_preview_joint_resources
from jmt.innovation_4_performance.capped_device import DeviceCap
from recommend_current_five_stages import surge_baselines,PROFILE,TOPOLOGY
from collect_current_tp_matrix import dump


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-resource-dir",type=Path,required=True)
    parser.add_argument("--tp-model",type=Path,required=True)
    parser.add_argument("--surge-matrix",type=Path,required=True)
    parser.add_argument("--tp-cache-boundary",type=Path,required=True)
    parser.add_argument("--device-saturation",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    report_path=args.prepared_resource_dir/"FIVE_STAGE_RECOMMENDATIONS.json"
    prepared=json.loads(report_path.read_text())
    freeze_path=args.prepared_resource_dir/"RESOURCE_OPTIONS_FREEZE.json"
    freeze=json.loads(freeze_path.read_text())
    details_path=args.prepared_resource_dir/"QUERY_OPTIONS.json"
    if (file_sha(details_path)!=freeze["query_options_sha256"]
            or file_sha(report_path)!=freeze["preparation_report_sha256"]
            or not prepared["innovation_1_cache_boundary_connected"]):
        raise ValueError("frozen AP preparation or PPT cache connection changed")
    for binding in prepared["source_artifacts"]:
        if file_sha(binding["path"])!=binding["sha256"]:
            raise ValueError("frozen resource source changed")
    tp=json.loads(args.tp_model.read_text());machine=tp["machine_fingerprint"]
    if machine!=prepared["machine_fingerprint"] or tp["benchmark_profile"]!=PROFILE:
        raise ValueError("TP/AP preparation identity differs")
    cache=load_boundary(args.tp_cache_boundary,machine_fingerprint=machine,profile=PROFILE,
                        require_miss_plateau=True)
    surge=surge_baselines(args.surge_matrix,machine)
    cap=DeviceCap.from_saturation(json.loads(args.device_saturation.read_text()))
    cpu=prepared["CPU_capacity_logical_CPUs_assumption_not_independent_effective_capacity"]
    details=json.loads(details_path.read_text())
    options={}
    for name,entry in details.items():
        query_name,wm_text=name.split("_wm")
        q,wm=int(query_name[1:]),int(wm_text)
        forecast,cost=entry["forecast"],entry["cost_preview"]
        io=cost["physical_IO_proxy"]
        options.setdefault(q,[]).append(QueryOption(
            q,wm,forecast["structural_peak_upper_mib"],io["read_requests"],
            io["write_requests"],cost["isolated_wall_seconds_estimate"],
            forecast["plan_structure_sha256"],cost["cpu_seconds_estimate"]))
    args.out.mkdir(parents=True,exist_ok=False)
    started=time.perf_counter();stages=[];previous={}
    for name,queries,terminals,base_n,surge_n in EXPECTED:
        baseline_rows=surge if surge_n else tp["rows"]
        topology="S5_128_plus_16" if surge_n else "TP128"
        b_high=cache["topologies"][topology]["boundary"]["B_high_mib"]
        fixed={q:wm for q,wm in previous.items() if q in queries}
        def predict(sb,baseline,state):
            demands=[]
            for q,wm in state.assignments:
                point=details["q%d_wm%d"%(q,wm)]["cost_preview"]
                demands.append({"cpu_seconds":point["cpu_seconds_estimate"],
                    "wall_seconds":point["isolated_wall_seconds_estimate"],
                    **point["physical_IO_proxy"]})
            return solve_preview_joint_resources(
                baseline_tps=baseline["sustainable_tps"],terminals=terminals,
                tp_cpu_seconds_per_tx=baseline["tp_cpu_seconds_per_tx"],
                tp_read_requests_per_tx=baseline["whole_device_read_events_per_tx_training_proxy"],
                tp_bytes_per_request=8192.,cpu_capacity=cpu,demands=demands,device_cap=cap)
        result=evaluate_current_joint(
            options_by_query={q:options[q] for q in queries},baseline_rows=baseline_rows,
            total_pool_mib=prepared["shared_SB_AP_pool_mib"],predict_tp=predict,
            fixed_running_work_mem=fixed,nonmonotone_resource_response=True,
            sb_upper_bound_mib=b_high)
        stages.append({"stage":name,"AP_queries":list(queries),
            "TP_baseline_terminals":base_n,"TP_surge_terminals":surge_n,
            "cache_topology":topology,"B_high_mib":b_high,
            "boundary_rule": cache["topologies"][topology]["boundary"],
            "preserved_running_WM":fixed,"result":result})
        if result["selected"] is None:
            break
        if result["selected"]["shared_buffers_mb"]>b_high:
            raise AssertionError("search violated replayed cache upper bound")
        previous=dict(result["selected"]["work_mem"])
    sources=[report_path,freeze_path,details_path,args.tp_model,args.surge_matrix,
             args.tp_cache_boundary,args.device_saturation]
    result={"schema":"jmt.cache-bounded-five-stage-component-preview/v1",
        "created_at":datetime.now().astimezone().isoformat(),
        "machine_fingerprint":machine,"stages":stages,
        "innovation_1_cache_boundary_connected":True,
        "B_high_is_derived_from_simulated_P_SB_not_TPS":True,
        "all_five_stages_have_numeric_recommendations":len(stages)==5 and all(
            s["result"]["selected"] for s in stages),
        "frozen_AP_preparation_time":prepared["created_at"],
        "frozen_AP_statistics_TTL_not_renewed":True,
        "not_a_claim_of_current_live_cardinality_refresh":True,
        "TP_response":str(args.tp_model.resolve()),"S5_topology":TOPOLOGY,
        "five_stage_search_seconds":time.perf_counter()-started,
        "physical_IO_is_proxy_not_lifecycle_truth":True,
        "CPU_capacity_is_logical_CPU_assumption":cpu,
        "shared_SB_AP_pool_mib":prepared["shared_SB_AP_pool_mib"],
        "pool_is_trial_policy_not_physical_safety_proof":True,
        "automatic_recommendation_qualified":False,"configuration_applied":False,
        "mixed_TPS_measured_for_these_new_configurations":False,
        "source_artifacts":[{"path":str(p.resolve()),"sha256":file_sha(p)} for p in sources]}
    dump(args.out/"FIVE_STAGE_RECOMMENDATIONS.json",result)
    print(json.dumps({"stages":[{"stage":s["stage"],"B_high_mib":s["B_high_mib"],
                                 "selected":s["result"]["selected"],
                                 "reason":s["result"].get("reason")} for s in stages],
                      "search_seconds":result["five_stage_search_seconds"]},indent=2),flush=True)


if __name__=="__main__":
    main()
