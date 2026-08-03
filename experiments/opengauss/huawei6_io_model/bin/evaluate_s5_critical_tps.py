#!/usr/bin/env python3
"""Compare protected TP TPS during S5 with identical retained AP pressure."""
from __future__ import annotations
import argparse, csv, json, statistics
from pathlib import Path

def rows(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))

def device_pressure(io, start: float, end: float):
    stable=[x for x in io if start <= float(x["elapsed_seconds"]) < end]
    rates=[]
    for before, after in zip(stable, stable[1:]):
        seconds=float(after["elapsed_seconds"])-float(before["elapsed_seconds"])
        reads=int(after["read_ios"])-int(before["read_ios"])
        writes=int(after["write_ios"])-int(before["write_ios"])
        if seconds <= 0 or reads + writes <= 0:
            continue
        service_ms=(int(after["read_millis"])-int(before["read_millis"]) + int(after["write_millis"])-int(before["write_millis"])) / (reads + writes)
        rates.append({"iops":(reads + writes) / seconds,"service_ms":service_ms})
    if not rates:
        return {"device_iops":0.0,"device_service_ms":0.0}
    return {"device_iops":statistics.fmean(x["iops"] for x in rates),"device_service_ms":statistics.fmean(x["service_ms"] for x in rates)}

def s5(run: Path, warmup: float, tail: float):
    events=[json.loads(x) for x in (run/"events.jsonl").read_text().splitlines() if x]
    start=next(float(x["elapsed_seconds"]) for x in events if x.get("event")=="phase_enter" and x.get("stage")=="stage5_tp_surge")
    end=next(float(x["elapsed_seconds"]) for x in events if x.get("event")=="tp_injection_stop")
    data=[x for x in rows(run/"tp_tps_samples.csv") if x["stage"]=="stage5_tp_surge" and start+warmup <= float(x["elapsed_seconds"]) < end-tail]
    if not data: raise ValueError(f"no stable S5 samples in {run}")
    all_io=rows(run/"io_latency_samples.csv")
    io=[x for x in all_io if x["stage"]=="stage5_tp_surge"]
    profile=json.loads((run/"profile.json").read_text())
    events=[json.loads(x) for x in (run/"events.jsonl").read_text().splitlines() if x]
    completed=[x for x in events if x.get("event")=="ap_complete"]
    return {"run":str(run),"samples":len(data),"protected_target_tps":profile["protected_tp_rate"],"protected_tps":statistics.fmean(float(x["protected_tp_tps"]) for x in data),"total_tps":statistics.fmean(float(x["tp_tps"]) for x in data),"surge_tps":statistics.fmean(float(x["surge_tp_tps"]) for x in data),"mean_running_ap":statistics.fmean(float(x["running_ap"]) for x in io),"completed_ap":len(completed),"failed_ap":sum(int(x["return_code"]) != 0 for x in completed),**device_pressure(all_io,start+warmup,end-tail)}

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--sb4",type=Path,required=True); p.add_argument("--sb8",type=Path,required=True); p.add_argument("--out",type=Path,required=True); p.add_argument("--warmup",type=float,default=20); p.add_argument("--tail",type=float,default=2); p.add_argument("--material-gain-percent",type=float,default=3.0); a=p.parse_args()
    result={"s5_sb4":s5(a.sb4,a.warmup,a.tail),"s5_sb8":s5(a.sb8,a.warmup,a.tail)}
    target4=result["s5_sb4"]["protected_target_tps"]
    target8=result["s5_sb8"]["protected_target_tps"]
    if target4 != target8: raise ValueError("protected TP targets differ between candidates")
    result["protected_target_tps"]=target4
    result["protected_retention_sb4"]=result["s5_sb4"]["protected_tps"]/target4
    result["protected_retention_sb8"]=result["s5_sb8"]["protected_tps"]/target8
    result["protected_tps_gain_8_over_4"]=result["s5_sb8"]["protected_tps"]-result["s5_sb4"]["protected_tps"]
    result["protected_tps_gain_percent_8_over_4"]=100.0 * result["protected_tps_gain_8_over_4"] / result["s5_sb4"]["protected_tps"]
    comparable=(abs(result["s5_sb8"]["mean_running_ap"]-result["s5_sb4"]["mean_running_ap"]) < 0.1 and result["s5_sb4"]["failed_ap"] == 0 and result["s5_sb8"]["failed_ap"] == 0)
    result["validation"]={"same_active_ap_and_no_failures":comparable,"material_gain_percent":a.material_gain_percent,"sb8_validates_blinded_raise_recommendation":comparable and result["protected_tps_gain_percent_8_over_4"] >= a.material_gain_percent}
    a.out.parent.mkdir(parents=True,exist_ok=True); a.out.write_text(json.dumps(result,indent=2)+"\n"); print(json.dumps(result,indent=2))
if __name__=="__main__": main()
