"""No fitting: short-horizon CPU-service persistence, not cold-start search.

The TP-only CPU budget and the AP-loaded service cost are DIFFERENT quantities.
Loaded cost must not redefine the independent TP-only reference.  All observed
inputs come from a prior window, never the future window being predicted.
"""
from .preview_cost import _number
from .cpu_queue_feedback import require_fresh_point
from .tps_diagnostics import summarize_windows

SCHEMA = "jmt.prior-window-CPU-service-persistence/v1"
PERSISTENCE = "prior_probe_TPS_persistence"
AP_ONLY = "prior_AP_CPU_reference_TP_service"
BACKEND = "prior_AP_CPU_loaded_TP_backend_service"
FULL = "prior_AP_CPU_loaded_TP_backend_driver_service"
LANES = (PERSISTENCE,AP_ONLY,BACKEND,FULL)


def predict_prior_service(*, reference, probe, prediction_ns, cpu_capacity, AP_task_identities,
                          probe_max_age_seconds=10, control_max_age_seconds=300):
    point = reference["point"]
    control_age = require_fresh_point(point,prediction_ns,control_max_age_seconds)
    if (point.get("role") != "calibration" or point.get("topology") != "TP128"
            or point["terminals"] != 128 or len(probe["backend_linux_tids"]) != 128
            or probe["shared_buffers_mib"] != point["shared_buffers_mib"]
            or not probe["AP_work_mem_mib"] or probe.get("S5_first_event_gate_ns") is not None
            or reference["frozen_monotonic_ns"] >= probe["measurement_window"]["start_ns"]):
        raise ValueError("prior AP-free TP128 reference and same-SB loaded probe required")
    age = (prediction_ns-probe["measurement_window"]["end_ns"])/1e9
    if not 0 <= age <= _number(probe_max_age_seconds,True):
        raise ValueError("AP/TP service probe stale or from future")
    if not .5 <= probe["scored_seconds"] <= 10:
        raise ValueError("bounded short probe required")
    # Replay exact CPU sums, not blindly trusting their denormalized fields.
    first,last = (probe["CPU_bracket"][k]["backend_cpu_ns"] for k in ("before","after"))
    deltas = [last[str(t)]-first[str(t)] for t in probe["backend_linux_tids"]]
    if min(deltas)<0 or sum(deltas)/1e9 != probe["TP_CPU_seconds"]:
        raise ValueError("TP probe CPU not bound to enrolled task endpoints")
    for key,endpoint in (("before","start_ns"),("after","end_ns")):
        snap = probe["CPU_bracket"][key]
        if (not 0<=snap["finished_ns"]-snap["started_ns"]<100_000_000
                or abs(snap["started_ns"]-probe["measurement_window"][endpoint])>100_000_000):
            raise ValueError("short probe CPU endpoint screen failed; no rescaling")
    ap_tids = {str(q):str(row["linux_tid"]) for q,row in AP_task_identities.items()}
    if not ap_tids or set(ap_tids) != set(probe["AP_CPU_seconds"]):
        raise ValueError("probe AP cohort identity mismatch")
    for q,tid in ap_tids.items():
        if (last[tid]-first[tid])/1e9 != probe["AP_CPU_seconds"][q]:
            raise ValueError("AP probe CPU not bound to owned task endpoints")
    observed = summarize_windows([probe])
    baseline = _number(point["baseline_tps"],True)
    backend = _number(point["backend_cpu_seconds_per_tx"],True)
    driver = _number(point["driver_cpu_seconds_per_tx"],True)
    capacity = _number(cpu_capacity,True)
    budget = baseline*(backend+driver)
    if budget >= capacity:
        raise ValueError("independent TP reference exceeds CPU capacity")
    ap = _number(observed["AP_CPU_cores_bracketed"])
    loaded_backend = _number(observed["TP_CPU_seconds_per_commit_proxy"],True)
    loaded_driver = _number(observed["driver_CPU_seconds_per_commit_proxy"],True)
    available = max(0.,budget-ap)
    services = {AP_ONLY:backend+driver,BACKEND:loaded_backend+driver,
                FULL:loaded_backend+loaded_driver}
    forecasts = {PERSISTENCE:{"predicted_tps":observed["committed_TPS"],
        "kind":"naive_prior_probe_TPS_persistence_comparator"}}
    forecasts.update({name:{"predicted_tps":min(baseline,available/service),
                           "loaded_TP_CPU_seconds_per_tx":service,
                           "kind":"CPU_conservation_conditional_forecast"}
                      for name,service in services.items()})
    return {
        "schema":SCHEMA,"prediction_monotonic_ns":prediction_ns,"lanes":forecasts,
        "prior_AP_CPU_cores":ap,"prior_loaded_backend_CPU_seconds_per_tx":loaded_backend,
        "prior_loaded_driver_CPU_seconds_per_tx":loaded_driver,
        "TP_reference_CPU_budget_cores":budget,"TP_reference_baseline_TPS":baseline,
        "TP_reference_age_seconds":control_age,"probe_age_seconds":age,
        "probe_end_ns":probe["measurement_window"]["end_ns"],
        "AP_task_identities":AP_task_identities,
        "TP_reference_not_redefined_as_loaded_service":True,
        "historical_cache_penalty_not_reapplied_to_measured_loaded_service":True,
        "AP_phase_and_TP_service_persistence_unqualified":True,
        "requires_current_configuration_AP_execution_before_forecast":True,
        "cold_start_configuration_forecast":False,
        "extra_TP_IO_latency_measured_or_known_zero":False,
        "future_target_TPS_CPU_or_IO_used":False,
        "physical_IO_or_full_five_stage_optimum_qualified":False,
        "automatic_recommendation_qualified":False}
