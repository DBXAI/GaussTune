#!/usr/bin/env python3
"""Innovation TWO: fresh pure forecasts for every query/WM, without allocation."""
import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql, extract_json
from jmt.common.machine import collect_machine
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.curves import build_spill_curves
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_2_spill.join_sample import JoinSampleProvider
from jmt.innovation_2_spill.key_group_sample import KeyGroupSample
from jmt.innovation_2_spill.scan_sample import ScanSampleProvider, CombinedCardinalityProvider
from export_ap_spill_curves import check_statistics


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--join-sample", type=Path)
    parser.add_argument("--key-sample", type=Path)
    parser.add_argument("--scan-sample", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    inputs_path, catalog_path = args.inputs / "INPUTS.json", args.inputs / "CATALOG.json"
    inputs, catalog = read(inputs_path), read(catalog_path)
    if inputs["database"] != catalog["database"]:
        raise ValueError("plan/catalog database mismatch")
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=30)
    if collect_catalog(client, inputs["database"])["catalog_snapshot_fingerprint"] != catalog["catalog_snapshot_fingerprint"]:
        raise ValueError("current catalog differs from blind plan inputs; recollect plans")
    statistics = {}
    for kind, path in (("join", args.join_sample), ("key", args.key_sample), ("scan", args.scan_sample)):
        if path:
            statistics[kind] = {"path": str(path.resolve()), "sha256": sha(path)}
    bindings, valid_from, expires = check_statistics(statistics, False)
    providers = {}
    constructors = {"join": JoinSampleProvider, "key": KeyGroupSample, "scan": ScanSampleProvider}
    for kind, item in bindings.items():
        bundle = read(Path(item["path"]))
        providers[kind] = constructors[kind](bundle, item["watermark_sha256"])
    cardinality = (CombinedCardinalityProvider(providers.get("join"), providers.get("scan"))
                   if "join" in providers or "scan" in providers else None)
    forecasts, grouped, sqls, sources = {}, {}, {}, [inputs_path, catalog_path]
    for item in bindings.values():
        path = Path(item["path"])
        sources.extend((path, path.parent / "WATERMARK_BEFORE.json", path.parent / "WATERMARK_AFTER.json"))
    code_paths = sorted((ROOT / "src/jmt/innovation_2_spill").glob("*.py"))
    code_paths = [p for p in code_paths if p.name != "allocation.py"]  # compatibility shim is NOT a predictor input
    code_paths.extend((Path(__file__).resolve(), ROOT / "tools/export_ap_spill_curves.py"))
    code_before = {str(p.relative_to(ROOT)): sha(p) for p in code_paths}
    started = time.perf_counter()
    for query in inputs["queries"]:
        q, path = query["query_id"], Path(query["SQL_path"])
        if sha(path) != query["SQL_sha256"]:
            raise ValueError("SQL changed since plan acquisition")
        sqls[q], grouped[q] = query["SQL_sha256"], []
        sources.append(path)
        session = ForecastSession(path.read_text(), catalog,
                                  cardinality_provider=cardinality, key_group_sample=providers.get("key"))
        for wm in range(inputs["grid_mib"], 4097, inputs["grid_mib"]):
            key = "q%d_wm%d" % (q, wm)
            raw_path = args.inputs / "plans" / (key + "_EXPLAIN_RAW.json")
            raw = read(raw_path)
            if (raw["exit_code"] or raw.get("blind") is not True
                    or raw.get("query_id") != q or raw.get("work_mem_mib") != wm
                    or raw.get("SQL_sha256") != sqls[q] or raw.get("database") != inputs["database"]):
                raise ValueError("failed/non-blind/unbound plan")
            f = session.forecast(extract_json(raw["stdout"]), wm)
            forecasts[key] = f
            grouped[q].append(f)
            sources.append(raw_path)
    curves = build_spill_curves(grouped, sqls)
    seconds = time.perf_counter() - started
    check_statistics(statistics, False)  # real clock and data watermark again; no renewal
    if collect_catalog(client, inputs["database"])["catalog_snapshot_fingerprint"] != catalog["catalog_snapshot_fingerprint"]:
        raise ValueError("catalog changed during forecasting")
    if code_before != {str(p.relative_to(ROOT)): sha(p) for p in code_paths}:
        raise ValueError("prediction code changed during curve preparation")
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    now = datetime.now().astimezone().isoformat()
    curves.update({
        "exported_at": now, "forecast_prepared_at": now,
        "existing_forecasts_reused_without_refit": False,
        "machine_fingerprint": machine["machine_fingerprint"], "machine": machine,
        "database": inputs["database"], "grid_mib": inputs["grid_mib"],
        "statistics": bindings, "valid_from_unix": valid_from, "expires_unix": expires,
        "current_inputs_revalidated": True, "machine_fingerprint_verified_on_export": True,
        "frozen_replay_only": False, "sample_TTL_renewed": False,
        "new_target_AP_execution_or_accuracy_holdout": False,
        "forecast_compute_seconds": seconds, "model_code_sha256": code_before,
        "source_artifacts": [{"path": str(p.resolve()), "sha256": sha(p)} for p in sources],
    })
    dump(args.out / "QUERY_FORECASTS.json", forecasts)
    dump(args.out / "SPILL_CURVES.json", curves)
    fields = ("query_id", "work_mem_mib", "logical_temp_estimate_complete", "logical_temp_read_bytes",
              "logical_temp_write_bytes", "logical_temp_read_plus_write_bytes", "structural_peak_mib")
    with (args.out / "SPILL_CURVES.csv").open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for curve in curves["queries"].values():
            for point in curve["points"]:
                writer.writerow({"query_id": curve["query_id"],
                                 **{k: point[k] for k in fields if k != "query_id"}})
    print(json.dumps({"queries": len(curves["queries"]),
                      "WM_points": sum(len(c["points"]) for c in curves["queries"].values()),
                      "forecast_compute_seconds": seconds, "contains_allocations": False,
                      "has_SB_or_budget_input": False}))


if __name__ == "__main__":
    main()
