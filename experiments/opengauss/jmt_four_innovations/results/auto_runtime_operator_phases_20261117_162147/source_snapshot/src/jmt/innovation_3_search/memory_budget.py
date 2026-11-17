"""Capacity minus EXPLICIT reserves; no fixed AP 6-GiB/16-GiB quota.

Preferred v2: configured database capacity planning with native execution
compatibility diagnostics. The v1 policy is retained for historical replay.
A smaller active SB does not by itself increase the native dynamic quota;
do not mistake either a large accepted SET or an uncapped plan for safe apply.
"""
import math

# Preserve the older evidence schema for historical/native-pipeline replay.
# New decisions use derive_memory_budget(), not the old fitted budget API.
from .legacy_memory_budget import build_memory_budget, validate_memory_budget_evidence

MIB = 1024 ** 2
SCHEMA = "jmt.capacity-minus-reserves-budget/v1"
DATABASE_SCHEMA = "jmt.database-configured-capacity-budget/v2"


def _nonnegative(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("finite nonnegative " + name + " required")


def derive_memory_budget(observation, *, host_safety_fraction=0.10,
                         host_min_reserve_mib=2048, host_extra_headroom_mib=512,
                         dynamic_headroom_mib=256, host_reserve_override_mib=None,
                         _require_positive_native_remainder=True):
    """All remaining memory is usable, SUBJECT TO the chosen reserve policy.

    10%/2GiB/512MiB/256MiB are transparent, configurable initial policies,
    NOT measured safety bounds or user/PPT requirements. The TP-only sample
    excludes AP workspaces; its observed dynamic maximum includes connection,
    catalog and common memory, so don't subtract idle memory a second time.
    """
    if (isinstance(host_safety_fraction, bool)
            or not isinstance(host_safety_fraction, (int, float))
            or not math.isfinite(host_safety_fraction) or not 0 <= host_safety_fraction < 1):
        raise ValueError("host safety fraction must be finite in [0,1)")
    for name, val in (("minimum host reserve", host_min_reserve_mib),
                      ("extra host headroom", host_extra_headroom_mib),
                      ("dynamic headroom", dynamic_headroom_mib)):
        _nonnegative(val, name)
    host, db = observation["host"], observation["database"]
    capacity = host["physical_memory_bytes"]
    _nonnegative(capacity, "physical capacity")
    if capacity == 0:
        raise ValueError("positive physical capacity required")
    limits = host.get("cgroup_hard_limits_bytes", [])
    for value in limits:
        _nonnegative(value, "cgroup hard limit")
    effective_bytes = min([capacity] + limits)
    effective_mib = effective_bytes / MIB
    kernel = host["nonreclaimable_kernel_bytes"] / MIB
    others = host["non_database_process_pss_bytes"] / MIB
    _nonnegative(kernel, "kernel memory")
    _nonnegative(others, "other processes")
    observed_host = kernel + others
    if host_reserve_override_mib is not None:
        _nonnegative(host_reserve_override_mib, "host reserve override")
        if host_reserve_override_mib < observed_host:
            raise ValueError("host reserve override below observed non-database usage")
        host_reserve = math.ceil(host_reserve_override_mib)
    else:
        host_reserve = math.ceil(max(host_min_reserve_mib, effective_mib * host_safety_fraction,
                                     observed_host + host_extra_headroom_mib))
    mem = db["memory_mib"]
    required = ("max_process_memory", "max_dynamic_memory", "max_shared_memory",
                "max_backend_memory", "max_cstore_memory", "udf_reserved_memory")
    for key in required:
        _nonnegative(mem[key], key)
    startup_sb = db["startup_shared_buffers_mib"]
    _nonnegative(startup_sb, "startup SB")
    if startup_sb > mem["max_shared_memory"]:
        raise ValueError("startup SB exceeds measured shared-memory ceiling")
    dynamic_peak = observation["TP_only_probe"]["max_dynamic_used_memory_mib"]
    _nonnegative(dynamic_peak, "observed TP-only dynamic peak")
    if observation["TP_only_probe"].get("AP_queries_present") is not False:
        raise ValueError("a verified TP-only memory sample is required")
    db_dynamic_reserve = math.ceil(dynamic_peak + dynamic_headroom_mib)
    non_sb_shared = mem["max_shared_memory"] - startup_sb
    db_fixed = math.ceil(non_sb_shared + mem["max_backend_memory"]
                         + mem["max_cstore_memory"] + mem["udf_reserved_memory"])
    host_db_ceiling = math.floor(effective_mib - host_reserve)
    host_pool = host_db_ceiling - db_fixed - db_dynamic_reserve
    current_db_ceiling = min(host_db_ceiling, math.floor(mem["max_process_memory"]))
    current_pool = current_db_ceiling - db_fixed - db_dynamic_reserve
    current_dynamic_cap = math.floor(mem["max_dynamic_memory"] - db_dynamic_reserve)
    if (min(host_pool, current_pool) <= 0
            or _require_positive_native_remainder and current_dynamic_cap <= 0):
        raise ValueError("reserves leave no positive SB/AP or current dynamic budget")
    return {
        "schema": SCHEMA, "effective_capacity_mib": effective_mib,
        "physical_capacity_mib": capacity / MIB, "host_reserve_mib": host_reserve,
        "observed_host_non_database_mib": observed_host,
        "non_SB_shared_ceiling_mib": non_sb_shared,
        "database_fixed_reserve_mib": db_fixed,
        "TP_and_common_dynamic_reserve_mib": db_dynamic_reserve,
        "observed_TP_only_dynamic_max_mib": dynamic_peak,
        "host_database_ceiling_after_reserve_mib": host_db_ceiling,
        "current_max_process_memory_mib": mem["max_process_memory"],
        "current_max_dynamic_memory_mib": mem["max_dynamic_memory"],
        "policy": {
            "host_safety_fraction": host_safety_fraction,
            "host_min_reserve_mib": host_min_reserve_mib,
            "host_extra_headroom_mib": host_extra_headroom_mib,
            "dynamic_headroom_mib": dynamic_headroom_mib,
            "host_reserve_override_mib": host_reserve_override_mib,
            "reserve_values_are_initial_policy_not_measured_peak_guarantees": True,
        },
        "scenarios": {
            "current_instance": {
                "shared_pool_mib": current_pool,
                "AP_dynamic_hard_cap_mib": current_dynamic_cap,
                "pool_formula": "min(host_after_reserve, current_max_process_memory) - DB_fixed - TP_common_dynamic_reserve",
                "AP_budget_formula": "min(pool - candidate_SB, startup_max_dynamic_memory - TP_common_dynamic_reserve)",
                "requires_database_restart_or_memory_protector_change": False,
            },
            "host_after_reserve": {
                "shared_pool_mib": host_pool, "AP_dynamic_hard_cap_mib": None,
                "pool_formula": "effective_physical_capacity - explicit_host_reserve - DB_fixed - TP_common_dynamic_reserve",
                "AP_budget_formula": "pool - candidate_SB",
                "required_max_process_memory_mib_at_least": host_db_ceiling,
                "requires_database_restart_or_memory_protector_change": True,
                "preconditions": [
                    "raise max_process_memory if below the derived host database ceiling",
                    "initialize/recompute the native dynamic-memory protector for the chosen SB; shrinking active SB alone is insufficient",
                    "verify TP common-memory reserve and concurrent AP physical peaks before apply",
                ],
                "scenario_not_currently_executable_configuration": True,
            },
        },
        "fixed_16_GiB_experimental_pool_used": False,
        "MemAvailable_used_as_total_pool": False,
        "SB_already_subtracted_from_pool": False,
        "reserve_policy_physically_validated": False,
        "configuration_applied": False,
    }


def derive_database_memory_budget(observation, **policy):
    """Preferred planning policy: database/host pool minus candidate SB.

    Retain the native-protector remainder as an explicit execution diagnostic,
    NOT as an AP-specific cap in the joint optimization. The v1 function stays
    byte-replay-compatible for historical evidence; it is not the CLI default.
    """
    result = derive_memory_budget(observation, _require_positive_native_remainder=False, **policy)
    settings = {r["name"]: r["setting"] for r in observation["database"]["settings"]}
    if settings.get("enable_memory_limit") not in ("on", "off"):
        raise ValueError("observed effective enable_memory_limit required")
    old = result["scenarios"].pop("current_instance")
    native_remainder = old.pop("AP_dynamic_hard_cap_mib")
    old.pop("requires_database_restart_or_memory_protector_change")
    old.update({
        "AP_dynamic_hard_cap_mib": None,
        "AP_budget_formula": "min(host_after_reserve, max_process_memory) - DB_fixed - TP_common_reserve - candidate_SB",
        "AP_specific_native_memory_limit_mib": None,
        "runtime_compatibility_requires_per_configuration_check": True,
        "requires_database_restart_or_memory_protector_change": None,
        "SET_domain_is_separate_from_total_AP_budget": True,
    })
    result["schema"] = DATABASE_SCHEMA
    result["default_planning_scenario"] = "database_configured"
    result["scenarios"]["database_configured"] = old
    result["native_runtime_protection"] = {
        "effective_enable_memory_limit": settings["enable_memory_limit"] == "on",
        "observed_max_dynamic_memory_mib": result["current_max_dynamic_memory_mib"],
        "TP_common_reserve_mib": result["TP_and_common_dynamic_reserve_mib"],
        "conservative_AP_remainder_mib": native_remainder,
        "remainder_is_tool_policy_not_database_AP_limit": True,
        "used_as_primary_optimizer_AP_cap": False,
        "online_SB_release_updates_dynamic_quota_verified": False,
        "threshold_is_tracked_allocation_guard_not_absolute_RSS_cap": True,
    }
    result["scenarios"]["native_protector_diagnostic"] = {
        "shared_pool_mib": old["shared_pool_mib"],
        "AP_dynamic_hard_cap_mib": (
            native_remainder if settings["enable_memory_limit"] == "on" else None),
        "AP_budget_formula": "diagnostic min(database pool - SB, native tracked quota minus tool TP reserve)",
        "not_a_database_AP_specific_cap": True,
        "diagnostic_only_not_primary_joint_optimization": True,
        "requires_database_restart_or_memory_protector_change": False,
    }
    result["AP_budget_policy"] = (
        "database_allowed_SET_domain + database-configured joint pool; "
        "native current protection checked separately before any apply")
    return result


def add_native_admission_diagnostics(frontier, budget):
    """Annotate actual selected allocations; NEVER change the DP budget/WM."""
    protection = budget.get("native_runtime_protection")
    if protection is None:
        return
    limit = protection["observed_max_dynamic_memory_mib"]
    reserve = protection["TP_common_reserve_mib"]
    enabled = protection["effective_enable_memory_limit"]
    for stage in frontier["stages"]:
        for point in stage["per_SB_minimum_spill"]:
            selected = point["selection"]
            if selected is None:
                continue
            charge = selected["rounded_reservation_mib"]
            over = enabled and charge + reserve > limit
            point["native_admission_diagnostic"] = {
                "modeled_AP_reservation_mib": charge,
                "tool_TP_common_reserve_mib": reserve,
                "modeled_dynamic_total_mib": charge + reserve,
                "observed_native_tracked_dynamic_limit_mib": limit,
                "exceeds_current_native_threshold_under_modeled_accounting": over,
                "native_quota_coordination_required_before_execution": over,
                "actual_operator_grants_and_physical_peaks_verified": False,
                "not_a_proof_of_actual_OOM_or_success": True,
                "optimizer_budget_or_selected_WM_modified": False,
            }
    frontier["native_protector_remainder_used_as_primary_AP_limit"] = (
        frontier["memory_budget_scenario"] == "native_protector_diagnostic")
    frontier["native_admission_separate_from_optimizer_budget"] = True
    frontier["AP_memory_policy"] = budget["AP_budget_policy"]
    frontier["current_native_runtime_execution_qualified"] = False
    frontier["SET_acceptance_alone_authorizes_execution"] = False


def main():
    """Historical CLI only; current capacity census has its own explicit tool."""
    import sys
    from .legacy_memory_budget import main as legacy_main
    print("Historical fitted-memory-budget schema; current route is tools/collect_machine_memory_budget.py",
          file=sys.stderr)
    return legacy_main()


if __name__ == "__main__":
    raise SystemExit(main())
