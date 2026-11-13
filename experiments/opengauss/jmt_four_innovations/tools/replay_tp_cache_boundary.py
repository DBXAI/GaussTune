#!/usr/bin/env python3
"""Replay ONE current native TP trace at many virtual SBs, then derive B_high."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import asdict

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"),str(ROOT/"vendor"),str(ROOT/"tools")]
import numpy as np
from jmt.innovation_1_cache.kernel_trace import read_epoch, summary_dict, RECORD_DTYPE, FILE_RE
from jmt.innovation_1_cache.kernel_trace import replay_two_level_miniature, miniature_capacity
from jmt.innovation_1_cache.schema import TraceEvent, PageKey
from jmt.innovation_1_cache.cache_boundary import (
    SCHEMA, derive_miss_plateau_boundary, file_sha,
    DEFAULT_MAX_MISS_REDUCTION_PER_10000_PER_GIB, MISS_PLATEAU_CRITERION,
)
from collect_current_tp_matrix import dump

COMPACT = np.dtype([("ns","<u8"),("tid","<u4"),("kind","<u4"),("page","<i4"),
                    ("buffer","<i4"),("observed","<i4"),("pad","<u4")])


def compact_events(records, oid, output):
    selected = records[records["db_node"] == oid]
    if not len(selected):
        raise ValueError("no target database page events")
    accesses = selected["kind"] == 1
    if np.any(accesses & ~np.isin(selected["strategy_type"],(-1,0))):
        raise ValueError("bulk-read ring outside normal TP accelerator domain")
    if np.any(selected["kind"] == 6):
        raise ValueError("dirty pages outside read-only TP accelerator domain")
    # Canonical integer page IDs preserve identity, never observed cache slot.
    page_dtype = np.dtype([(name,selected.dtype.fields[name][0]) for name in
        ("spc_node","db_node","rel_node","bucket_node","fork_num","block_num")])
    pages = np.empty(len(selected),dtype=page_dtype)
    for name in page_dtype.names:
        pages[name]=selected[name]
    unique,ids=np.unique(pages,return_inverse=True)
    expanded=np.zeros(len(selected)+int(accesses.sum()),dtype=COMPACT)
    expanded["ns"][:len(selected)]=selected["start_ns"]
    for target,source in (("tid","tid"),("kind","kind"),("buffer","buffer_id"),
                          ("observed","observed_hit")):
        expanded[target][:len(selected)]=selected[source]
    expanded["page"][:len(selected)]=ids
    returned=selected[accesses]
    offset=len(selected)
    expanded["ns"][offset:]=returned["end_ns"]
    expanded["tid"][offset:]=returned["tid"]
    expanded["kind"][offset:]=8
    expanded["page"][offset:]=ids[accesses]
    expanded["buffer"][offset:]=returned["buffer_id"]
    expanded["observed"][offset:]=returned["observed_hit"]
    expanded=expanded[np.argsort(expanded["ns"],kind="stable")]
    expanded.tofile(str(output))
    return expanded,{"compact_events":len(expanded),"sampled_unique_pages":len(unique),
                     "native_target_records":len(selected),"compact_bytes":expanded.nbytes}


def compile_accelerator(output):
    source=ROOT/"src/jmt/innovation_1_cache/tp_mrc_replay.cpp"
    subprocess.run(["g++","-std=c++11","-O2","-Wall","-Wextra",str(source),"-o",str(output)],
                   check=True,capture_output=True,text=True)
    return source


def accelerate(binary,events_path,ppm,startup,os_mib,lists,windows,grid):
    argv=[str(binary),str(events_path),str(ppm),str(startup*128),str(os_mib*128),
          str(lists)]+[str(v) for pair in windows for v in pair]+[str(v) for v in grid]
    return json.loads(subprocess.check_output(argv,text=True))


def reference_events(rows,start,end):
    names={1:"ACCESS",2:"PIN",3:"PIN_LOCKED",5:"UNPIN_FINAL",7:"FLUSH",8:"RETURN"}
    pending_phases={}
    for i,row in enumerate(rows):
        ns=int(row["ns"])
        if ns>=end and int(row["kind"])!=8:
            continue
        kind=names[int(row["kind"])]
        phase="warmup" if ns<start else ("measure" if ns<end else "ignore")
        tid=int(row["tid"])
        if kind=="ACCESS":
            pending_phases.setdefault(tid,[]).append(phase)
        if kind=="RETURN":
            queue=pending_phases.get(tid,[])
            phase=queue.pop(0) if queue else "ignore"
        yield TraceEvent(
            seq=i+1,timestamp_ns=ns,backend_pid=tid,event=kind,phase=phase,
            page=PageKey(1,1,1,-1,0,int(row["page"])) if kind!="RETURN" else None,
            buffer_id=int(row["buffer"]),observed_hit=bool(row["observed"]) if kind=="RETURN" else None,
            strategy_type=0,strategy_id=0,ring_pages=0,workload_class="tp")


def verify_reference(binary,events_path,rows,ppm,startup,os_mib,lists,grid,out):
    # Differential check on REAL trace prefix, independent of synthetic tests.
    subset=rows[:min(len(rows),80000)]
    if len(subset)<1000:
        raise ValueError("insufficient real events for differential validation")
    path=out/"reference-prefix.events"
    subset.tofile(str(path))
    # Keep a tail beyond the scored endpoint so an ACCESS before the
    # boundary can retain its original phase on RETURN after the boundary.
    a=int(subset["ns"][0]);b=int(subset["ns"][len(subset)*3//4])
    middle=int(subset["ns"][len(subset)//2])
    result=accelerate(binary,path,ppm,startup,os_mib,lists,[(middle,b),(b,b+1)],grid)
    tids=[int(t) for t in np.unique(subset["tid"][subset["kind"]==1])]
    comparisons=[]
    for row in result["rows"]:
        if row.get("replay_error"):
            raise ValueError("differential prefix replay failed: "+row["replay_error"])
        sb=row["shared_buffers_mib"]
        ref=replay_two_level_miniature(
            reference_events(subset,middle,b),shared_buffer_pages=sb*128,
            os_cache_pages=os_mib*128,ppm=ppm,startup_shared_buffer_pages=startup*128,
            candidate_lists=lists,count_classes=("tp",),session_threads=tids)
        expected=ref["classes"]["tp"]
        got=row["windows"][0]
        binding={"shared_buffers_mib":sb,
                 "reference":expected,"accelerator":got,
                 "exact_count_match":all(expected[k]==got[k] for k in
                     ("accesses","sb_hits","os_hits","disk","observed_sb_hits"))}
        comparisons.append(binding)
        if not binding["exact_count_match"]:
            dump(out/"REFERENCE_EQUIVALENCE_FAILED.json",comparisons)
            raise ValueError("C++ accelerator differs from existing Python replay")
    dump(out/"REFERENCE_EQUIVALENCE.json",{"passed":True,"real_events":len(subset),
         "comparisons":comparisons,"scope":"real prefix; also covered by synthetic unit tests"})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--os-cache-mib",type=int,default=8192)
    parser.add_argument("--startup-shared-buffers-mib", type=int,
                        help="Startup maximum for candidate-list partitioning; otherwise read SB_RESTORE")
    parser.add_argument("--max-miss-reduction-per-10000-per-gib", type=float,
                        default=DEFAULT_MAX_MISS_REDUCTION_PER_10000_PER_GIB)
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    collection=json.loads((args.collection/"COLLECTION.json").read_text())
    policy=json.loads((args.collection/"POLICY.json").read_text())
    restore_path = args.collection/"SB_RESTORE.json"
    if args.startup_shared_buffers_mib is not None:
        startup = args.startup_shared_buffers_mib
    elif restore_path.is_file():
        restore = json.loads(restore_path.read_text())
        maximum_pages = int(restore["after"]["maximum_buffers"])
        if maximum_pages % 128:
            raise ValueError("recorded startup maximum is not an integer MiB")
        startup = maximum_pages//128
    else:
        raise ValueError("recorded SB_RESTORE or explicit --startup-shared-buffers-mib required")
    if startup <= 0 or args.os_cache_mib <= 0:
        raise ValueError("positive startup and diagnostic OS cache capacities required")
    if not collection["complete"] or not collection["restoration_verified"]:
        raise ValueError("trace collector incomplete or configuration not restored")
    epoch=collection["epoch"];ppm=policy["sample_ppm"]
    manifest_path=args.collection/"TRACE_FILES.json"
    manifest=json.loads(manifest_path.read_text())
    declared_names=[row["name"] for row in manifest["files"]]
    actual_names={p.name for p in (args.collection/"native").iterdir()
                  if FILE_RE.match(p.name) and int(FILE_RE.match(p.name).group("epoch"))==epoch}
    if (manifest["epoch"]!=epoch or len(declared_names)!=len(set(declared_names))
            or set(declared_names)!=actual_names
            or any(Path(name).name!=name for name in declared_names)):
        raise ValueError("native manifest and actual epoch file set differ")
    sources=[]
    for row in manifest["files"]:
        p=args.collection/"native"/row["name"]
        if p.stat().st_size!=row["bytes"] or file_sha(p)!=row["sha256"]:
            raise ValueError("native raw trace changed")
        sources.append({"path":str(p.resolve()),"sha256":row["sha256"]})
    records,summary=read_epoch(args.collection/"native",epoch)
    if summary.ppm!=ppm:
        raise ValueError("trace-header sampling rate differs from collection policy")
    dump(args.out/"EPOCH_INTEGRITY.json",summary_dict(summary))
    path=args.out/"tp.events"
    compact,metadata=compact_events(records,int(collection["settings"]["oid"]),path)
    dump(args.out/"COMPACT_TRACE.json",metadata)
    del records
    binary=args.out/"tp_mrc_replay"
    source=compile_accelerator(binary)
    windows=[]
    for name in ("TP128_TRACE_ON","S5_TRACE_ON"):
        p=args.collection/(name+".WINDOW.json")
        row=json.loads(p.read_text());w=row["measurement_window"]
        windows.append((w["start_ns"],w["end_ns"]))
        sources.append({"path":str(p.resolve()),"sha256":file_sha(p)})
    # Geometric extension below the original grid is declared before any
    # replay result, to ensure a rising-side bracket rather than a fake knee.
    grid=[16,32,64]+policy["cache_grid_mib"]
    dump(args.out/"REPLAY_POLICY.json",{
        "created_at":datetime.now().astimezone().isoformat(),"grid_mib":grid,
        "boundary_criterion": MISS_PLATEAU_CRITERION,
        "max_miss_reduction_per_10000_per_gib": args.max_miss_reduction_per_10000_per_gib,
        "endpoint_policy": "right endpoint of first negligible SB-miss reduction",
        "original_collection_boundary_criterion": policy.get("B_high_criterion"),
        "startup_shared_buffers_mib": startup,
        "extension_reason":"bracket below original minimum, not change saturation threshold",
        "OS_cache_mib":args.os_cache_mib,"OS_cache_is_explicit_diagnostic_assumption":True,
        "OS_initial_state":"cold with recorded warmup, not fitted from target labels",
        "physical_OS_IO_accuracy_qualified":False,
        "read_only_normal_strategy_only":True})
    lists=int(collection["settings"]["pagewriters"])
    if max(grid) > startup:
        raise ValueError("replay grid exceeds the recorded startup shared-buffer maximum")
    reference_domain=sorted({sb for sb in grid if 128<=sb<=startup})
    if len(reference_domain)<3:
        raise ValueError("need at least three supported capacities >=128MiB for reference validation")
    reference_grid={reference_domain[0],reference_domain[-1],
                    min(reference_domain,key=lambda sb:abs(sb-policy["source_SB_mib"]))}
    if len(reference_grid)<3:
        reference_grid.add(reference_domain[len(reference_domain)//2])
    verify_reference(binary,path,compact,ppm,startup,args.os_cache_mib,lists,
                     sorted(reference_grid),args.out)
    before=time.perf_counter()
    result=accelerate(binary,path,ppm,startup,args.os_cache_mib,lists,windows,grid)
    elapsed=time.perf_counter()-before
    dump(args.out/"REPLAY_COUNTS.json",result)
    topologies={}
    for index,name in enumerate(("TP128","S5_128_plus_16")):
        curve=[]
        failed_candidates=[]
        for row in result["rows"]:
            if row.get("replay_error"):
                failed_candidates.append({"shared_buffers_mib":row["shared_buffers_mib"],
                                          "reason":row["replay_error"],
                                          "partial_counts_not_used_as_hits":True})
                continue
            s=row["windows"][index];n=s["accesses"]
            if n<10000 or sum(s[k] for k in ("sb_hits","os_hits","disk"))!=n:
                raise ValueError("insufficient or nonpartitioned measured trace")
            curve.append({"shared_buffers_mib":row["shared_buffers_mib"],
                "p_sb":s["sb_hits"]/n,"p_os":s["os_hits"]/n,"p_disk":s["disk"]/n,
                "sampled_accesses":n,"observed_source_P_SB":s["observed_sb_hits"]/n,
                "measured_state_anomalies":s["anomalies"]})
        bound=derive_miss_plateau_boundary(curve,args.max_miss_reduction_per_10000_per_gib)
        actual=next(r for r in curve if r["shared_buffers_mib"]==policy["source_SB_mib"])
        truth=actual["observed_source_P_SB"];error=abs(actual["p_sb"]-truth)
        allowed=max(policy["actual_capacity_hit_abs_tolerance"],
                    policy["actual_capacity_miss_relative_tolerance"]*(1-truth))
        topologies[name]={"curve":curve,"boundary":bound,
            "failed_small_capacity_candidates":failed_candidates,"actual_capacity_validation":{
            "actual_SB_mib":policy["source_SB_mib"],"sampled_observed_P_SB":truth,
            "predicted_P_SB":actual["p_sb"],"absolute_hit_error":error,
            "allowed_absolute_error":allowed,"valid":error<=allowed,
            "not_independent_counterfactual_SB_validation":True}}
    for p in (args.collection/"POLICY.json",args.collection/"COLLECTION.json",manifest_path,
              source,args.out/"REFERENCE_EQUIVALENCE.json"):
        sources.append({"path":str(p.resolve()),"sha256":file_sha(p)})
    if restore_path.is_file():
        sources.append({"path":str(restore_path.resolve()),"sha256":file_sha(restore_path)})
    report={"schema":SCHEMA,"created_at":datetime.now().astimezone().isoformat(),
        "machine_fingerprint":collection["machine_fingerprint"],"TP_profile":policy["TP_profile"],
        "trace_integrity_verified":True,"reference_accelerator_equivalence_verified":True,
        "source_SB_mib":policy["source_SB_mib"],"source_epoch":epoch,"sample_ppm":ppm,
        "topologies":topologies,"source_artifacts":sources,
        "replay_seconds":elapsed,"events":result["events"],
        "physical_IO_prediction_qualified":False,
        "independent_capacity_validation_completed":False,
        "automatic_recommendation_qualified":False}
    dump(args.out/"TP_CACHE_BOUNDARY.json",report)
    print(json.dumps({k:{"B_high_mib":v["boundary"]["B_high_mib"],
                         "left_censored":v["boundary"]["left_censored"],
                         "actual_capacity_valid":v["actual_capacity_validation"]["valid"]}
                      for k,v in topologies.items()}),flush=True)
    print("replay_seconds=%.3f"%elapsed,flush=True)


if __name__=="__main__":
    main()
