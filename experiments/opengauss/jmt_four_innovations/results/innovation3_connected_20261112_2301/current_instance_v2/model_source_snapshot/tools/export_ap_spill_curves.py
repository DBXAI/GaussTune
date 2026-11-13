#!/usr/bin/env python3
"""Export innovation TWO's query/WM spill curves, without SB or allocation."""
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
from jmt.innovation_2_spill.curves import build_spill_curves


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def check_statistics(statistics, frozen):
    """Revalidate provenance/TTL/watermarks; NEVER rewrite acquisition times."""
    from jmt.innovation_2_spill.group_synopsis import data_watermark_sql, watermark
    from jmt.innovation_2_spill.join_sample import JoinSampleProvider
    from jmt.innovation_2_spill.key_group_sample import KeyGroupSample
    from jmt.innovation_2_spill.scan_sample import ScanSampleProvider
    if not frozen:
        from jmt.common.db import Gsql
        client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=30)
    bindings, generations, expirations = {}, [], []
    for kind, old in statistics.items():
        path = Path(old["path"])
        if sha(path) != old["sha256"]:
            raise ValueError("source sample changed")
        bundle = read(path)
        before, after = read(path.parent / "WATERMARK_BEFORE.json"), read(path.parent / "WATERMARK_AFTER.json")
        if not before["sha256"] == after["sha256"] == bundle["watermark_sha256"]:
            raise ValueError("sample acquisition watermark mismatch")
        if not 0 < bundle["expires_unix"] - bundle["generated_unix"] <= 21600:
            raise ValueError("sample TTL must remain <=6 hours")
        if kind == "join":
            profiles = [{**r, "column": "unused"} for e in bundle["entries"] for r in e["scope"]["relations"]]
            constructor = JoinSampleProvider
        elif kind == "scan":
            profiles = [{**e["scope"], "column": "unused"} for e in bundle["entries"]]
            constructor = ScanSampleProvider
        elif kind == "key":
            profiles, constructor = [bundle["profile"]], KeyGroupSample
        else:
            raise ValueError("unsupported statistics kind")
        live = None
        if not frozen:
            live = watermark(client.json(bundle["database"], data_watermark_sql(profiles)))
            if live["sha256"] != bundle["watermark_sha256"]:
                raise ValueError("current data/statistics watermark changed")
            constructor(bundle, live["sha256"])  # validates raw counts, eligibility and actual clock
        generations.append(bundle["generated_unix"])
        expirations.append(bundle["expires_unix"])
        bindings[kind] = {
            "path": str(path.resolve()), "sha256": sha(path),
            "generated_unix": bundle["generated_unix"], "expires_unix": bundle["expires_unix"],
            "watermark_sha256": bundle["watermark_sha256"],
            "current_watermark_verified": live is not None, "TTL_renewed": False,
        }
    return bindings, max(generations) if generations else None, min(expirations) if expirations else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-run", type=Path, required=True,
                        help="existing frozen independent per-query forecasts; allocations are NOT read")
    parser.add_argument("--frozen-replay", action="store_true",
                        help="historical export only, not current valid inputs; no DB calls or TTL renewal")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    report_path = args.prepared_run / "FIVE_STAGE_RESULTS.json"
    report = read(report_path)
    forecast_path = args.prepared_run / "QUERY_FORECASTS.json"
    if sha(forecast_path) != report["prepared_artifact_sha256"][forecast_path.name]:
        raise ValueError("prepared forecasts changed")
    input_paths = [Path(b["path"]) for b in report["source_artifacts"] if b["path"].endswith("/INPUTS.json")]
    if len(input_paths) != 1:
        raise ValueError("one blind SQL/WM input manifest required")
    source_bindings = {b["path"]: b["sha256"] for b in report["source_artifacts"]}
    input_path = input_paths[0]
    if sha(input_path) != source_bindings[str(input_path)]:
        raise ValueError("blind input manifest changed")
    inputs, forecasts = read(input_path), read(forecast_path)
    sqls, grouped, sources = {}, {}, [report_path, forecast_path, input_path]
    for query in inputs["queries"]:
        q, path = query["query_id"], Path(query["SQL_path"])
        if sha(path) != query["SQL_sha256"]:
            raise ValueError("query SQL identity changed")
        sqls[q] = query["SQL_sha256"]
        sources.append(path)
        grouped[q] = []
        for wm in range(inputs["grid_mib"], 4097, inputs["grid_mib"]):
            key = "q%d_wm%d" % (q, wm)
            if key not in forecasts or forecasts[key]["work_mem_mib"] != wm:
                raise ValueError("prepared query/WM curve incomplete")
            raw_path = input_path.parent / "plans" / (key + "_EXPLAIN_RAW.json")
            if sha(raw_path) != source_bindings[str(raw_path.resolve())]:
                raise ValueError("blind plan changed")
            grouped[q].append(forecasts[key])
            sources.append(raw_path)
    snapshot_path = args.prepared_run / "MODEL_SOURCE_SNAPSHOT.json"
    if snapshot_path.exists():
        snapshot = read(snapshot_path)
        if snapshot["original_result_sha256"] != sha(report_path):
            raise ValueError("frozen model snapshot refers to another preparation")
        source_code = {entry["original_path"]: entry for entry in snapshot["files"]}
        for name, expected in report["model_code_sha256"].items():
            entry = source_code[name]
            if entry["sha256"] != expected or sha(args.prepared_run / entry["snapshot_path"]) != expected:
                raise ValueError("frozen prediction source changed")
        sources.append(snapshot_path)
    else:
        for name, expected in report["model_code_sha256"].items():
            if sha(ROOT / name) != expected:
                raise ValueError("prediction code changed without a preserved source snapshot")
    stats, valid_from, expires = check_statistics(report["statistics"], args.frozen_replay)
    if not args.frozen_replay:
        from jmt.common.machine import collect_machine
        current = collect_machine(Path(report["machine"]["device"]), Path("/opt/openGauss/bin/gaussdb"),
                                  report["machine"]["source_commit"])
        if current["machine_fingerprint"] != report["machine"]["machine_fingerprint"]:
            raise ValueError("current machine/executor differs from frozen forecast inputs")
    source_time = datetime.fromisoformat(report["created_at"]).timestamp()
    if stats and not valid_from <= source_time <= expires:
        raise ValueError("original forecast was outside its input validity interval")
    started = time.perf_counter()
    curves = build_spill_curves(grouped, sqls)
    export_seconds = time.perf_counter() - started
    curves.update({
        "exported_at": datetime.now().astimezone().isoformat(),
        "forecast_prepared_at": report["created_at"], "existing_forecasts_reused_without_refit": True,
        "machine_fingerprint": report["machine"]["machine_fingerprint"],
        "database": inputs["database"], "grid_mib": inputs["grid_mib"],
        "statistics": stats, "valid_from_unix": valid_from, "expires_unix": expires,
        "current_inputs_revalidated": not args.frozen_replay,
        "machine_fingerprint_verified_on_export": not args.frozen_replay,
        "frozen_replay_only": args.frozen_replay, "sample_TTL_renewed": False,
        "new_target_AP_execution_or_accuracy_holdout": False,
        "source_forecast_model_code_sha256": report["model_code_sha256"],
        "source_frozen_model_snapshot": str(snapshot_path.resolve()) if snapshot_path.exists() else None,
        "forecasting_model_unchanged_by_module_boundary_fix": True,
        "export_compute_seconds": export_seconds,
        "curve_export_code_sha256": {
            name: sha(ROOT / name) for name in (
                "src/jmt/innovation_2_spill/curves.py", "tools/export_ap_spill_curves.py")},
        "source_artifacts": [{"path": str(p.resolve()), "sha256": sha(p)} for p in sources],
    })
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
    print(json.dumps({"curves": len(curves["queries"]),
                      "WM_points": sum(len(c["points"]) for c in curves["queries"].values()),
                      "current_inputs_revalidated": not args.frozen_replay,
                      "contains_allocations": False, "compute_seconds": export_seconds}))


if __name__ == "__main__":
    main()
