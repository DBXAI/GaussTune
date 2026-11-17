"""Bounded mixed TP/AP cache replay, scored on TP accesses only.

Uses existing pin/clock/candidate-list/ring semantics. AP accesses remain in
the same ordered stream and mutate both caches. This is a diagnostic/profile
building component, NOT independently validated mixed-load performance.
"""
import math

from .cache_replay import replay_cache


def replay_mixed_curve(events, variants, *, context, sample_ppm=1000000,
                       buffer_policy="clock", candidate_lists=1, startup_pages=None,
                       max_events=1000000, session_threads=()):
    if (isinstance(sample_ppm, bool) or not isinstance(sample_ppm, int)
            or not 1 <= sample_ppm <= 1000000):
        raise ValueError("spatial page sample rate in 1..1000000 ppm required")
    if not isinstance(max_events, int) or isinstance(max_events, bool) or not 1 <= max_events <= 1000000:
        raise ValueError("bounded offline event budget in 1..1000000 required")
    if not 1 <= len(variants) <= 16:
        raise ValueError("bounded 1..16 cache variants required")
    if (isinstance(candidate_lists, bool) or not isinstance(candidate_lists, int)
            or not 1 <= candidate_lists <= 64):
        raise ValueError("integer candidate-list count in 1..64 required")
    threads = list(session_threads)
    if (len(threads) > 10000 or any(
            isinstance(t, bool) or not isinstance(t, int) or t <= 0 for t in threads)):
        raise ValueError("bounded positive integer session thread IDs required")
    for key in ("stage", "AP_profile_sha256", "TP_topology"):
        if not context.get(key):
            raise ValueError("mixed cache context missing " + key)
    profile = context["AP_profile_sha256"]
    if (not isinstance(profile, str) or len(profile) != 64
            or any(c not in "0123456789abcdef" for c in profile)):
        raise ValueError("bind AP plans, WM, concurrency, and phase as a profile hash")
    materialized, accesses = [], {"tp": 0, "ap": 0, "background": 0}
    last_seq, last_ns = -1, -1
    for event in events:
        if len(materialized) >= max_events:
            raise ValueError("mixed trace event work budget exceeded; no silent truncation")
        if event.seq <= last_seq or event.timestamp_ns < last_ns:
            raise ValueError("mixed trace requires one global monotonic order")
        last_seq, last_ns = event.seq, event.timestamp_ns
        if event.event == "ACCESS" and event.phase == "measure":
            if event.workload_class not in accesses:
                raise ValueError("unattributed shared-buffer ACCESS cannot be dropped")
            accesses[event.workload_class] += 1
        materialized.append(event)
    if not accesses["tp"] or not accesses["ap"]:
        raise ValueError("mixed profile requires measured TP AND AP accesses")
    fraction = sample_ppm / 1000000.

    def scale(pages, allow_zero=False):
        if (isinstance(pages, bool) or not isinstance(pages, int)
                or pages < (0 if allow_zero else 1)):
            raise ValueError("integer physical cache-page capacity required")
        return max(0 if allow_zero else 1, int(math.ceil(pages * fraction)))

    partition = scale(startup_pages) if startup_pages is not None else None
    rows = []
    for variant in variants:
        sb, os_pages = variant["shared_buffer_pages"], variant["os_cache_pages"]
        sampled_sb, sampled_os = scale(sb), scale(os_pages, True)
        if sampled_sb > 2000000:
            raise ValueError("sampled buffer-page allocation exceeds 2000000; use a smaller spatial sample")
        replay = replay_cache(
            materialized, shared_buffer_pages=sampled_sb, os_cache_pages=sampled_os,
            measured_workload_classes=("tp",), buffer_policy=buffer_policy,
            candidate_lists=candidate_lists, partition_pages=partition, ring_scale=fraction,
            session_threads=threads)
        stats = replay.stats
        rows.append({
            "shared_buffer_pages": sb, "os_cache_pages": os_pages,
            "sampled_shared_buffer_pages": sampled_sb,
            "sampled_os_cache_pages": sampled_os, **stats.path_fractions(),
            "sampled_TP_accesses": stats.accesses,
            "sampled_AP_accesses": accesses["ap"],
            "sampled_background_accesses": accesses["background"],
            "TP_SB_misses": stats.sb_misses, "TP_OS_hits": stats.os_hits,
            "TP_disk_misses": stats.disk_reads,
            "measured_state_anomalies": stats.measured_state_anomalies,
            "state_anomalies": stats.state_anomalies,
            "trace_state_screen_passed": (
                not stats.state_anomalies and not stats.measured_state_anomalies),
        })
    return {
        "schema": "jmt.conditional-mixed-TP-cache-curve/v1",
        "context": dict(context), "curve": rows, "events_replayed": len(materialized),
        "sample_ppm": sample_ppm,
        "buffer_policy": buffer_policy, "candidate_lists": candidate_lists,
        "startup_shared_buffer_pages": startup_pages,
        "sampled_startup_shared_buffer_pages": partition,
        "ring_scale": fraction,
        "supplied_session_thread_ids": sorted(set(threads)),
        "session_thread_evidence_completeness_checked": False,
        "initial_cache_state": "empty, then supplied warmup events; no native snapshot seeded",
        "sample_policy": "same spatial page hash for TP/AP; all selected-page state transitions required",
        "spatial_sample_membership_checked_by_this_replayer": False,
        "measured_workload_classes": ["tp"],
        "AP_accesses_mutate_shared_and_OS_cache": True,
        "AP_working_set_GiB_offset_fitted": False,
        "offline_replay_complexity": "O(cache_variants * sampled_events), bounded reference implementation",
        "online_use": "precomputed profile lookup; new WM/plan/rate/phase outside profile requires revalidation",
        "online_mixed_performance_evaluator_profile_ingestion_implemented": False,
        "counterfactual_schedule_feedback_modeled": False,
        "native_trace_loss_and_real_capacity_validation_still_required": True,
        "profile_transfer_to_other_AP_WM_or_rates_qualified": False,
        "mixed_hard_upper_bound_qualified": False,
        "automatic_recommendation_qualified": False,
    }
