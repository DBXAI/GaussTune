"""A TP-only miss plateau is NOT a hard bound for a shared TP/AP cache."""


def mixed_search_domain(boundary, candidate_SB_mib, *, startup_SB_mib=None,
                        legacy_TP_only_pruning=False):
    """Keep measured/replayed larger points until a mixed-scope bound exists.

    This does not invent a mixed miss curve, AP occupancy, or a new optimum.
    The exploration ceiling is hardware/trace-domain limited, NOT a claim
    that TP/AP cache benefit has saturated at that value.
    """
    grid = list(candidate_SB_mib)
    if (not grid or len(grid) != len(set(grid))
            or any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in grid)):
        raise ValueError("positive unique integer SB exploration grid required")
    if startup_SB_mib is not None and (
            isinstance(startup_SB_mib, bool) or not isinstance(startup_SB_mib, int)
            or startup_SB_mib <= 0):
        raise ValueError("positive integer startup SB required")
    hints = {name: row["boundary"]["B_high_mib"]
             for name, row in boundary["topologies"].items()}
    if set(hints) != {"TP128", "S5_128_plus_16"}:
        raise ValueError("both TP topologies required")
    ceiling = min(max(grid), startup_SB_mib) if startup_SB_mib is not None else max(grid)
    if not any(x <= ceiling for x in grid):
        raise ValueError("no SB candidate under startup ceiling")
    upper = ({name: min(hint, ceiling) for name, hint in hints.items()}
             if legacy_TP_only_pruning else {name: ceiling for name in hints})
    return {
        "schema": "jmt.mixed-workload-SB-search-scope/v1",
        "TP_only_plateau_hint_mib": hints,
        "hard_search_ceiling_by_topology_mib": upper,
        "startup_SB_ceiling_mib": startup_SB_mib,
        "search_grid_domain_maximum_mib": max(grid),
        "TP_only_hint_applied_as_mixed_hard_bound": legacy_TP_only_pruning,
        "legacy_diagnostic_only": legacy_TP_only_pruning,
        "mixed_cache_plateau_qualified": False,
        "mixed_TP_cache_hit_curve_available": False,
        "requires_mixed_cache_profile_for_performance_ranking": True,
        "ceiling_is_exploration_limit_not_mixed_saturation": not legacy_TP_only_pruning,
        "candidates_previously_pruned_by_TP_only_hint_retained_mib": {
            name: sorted(x for x in grid if hint < x <= ceiling)
            if not legacy_TP_only_pruning else [] for name, hint in hints.items()},
        "note": "AP accesses can replace TP pages; do not subtract an arbitrary AP GiB constant",
    }
