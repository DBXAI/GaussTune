"""Capacity minus EXPLICIT reserves; no fixed experimental 16-GiB quota.

Separate the host-capacity planning scenario from what the currently running
openGauss memory protector admits. A smaller active SB does not, by itself,
increase maxChunksPerProcess: memprot.cpp initializes it at postmaster start.
"""
import math

# Preserve the older evidence schema for historical/native-pipeline replay.
# New decisions use derive_memory_budget(), not the old fitted budget API.
from .legacy_memory_budget import build_memory_budget, validate_memory_budget_evidence

MIB = 1024 ** 2
SCHEMA = "jmt.capacity-minus-reserves-budget/v1"


def _nonnegative(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("finite nonnegative " + name + " required")


def derive_memory_budget(observation, *, host_safety_fraction=0.10,
                         host_min_reserve_mib=2048, host_extra_headroom_mib=512,
                         dynamic_headroom_mib=256, host_reserve_override_mib=None):
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
    if min(host_pool, current_pool, current_dynamic_cap) <= 0:
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


def main():
    """Historical CLI only; current capacity census has its own explicit tool."""
    import sys
    from .legacy_memory_budget import main as legacy_main
    print("Historical fitted-memory-budget schema; current route is tools/collect_machine_memory_budget.py",
          file=sys.stderr)
    return legacy_main()


if __name__ == "__main__":
    raise SystemExit(main())
