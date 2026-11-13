"""Operator-derived WM candidates; bounded, SQL-ID-agnostic, no pool/SB input.

Candidate WM and DP budget quantization are DIFFERENT things. Every primary
point below comes from an operator requirement (0.5 M / M / 2 M), a hash
batch-size hypothesis M / 2**k, or a witnessed plan/operator transition.
There is no fixed 64..4096 grid and no claim of exhaustive/global optimality.
"""
import math

from jmt.common.memory_units import MAX_WORK_MEM_MIB, valid_work_mem_mib
from .curves import mode_signature

SCHEMA = "jmt.operator-derived-work-mem-candidates/v1"


def memory_anchors(forecast):
    """Use spill-check memory, NOT physical structural peak, as the anchor.

    Hash Join's demand duplicates its Hash child, so only the child's anchor
    is used. Hash Aggregate parent-context spill accounting is distinct from
    its parent+child physical-memory estimate. Non-hash aggregates are not
    workspace boundaries. Unknown/unsupported memory operators stay explicit.
    """
    result = []
    for op in forecast["operators"]:
        kind, mode, details = op["node_type"], op["mode"], op["details"]
        field = None
        if kind == "Hash":
            field = ("required_full_memory_mib" if "required_full_memory_mib" in details
                     else "required_memory_mib")
        elif kind in ("Aggregate", "HashAggregate", "SetOp") and (
                mode.startswith("hash_") or "spill_trigger_required_mib" in details):
            field = "spill_trigger_required_mib" if "spill_trigger_required_mib" in details else "required_mib"
        elif kind in ("Sort", "Materialize", "WindowAgg") and "required_mib" in details:
            field = "required_mib"
        if field is None or field not in details:
            continue
        value = details[field]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            raise ValueError("invalid operator memory requirement")
        if value > 0:
            result.append({
                "operator_path": op["path"], "node_type": kind, "mode": mode,
                "requirement_field": field, "required_mib": value,
                "plan_structure_sha256": forecast["plan_structure_sha256"],
                "source_work_mem_mib": forecast["work_mem_mib"],
                "requirement_is_estimated_not_actual_grant": True,
            })
    return result


def derive_candidates(evaluate, seed_mib, *, min_mib=1, max_mib=MAX_WORK_MEM_MIB,
                      max_points=192, max_refinements=64, smooth_segments=16):
    """evaluate(WM) obtains a BLIND plan at that WM and forecasts it.

    Each new plan shape contributes operator anchors once; actual plans at
    other WMs are never substituted from the seed. Plan switches get priority
    in bounded integer-MiB refinement. Hash batch and merge-pass flips are
    refined only where they are witnessed, not assumed monotone globally.
    """
    if (not all(valid_work_mem_mib(v) for v in (seed_mib, min_mib, max_mib))
            or not min_mib <= seed_mib <= max_mib
            or isinstance(max_points, bool) or not isinstance(max_points, int) or not 8 <= max_points <= 256
            or isinstance(max_refinements, bool) or not isinstance(max_refinements, int) or not 0 <= max_refinements <= 128
            or isinstance(smooth_segments, bool) or not isinstance(smooth_segments, int) or not 2 <= smooth_segments <= 32):
        raise ValueError("bounded engine WM domain, seed and candidate/refinement counts required")
    forecasts, reasons, anchors, seen_shapes = {}, {}, [], set()
    excluded, unprocessed, rounds = [], [], 0

    def propose(value, evidence):
        point = max(min_mib, int(math.ceil(value)))
        if point > max_mib:
            excluded.append({"proposed_work_mem_mib": point, "reason": evidence,
                             "exclusion": "outside_explicit_engine_domain_not_silently_clipped"})
            return
        record = dict(evidence)
        record.update(unrounded_mib=value, rounded_up_mib=point)
        if value < min_mib:
            record["clamped_to_engine_or_caller_minimum"] = True
        if record not in reasons.setdefault(point, []):
            reasons[point].append(record)

    def acquire(point):
        nonlocal rounds
        if point in forecasts:
            return
        value = evaluate(point)
        if value["work_mem_mib"] != point or value.get("uses_target_execution_labels"):
            raise ValueError("candidate evaluation must be a matching blind WM forecast")
        forecasts[point] = value
        shape = value["plan_structure_sha256"]
        if shape in seen_shapes:
            return
        seen_shapes.add(shape)
        rounds += 1
        for anchor in memory_anchors(value):
            anchors.append(anchor)
            m = anchor["required_mib"]
            # Exact PPT construction, with explicit operator evidence.
            for factor in (1.0, 0.5, 2.0):
                propose(m * factor, {"kind": "operator_requirement_multiple",
                                     "factor": factor, **anchor})
            boundary = math.ceil(m)
            if boundary > min_mib:
                propose(boundary - 1, {"kind": "one_MiB_below_requirement", **anchor})
            if anchor["node_type"] == "Hash":
                # hypotheses only: source bucket rounding / skew can shift
                # the real modeled transition, which is evaluated/refined.
                for exponent in range(2, 32):
                    divided = m / (2 ** exponent)
                    if divided < min_mib:
                        break
                    propose(divided, {"kind": "hash_batch_scale_hypothesis",
                                      "batch_scale": 2 ** exponent, **anchor})
            elif anchor["node_type"] in ("Aggregate", "HashAggregate", "SetOp", "Materialize", "WindowAgg"):
                # Aggregate spill is often continuous as resident groups grow.
                # ONLY 0.5M/M/2M can leave most of a valid budget unusable.
                # These knots scale with THIS operator's modeled M, not a
                # common 64/128/... WM grid. Still bounded and not a claim of
                # continuous optimality; DP can retain all beneficial knots.
                for numerator in range(1, smooth_segments):
                    propose(m * numerator / smooth_segments, {
                        "kind": "operator_scaled_continuous_spill_segment",
                        "numerator": numerator, "denominator": smooth_segments, **anchor})

    propose(seed_mib, {"kind": "server_default_WM_plan_probe_not_a_fixed_search_grid"})
    propose(min_mib, {"kind": "legal_WM_domain_lower_endpoint"})
    while len(forecasts) < max_points:
        pending = sorted(set(reasons) - set(forecasts))
        if not pending:
            break
        for point in pending:
            if len(forecasts) == max_points:
                break
            acquire(point)

    refinements = []
    for _ in range(max_refinements):
        if len(forecasts) >= max_points:
            break
        intervals = []
        ordered = sorted(forecasts)
        for left, right in zip(ordered, ordered[1:]):
            if right - left <= 1:
                continue
            lf, rf = forecasts[left], forecasts[right]
            plan_changed = lf["plan_structure_sha256"] != rf["plan_structure_sha256"]
            if not plan_changed and mode_signature(lf) == mode_signature(rf):
                continue
            # Prioritize plan switches, then the largest witnessed IO effect.
            li = lf.get("logical_temp_read_bytes_estimate") or 0
            li += lf.get("logical_temp_write_bytes_estimate") or 0
            ri = rf.get("logical_temp_read_bytes_estimate") or 0
            ri += rf.get("logical_temp_write_bytes_estimate") or 0
            intervals.append((not plan_changed, -abs(li - ri), -(right - left), left, right))
        if not intervals:
            break
        _, _, _, left, right = min(intervals)
        point = (left + right) // 2
        evidence = {"kind": "witnessed_plan_or_operator_transition_refinement",
                    "left_mib": left, "right_mib": right,
                    "plan_changed": forecasts[left]["plan_structure_sha256"] != forecasts[right]["plan_structure_sha256"]}
        propose(point, evidence)
        refinements.append({"work_mem_mib": point, **evidence})
        acquire(point)
        # A newly discovered plan can add new primary operator anchors.
        pending = sorted(set(reasons) - set(forecasts))
        for added in pending:
            if len(forecasts) == max_points:
                break
            acquire(added)
    unprocessed = sorted(set(reasons) - set(forecasts))
    return [forecasts[p] for p in sorted(forecasts)], {
        "schema": SCHEMA, "generation_method": "operator_requirements_then_witnessed_transition_refinement",
        "WM_unit": "integer MiB; round up thresholds, preserve below-boundary neighbor",
        "domain_min_mib": min_mib, "domain_max_mib": max_mib,
        "seed_mib": seed_mib, "anchors": anchors,
        "candidates": [{"work_mem_mib": p, "reasons": reasons[p]} for p in sorted(forecasts)],
        "unique_plan_shapes": len(seen_shapes), "operator_anchor_expansions": rounds,
        "evaluations": len(forecasts), "max_evaluations": max_points,
        "smooth_operator_segment_count": smooth_segments,
        "transition_refinements": refinements, "max_transition_refinements": max_refinements,
        "unprocessed_proposals_mib": unprocessed, "excluded_requirements": excluded,
        "fixed_WM_grid_used": False, "all_possible_plan_switches_or_interior_WMs_covered": False,
        "optimum_scope": "these finite modeled candidates only; not a continuous WM/global optimum",
        "accuracy_or_actual_operator_grants_validated": False,
    }
