#!/usr/bin/env python3
"""Run an explicit experimental SB/WM recommendation, not deployment acceptance.

No DB mutation or target execution. Existing strict/native release gates are
not called with patched evidence. A preview uses recorded assumptions and is
always marked unqualified, even when the discrete search is complete.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.common.db import extract_json
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_3_search.current_joint import evaluate_current_joint
from jmt.innovation_3_search.pipeline_native import _predict_tps
from jmt.innovation_3_search.search import QueryOption
from jmt.innovation_3_search.stage_spec import EXPECTED
from jmt.innovation_4_performance.ap_resource_bridge import digest, extract_work
from jmt.innovation_4_performance.device import DeviceSurface, ServiceTimes, SurfacePoint
from jmt.innovation_4_performance.preview_cost import (
    fit_preview_cost, predict_preview_cost, solve_preview_joint_resources,
)
from jmt.innovation_4_performance.capped_device import DeviceCap
from jmt.innovation_1_cache.cache_boundary import load_boundary
from collect_current_ap import SQL_SHA256


PROFILE = "current_16x1M_sysbench_readonly_omm_local_socket"
TOPOLOGY = "128-warmed-plus-16-first-event-gated-at-measurement"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def dump(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def checked_work(work):
    if work["sha256"] != digest({k: v for k, v in work.items() if k != "sha256"}):
        raise ValueError("changed frozen work")
    return work


def training_rows(micro, current, machine):
    """Frozen calibration pairs, never their stale statistics as target N/G."""
    rows, io_rows, sources = [], [], []
    frozen = read(micro / "FROZEN_WORK.json")
    sources.append(micro / "FROZEN_WORK.json")
    for point in frozen["points"]:
        if point["role"] != "calibration":
            continue
        observation_path = micro / (point["key"] + "_OBS.json")
        observation = read(observation_path)
        if (point["machine_fingerprint"] != machine
                or point["resource_frozen_unix_ns"] >= observation["started_unix_ns"]
                or observation.get("status") != "measured"):
            raise ValueError("micro calibration identity/time mismatch")
        rows.append({
            "key": point["key"], "role": "calibration", "statement_completed": True,
            "features_frozen_before_execution": True, "work": checked_work(point["work"]),
            "cpu_seconds": observation["cpu_seconds"], "wall_seconds": observation["wall_seconds"],
            "training_statistics_expired_but_not_reused_as_target_statistics": True,
        })
        sources.append(observation_path)
    if read(current / "MACHINE.json")["machine_fingerprint"] != machine:
        raise ValueError("current AP and TP machines differ")
    sources += [current / "MACHINE.json", current / "CATALOG.json"]
    for path in sorted(current.glob("q*_wm*/MEASUREMENT.json")):
        observation = read(path)
        if observation.get("statement_completed") is not True:
            continue
        arm = path.parent
        frozen = read(arm / "FREEZE.json")
        if (frozen["resource_frozen_unix_ns"] >= observation["started_unix_ns"]
                or frozen["work_sha256"] != sha(arm / "FROZEN_WORK.json")
                or frozen.get("uses_target_labels") is not False):
            raise ValueError("current AP work was not frozen before execution")
        block = observation["block_issue"]
        whole = {
            key: block["snapshot_after"]["device_totals"][key]
                 - block["snapshot_before"]["device_totals"][key]
            for key in ("read_requests", "write_requests", "read_bytes", "write_bytes")
        }
        row = {
            "key": arm.name, "role": "calibration", "statement_completed": True,
            "features_frozen_before_execution": True,
            "work": checked_work(read(arm / "FROZEN_WORK.json")),
            "cpu_seconds": observation["cpu_seconds"], "wall_seconds": observation["wall_seconds"],
            "foreground_IO": {key: block[key] for key in whole}, "whole_device_IO": whole,
        }
        rows.append(row)
        io_rows.append(row)
        sources += [path, arm / "FREEZE.json", arm / "FROZEN_WORK.json"]
    return rows, io_rows, sources


def failed_wm_regions(current):
    """Conservative trial-domain exclusions, NOT fitted query-ID corrections.

    More memory is not guaranteed to fix a timeout, nor is spill monotonic
    across plan changes. This only avoids recommending an already-failed
    point or a still-lower WM as the next unvalidated trial configuration.
    """
    failures = []
    for path in sorted(current.glob("q*_wm*/MEASUREMENT.json")):
        row = read(path)
        error = str(row.get("error") or "")
        if row.get("statement_completed") or not any(
                term in error for term in ("temp_file_limit", "statement timeout")):
            continue
        failures.append({
            "SQL_sha256": read(path.parent / "FREEZE.json")["SQL_sha256"],
            "failed_work_mem_mib": int(path.parent.name.split("_wm", 1)[1]),
            "source": str(path.resolve()), "source_sha256": sha(path), "error": error,
            "policy": "exclude same-SQL candidate WM <= failed point in this trial; "
                      "not a proof of monotonicity or a guaranteed repair",
        })
    return failures


def surge_baselines(matrix_path, machine):
    """Separate real surge calibration: reps1/2 train; rep3 remains held out."""
    matrix = read(matrix_path)
    if (matrix.get("schema") != "jmt.current-TP-surge-kernel-window-matrix/v1"
            or matrix.get("complete") is not True or matrix.get("restoration_verified") is not True
            or matrix.get("error") is not None or matrix.get("machine_fingerprint") != machine
            or matrix.get("topology") != TOPOLOGY):
        raise ValueError("S5 needs its completed and restored real 128+16 matrix")
    grouped = {}
    wrapper = read(matrix_path.parent / "SOURCE.json")["wrapper"]
    for source in matrix["rows"]:
        path = Path(source["window_path"])
        if sha(path) != source["window_sha256"]:
            raise ValueError("changed S5 window")
        row = read(path)
        window = row["measurement_window"]
        duration = (window["end_ns"] - window["start_ns"]) / 1e9
        base, extra = row["baseline_backend_tids"], row["surge_backend_tids"]
        if (window["clock"] != "monotonic_ns" or duration <= 0
                or abs(duration - row["scored_seconds"]) > 1e-9
                or row["topology"] != TOPOLOGY or row["role_profile"] != PROFILE
                or row["surge_gate_monotonic_ns"] != window["start_ns"]
                or row["surge_wrapper"] != wrapper
                or row["driver_exit_codes"] != [0, 0]
                or row["device_error_completion_events"] != 0
                or row["not_flat_144_baseline"] is not True
                or len(set(base)) != 128 or len(set(extra)) != 16 or set(base) & set(extra)
                or set(row["backend_linux_tids"]) != set(base + extra)):
            raise ValueError("invalid S5 topology or exact-window evidence")
        for name in ("baseline.log", "surge.log"):
            match = re.search(r"ignored errors:\s*(\d+)", (path.parent / name).read_text())
            if not match or int(match[1]):
                raise ValueError("S5 driver errors")
        tx = row["committed_transactions"]
        if tx <= 0 or abs(source["TPS"] - tx / duration) > 1e-9:
            raise ValueError("S5 transaction count must not be rescaled")
        for name, endpoint in (("CPU_snapshot_before", "start_ns"),
                               ("CPU_snapshot_after", "end_ns")):
            snap = row[name]
            if (not 0 <= snap["finished_ns"] - snap["started_ns"] < 100_000_000
                    or abs(snap["started_ns"] - window[endpoint]) > 100_000_000):
                raise ValueError("S5 CPU sampling boundary too wide")
        grouped.setdefault(source["shared_buffers_mib"], []).append((source, row))
    rows = []
    for sb, pairs in sorted(grouped.items()):
        pairs.sort(key=lambda item: item[0]["repeat"])
        if [item[0]["repeat"] for item in pairs] != [1, 2, 3]:
            raise ValueError("S5 needs two training repeats and the third holdout")
        training, heldout = pairs[:2], pairs[2]
        rows.append({
            "shared_buffers_mib": sb,
            "sustainable_tps": statistics.mean(s["TPS"] for s, _ in training),
            "tp_cpu_seconds_per_tx": statistics.mean(
                r["CPU"]["backend_cpu_ms_per_kernel_window_transaction"] / 1000
                for _, r in training),
            "whole_device_read_events_per_tx_training_proxy": statistics.mean(
                r["device_read_completion_events"] / r["committed_transactions"]
                for _, r in training),
            "heldout_TPS": heldout[0]["TPS"],
            "repeat_3_not_used_for_selection": True,
            "not_flat_144_baseline": True,
        })
    if len(rows) < 3:
        raise ValueError("at least three measured S5 SB points required")
    return rows


def load_surfaces(directory):
    service_path = directory / "service-times-v2.json"
    service_doc = read(service_path)
    service = ServiceTimes(**service_doc["service_times_ms"])
    surfaces, sources = [], [service_path]
    for name in ("holdout-report-v2.json", "pure-read/holdout-report-v2.json"):
        path = directory / name
        report = read(path)
        surface = DeviceSurface([
            SurfacePoint(row["tp_queue_depth"], row["ap_queue_depth"], row["tp_read_latency_ms"])
            for row in report["surface"]
        ], report["machine_fingerprint"], ap_read_fraction=report["ap_read_fraction"])
        surfaces.append((report, surface))
        sources.append(path)
    return service, surfaces, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experimental-preview", action="store_true", required=True)
    parser.add_argument("--tp-model", type=Path, required=True)
    cache_group = parser.add_mutually_exclusive_group(required=True)
    cache_group.add_argument("--tp-cache-boundary", type=Path,
        help="Mandatory PPT path: trace-replayed TP P_SB curves constrain SB by B_high")
    cache_group.add_argument("--legacy-unbounded-preview", action="store_true",
        help="Explicit historical diagnostic ONLY; not PPT innovation-1-connected")
    parser.add_argument("--surge-matrix", type=Path, required=True)
    parser.add_argument("--micro-calibration", type=Path, required=True)
    parser.add_argument("--current-ap-calibration", type=Path, required=True)
    parser.add_argument("--fio-calibration", type=Path)
    parser.add_argument("--device-saturation", type=Path,
                        help="Prefer measured media caps over a low-QD-only fio surface")
    parser.add_argument("--plans-dir", type=Path, required=True)
    parser.add_argument("--sql-dir", type=Path, required=True)
    parser.add_argument("--join-sample-bundle", type=Path)
    parser.add_argument("--key-group-sample-bundle", type=Path)
    parser.add_argument("--pool-mib", type=int, default=16384)
    parser.add_argument("--work-mem-grid", default="64,512,2048,4096")
    parser.add_argument("--independent-stage-restarts", action="store_true",
                        help="Otherwise preserve WM for queries continuing between stages")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    grid = sorted(set(int(v) for v in args.work_mem_grid.split(",")))
    if len(grid) > 8 or not grid or not all(1 <= v <= 4096 for v in grid):
        raise ValueError("bounded explicit coarse WM grid required")
    args.out.mkdir(parents=True, exist_ok=False)
    tp = read(args.tp_model)
    machine = tp["machine_fingerprint"]
    if tp.get("benchmark_profile") != PROFILE or tp.get("baseline_terminals") != 128:
        raise ValueError("current TP calibration profile mismatch")
    surge = surge_baselines(args.surge_matrix, machine)
    cache_bound = (load_boundary(args.tp_cache_boundary,machine_fingerprint=machine,profile=PROFILE,
                                 require_miss_plateau=True)
                   if args.tp_cache_boundary else None)
    cpu_rows, io_rows, sources = training_rows(
        args.micro_calibration, args.current_ap_calibration, machine)
    model = fit_preview_cost(cpu_rows, io_rows)
    failed_regions = failed_wm_regions(args.current_ap_calibration)
    sources += [Path(item["source"]) for item in failed_regions]
    dump(args.out / "PREVIEW_COST_MODEL.json", model)
    dump(args.out / "COST_CALIBRATION_ROWS.json", cpu_rows)
    device_cap = None
    if args.device_saturation:
        device_cap = DeviceCap.from_saturation(read(args.device_saturation))
        service, surfaces, fio_sources = None, [], [args.device_saturation]
    elif args.fio_calibration:
        service, surfaces, fio_sources = load_surfaces(args.fio_calibration)
    else:
        raise ValueError("explicit device calibration required; missing IO is not zero")
    sources += fio_sources + [args.tp_model, args.surge_matrix]
    if args.tp_cache_boundary:
        sources.append(args.tp_cache_boundary)
        dump(args.out / "CACHE_BOUNDARY_BINDING.json", {
            "path":str(args.tp_cache_boundary.resolve()),"sha256":sha(args.tp_cache_boundary),
            "bound_source":"simulated P_SB, never measured TPS winner",
            "B_high_by_topology":{k:v["boundary"]["B_high_mib"]
                                  for k,v in cache_bound["topologies"].items()}})
    dump(args.out / "S5_CURRENT_TP_MODEL.json", {
        "rows": surge, "topology": TOPOLOGY, "automatic_recommendation_qualified": False})
    machine_doc = read(args.current_ap_calibration / "MACHINE.json")
    cpu_capacity = machine_doc["logical_cpus"]
    catalog = read(args.current_ap_calibration / "CATALOG.json")
    join_provider, key_provider, bindings = None, None, {}
    for name, path in (("join", args.join_sample_bundle), ("key_group", args.key_group_sample_bundle)):
        if path is None:
            continue
        bundle = read(path)
        before_path, after_path = path.parent / "WATERMARK_BEFORE.json", path.parent / "WATERMARK_AFTER.json"
        before, after = read(before_path), read(after_path)
        if before["sha256"] != after["sha256"] or bundle["watermark_sha256"] != after["sha256"]:
            raise ValueError("sample acquisition watermark changed")
        if name == "join":
            from jmt.innovation_2_spill.join_sample import JoinSampleProvider
            join_provider = JoinSampleProvider(bundle, after["sha256"])
        else:
            from jmt.innovation_2_spill.key_group_sample import KeyGroupSample
            key_provider = KeyGroupSample(bundle, after["sha256"])
        bindings[name] = {
            "path": str(path.resolve()), "sha256": sha(path),
            "data_watermark_sha256": after["sha256"],
            "scope": "readonly offline prediction as of completed collector watermark; not transactional CDC",
            "generated_unix": bundle["generated_unix"], "expires_unix": bundle["expires_unix"],
        }
        sources += [path, before_path, after_path]
    dump(args.out / "STATISTICS_BINDINGS.json", bindings)
    options, details = {}, {}
    prepare_started = time.perf_counter()
    for q in sorted(SQL_SHA256):
        sql_path = args.sql_dir / ("q%d.sql" % q)
        if sha(sql_path) != SQL_SHA256[q]:
            raise ValueError("full experiment-one SQL identity mismatch")
        sources.append(sql_path)
        session = ForecastSession(sql_path.read_text(), catalog,
                                  cardinality_provider=join_provider, key_group_sample=key_provider)
        options[q] = []
        for wm in grid:
            if any(item["SQL_sha256"] == SQL_SHA256[q]
                   and wm <= item["failed_work_mem_mib"] for item in failed_regions):
                continue
            path = args.plans_dir / ("q%d_wm%d_EXPLAIN_RAW.json" % (q, wm))
            raw = read(path)
            if raw["exit_code"]:
                raise ValueError("saved blind EXPLAIN failed")
            document = extract_json(raw["stdout"])
            forecast = session.forecast(document, wm)
            work = extract_work(forecast, document, catalog)
            cost = predict_preview_cost(model, work)
            physical = cost["physical_IO_proxy"]
            if device_cap is not None:
                roofline = max(
                    (physical["read_requests"] + physical["write_requests"]) / device_cap.iops,
                    (physical["read_bytes"] + physical["write_bytes"]) / (device_cap.mb_s * 1e6))
                cost["isolated_wall_before_media_roofline"] = cost["isolated_wall_seconds_estimate"]
                cost["isolated_wall_seconds_estimate"] = max(
                    cost["isolated_wall_seconds_estimate"], roofline)
                cost["media_roofline_is_transferred_cap_assumption"] = True
            option = QueryOption(
                q, wm, forecast["structural_peak_upper_mib"],
                physical["read_requests"], physical["write_requests"],
                cost["isolated_wall_seconds_estimate"], forecast["plan_structure_sha256"],
                cost["cpu_seconds_estimate"])
            options[q].append(option)
            details["q%d_wm%d" % (q, wm)] = {
                "forecast": forecast, "resource_work": work, "cost_preview": cost}
            sources.append(path)
        if not options[q]:
            raise ValueError("no trial WM above observed failure region for SQL " + SQL_SHA256[q])
    dump(args.out / "QUERY_OPTIONS.json", details)
    dump(args.out / "RESOURCE_OPTIONS_PREPARATION.json", {
        "prepared_at":datetime.now().astimezone().isoformat(),
        "query_options_sha256":sha(args.out/"QUERY_OPTIONS.json"),
        "machine_fingerprint":machine,
        "statistics_bindings":bindings,
        "sample_TTL_is_not_renewed_by_preparing_forecasts":True,
        "purpose":"small frozen resource interface for controlled cache-boundary experiments"})
    prepare_seconds = time.perf_counter() - prepare_started
    stages, previous = [], {}
    search_started = time.perf_counter()
    for name, queries, terminals, baseline_n, surge_n in EXPECTED:
        baselines = surge if surge_n else tp["rows"]
        b_high = (cache_bound["topologies"]["S5_128_plus_16" if surge_n else "TP128"]
                  ["boundary"]["B_high_mib"] if cache_bound else None)
        fixed = {} if args.independent_stage_restarts else {
            q: wm for q, wm in previous.items() if q in queries}
        def predict(sb, baseline, state):
            if device_cap is not None:
                demands = []
                for q, wm in state.assignments:
                    point = details["q%d_wm%d" % (q, wm)]["cost_preview"]
                    demands.append({
                        "cpu_seconds": point["cpu_seconds_estimate"],
                        "wall_seconds": point["isolated_wall_seconds_estimate"],
                        **point["physical_IO_proxy"],
                    })
                result = solve_preview_joint_resources(
                    baseline_tps=baseline["sustainable_tps"], terminals=terminals,
                    tp_cpu_seconds_per_tx=baseline["tp_cpu_seconds_per_tx"],
                    tp_read_requests_per_tx=baseline["whole_device_read_events_per_tx_training_proxy"],
                    tp_bytes_per_request=8192., cpu_capacity=cpu_capacity,
                    demands=demands, device_cap=device_cap)
                result["TP_read_events_are_whole_device_proxy_not_owned_IO"] = True
                return result
            rates = state.ap_read_iops + state.ap_write_iops
            fraction = state.ap_read_iops / rates if rates else 1.
            matches = [(report, surface) for report, surface in surfaces
                       if abs(fraction - report["ap_read_fraction"]) <= surface.ap_mix_tolerance]
            if not matches:
                raise ValueError("AP read/write mix outside transferred fio grid")
            report, surface = min(matches, key=lambda pair: abs(fraction - pair[0]["ap_read_fraction"]))
            result = _predict_tps(
                baseline_tps=baseline["sustainable_tps"], terminals=terminals,
                read_requests_per_tx=baseline["whole_device_read_events_per_tx_training_proxy"],
                write_requests_per_tx=0.,
                ap_read_iops=state.ap_read_iops, ap_write_iops=state.ap_write_iops,
                service=service, surface=surface,
                tp_cpu_seconds_per_tx=baseline["tp_cpu_seconds_per_tx"],
                ap_cpu_seconds_per_second=state.ap_cpu_seconds_per_second,
                cpu_capacity_seconds_per_second=cpu_capacity)
            result["fio_calibration_machine_fingerprint"] = report["machine_fingerprint"]
            result["fio_transfer_to_current_machine_is_unvalidated"] = (
                report["machine_fingerprint"] != machine)
            result["TP_read_events_are_whole_device_proxy_not_owned_IO"] = True
            return result
        result = evaluate_current_joint(
            options_by_query={q: options[q] for q in queries}, baseline_rows=baselines,
            total_pool_mib=args.pool_mib, predict_tp=predict, fixed_running_work_mem=fixed,
            nonmonotone_resource_response=device_cap is not None,
            sb_upper_bound_mib=b_high)
        selected = result["selected"]
        stages.append({
            "stage": name, "AP_queries": list(queries),
            "TP_baseline_terminals": baseline_n, "TP_surge_terminals": surge_n,
            "B_high_mib": b_high,
            "boundary_rule": (cache_bound["topologies"]["S5_128_plus_16" if surge_n else "TP128"]
                              ["boundary"] if cache_bound else None),
            "preserved_running_WM": fixed, "result": result,
            "status": "experimental_recommendation" if selected else "no_complete_recommendation"})
        if selected:
            previous = dict(selected["work_mem"])
        else:
            break
    report = {
        "schema": "jmt.experimental-five-stage-recommendations/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "selection_policy": "max_TP_TPS_then_AP_time_then_memory_on_exact_TPS_ties",
        "innovation_1_cache_boundary_connected":cache_bound is not None,
        "cache_boundary_sha256":sha(args.tp_cache_boundary) if cache_bound else None,
        "machine_fingerprint": machine, "stages": stages,
        "all_five_stages_have_numeric_recommendations": len(stages) == 5 and all(
            s["result"]["selected"] for s in stages),
        "shared_SB_AP_pool_mib": args.pool_mib,
        "observed_failure_domain_exclusions": failed_regions,
        "failure_domain_policy_is_not_proven_monotonicity": True,
        "pool_is_explicit_trial_policy_not_measured_physical_safe_budget": True,
        "work_mem_grid_mib": grid,
        "CPU_capacity_logical_CPUs_assumption_not_independent_effective_capacity": cpu_capacity,
        "continuing_query_WM_preserved": not args.independent_stage_restarts,
        "dominance_disabled_for_nonmonotone_finite_AP_closure": device_cap is not None,
        "cost_calibration_CPU_rows": len(cpu_rows), "cost_calibration_IO_rows": len(io_rows),
        "cost_preparation_seconds": prepare_seconds,
        "five_stage_search_seconds": time.perf_counter() - search_started,
        "AP_cardinality_input": {
            "default": "planner plus current generic semantic inference",
            "direct_sample_bindings": bindings,
        },
        "physical_IO_is_proxy_not_lifecycle_truth": True,
        "old_device_calibration_transfer_is_unvalidated": True,
        "device_model": "measured_saturation_caps" if device_cap else "low_QD_fio_surface",
        "transferred_device_caps": {
            "IOPS": device_cap.iops, "MB_per_second": device_cap.mb_s,
            "unloaded_read_ms": device_cap.unloaded_read_ms,
        } if device_cap else None,
        "prediction_is_independent_accuracy_proof": False,
        "automatic_recommendation_qualified": False, "configuration_applied": False,
        "strict_release_gates_modified_or_bypassed_to_claim_acceptance": False,
        "source_artifacts": [{"path": str(p.resolve()), "sha256": sha(p)}
                             for p in sorted(set(sources))],
    }
    dump(args.out / "FIVE_STAGE_RECOMMENDATIONS.json", report)
    print(json.dumps({
        "all_five_stages_have_numeric_recommendations": report["all_five_stages_have_numeric_recommendations"],
        "five_stage_search_seconds": report["five_stage_search_seconds"],
        "stages": [{**{k: s[k] for k in ("stage", "status", "preserved_running_WM")},
                    "selected": s["result"]["selected"],
                    "failures": len(s["result"]["failures"]),
                    "reason": s["result"].get("reason")} for s in stages],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
