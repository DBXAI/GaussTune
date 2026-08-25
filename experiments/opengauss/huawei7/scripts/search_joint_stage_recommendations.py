#!/usr/bin/env python3
"""Search every valid native candidate with the joint CPU/IO model.

The v11 command applied the joint model to the already frozen native-best
candidate.  That is useful for validation, but it cannot prove that a stage
configuration is optimal and it cannot demonstrate a gain over one fixed
configuration.  This command closes that gap:

* every native candidate for every PPT stage is scored;
* TP CPU demand is selected from resource features, never a benchmark label;
* AP CPU work is anchored to the independent CPU measurement; the optional
  resource-decomposition mode scales that anchor only by non-device work from
  the independent AP bundle, never by TPS;
* AP wall time and physical request work come from the independent AP model
  bundle for that candidate's work_mem;
* the finite-slot AP closure, CPU queue, FIO queue, and optional database
  buffered path are solved together;
* candidates outside a measured resource domain are rejected rather than
  silently receiving zero contention;
* the selected candidate is written as a model-result wrapper whose ``best``
  is the searched candidate, so the recommendation remains executable by the
  existing stage runner.

This is a diagnostic recommendation search until a real end-to-end holdout
has been run with the newly selected, stage-specific configurations.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.ap_closed_loop import APClosedLoopSpec, APQueryDemand
from huawei7.buffered_path import surface_from_document
from huawei7.cpu_io_surface import predict_stage_with_cpu_io_surface
from huawei7.cpu_surface import effective_cpu_capacity_seconds
from huawei7.device import ServiceTimes
from huawei7.provenance import sha256
from huawei7.stage_spec import Stage, read_stage_spec
from huawei7.workload_features import select_tp_workload_by_features
from huawei7.mixed_resource import summarize_mixed_resource

from scripts.apply_cpu_io_surface import (
    _load_ap_buffer_demand,
    _load_cpu,
    _load_empirical,
    _load_fio_reports,
    _load_tp_feature_catalog,
    _metric_at_shared_buffers,
    _select_fio,
)


class CandidateRejected(ValueError):
    """A candidate cannot be scored without leaving a measured domain."""


@dataclasses.dataclass(frozen=True)
class MixedResourceAnchor:
    """Resource-only mixed TP/AP anchor used for feature projection.

    The anchor carries resource deltas, not a throughput correction.  Candidate
    work is projected from intrinsic AP features (CPU work, logical pages and
    physical request rate); no benchmark label or observed mixed TPS is used
    during selection.
    """

    source_path: str
    source_sha256: str
    benchmark_provenance: Optional[str]
    stage: str
    ap_queries: tuple
    tp_terminals: int
    shared_buffers_mb: int
    ap_features: Mapping[str, float]
    native_tp_cpu_ms_per_tx: float
    shared_cpu_delta_per_tx: float
    native_tp_read_requests_per_tx: float
    native_tp_buffer_accesses_per_tx: float
    mixed_tp_read_requests_per_tx: float
    mixed_tp_buffer_accesses_per_tx: float
    read_delta_per_tx: float
    buffer_delta_per_tx: float
    resource_summary: Mapping[str, object]


def _load_json(path: Path) -> Mapping[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _candidate_work_mem(candidate: Mapping[str, object]) -> Mapping[str, int]:
    rows = candidate.get("work_mem")
    if not isinstance(rows, list):
        raise CandidateRejected("candidate_work_mem_missing")
    result = {}
    for raw in rows:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise CandidateRejected("candidate_work_mem_invalid")
        result[str(int(raw[0]))] = int(raw[1])
    if not result:
        raise CandidateRejected("candidate_work_mem_empty")
    return result


def _same_configuration(
    candidate: Mapping[str, object],
    shared_buffers_mb: int,
    work_mem_by_query: Mapping[str, object],
) -> bool:
    try:
        candidate_wm = _candidate_work_mem(candidate)
    except CandidateRejected:
        return False
    wanted = {
        str(query): int(value)
        for query, value in work_mem_by_query.items()
    }
    return (
        int(candidate.get("shared_buffers_mb", -1)) == int(shared_buffers_mb)
        and candidate_wm == wanted
    )


def _load_full_ap_options(
    path: Path,
    machine_fingerprint: str,
) -> tuple[Mapping[str, object], Mapping[str, Mapping[int, Mapping[str, object]]]]:
    document = _load_json(path)
    if (
        document.get("schema") != "huawei7.ap-model-bundle/v1"
        or document.get("valid") is not True
        or document.get("machine_fingerprint") != machine_fingerprint
    ):
        raise ValueError("AP model bundle is invalid or belongs to another machine")
    raw_options = document.get("query_options")
    if not isinstance(raw_options, dict):
        raise ValueError("AP model bundle lacks query_options")
    options = {}
    for raw_query, rows in raw_options.items():
        if not isinstance(rows, list) or not rows:
            raise ValueError("AP model bundle query %s has no options" % raw_query)
        query = str(raw_query)
        by_work_mem = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid AP model option for query %s" % query)
            work_mem = int(round(float(row["work_mem_mb"])))
            if work_mem in by_work_mem:
                raise ValueError(
                    "duplicate AP model option q%s wm%d" % (query, work_mem)
                )
            for key in (
                "cpu_operations",
                "execution_seconds",
                "read_requests",
                "write_requests",
                "logical_read_pages",
                "logical_write_pages",
            ):
                value = float(row[key])
                if value < 0 or not value == value:
                    raise ValueError(
                        "invalid AP model option %s for q%s" % (key, query)
                    )
            if float(row["execution_seconds"]) <= 0:
                raise ValueError("AP execution_seconds must be positive")
            by_work_mem[work_mem] = row
        options[query] = by_work_mem
    return document, options


def _load_ap_cpu_anchor_surface(
    path: Optional[Path],
    machine_fingerprint: str,
) -> Mapping[str, Sequence[Mapping[str, object]]]:
    if path is None:
        return {}
    document = _load_json(path)
    if (
        document.get("schema") != "huawei7.ap-cpu-anchor-surface/v1"
        or document.get("valid") is not True
        or document.get("machine_fingerprint") != machine_fingerprint
        or document.get("calibration_contract", {}).get(
            "final_stage_tps_used"
        ) is not False
        or document.get("calibration_contract", {}).get(
            "mixed_tp_ap_tps_used"
        ) is not False
    ):
        raise ValueError("AP CPU anchor surface is invalid or leakage-prone")
    grouped = {}
    for row in document.get("rows", []):
        query = str(row["query"])
        grouped.setdefault(query, []).append({
            "work_mem_mb": int(row["work_mem_mb"]),
            "plan_family": row.get("plan_family"),
            "cpu_seconds_per_query": float(
                row["cpu_seconds_per_query"]
            ),
            "wall_seconds_per_query": float(
                row["wall_seconds_per_query"]
            ),
            "repeats": int(row["repeats"]),
            "coefficient_of_variation": float(
                row["coefficient_of_variation"]
            ),
            "source": row.get("source"),
        })
    for rows in grouped.values():
        rows.sort(key=lambda row: int(row["work_mem_mb"]))
    return grouped


def _machine_reference_wm(
    ap_demands: Mapping[str, object],
) -> Mapping[str, int]:
    """Recover the measured AP CPU anchor's work_mem from provenance paths."""

    result = {}
    pattern = re.compile(r"ap-q(\d+)-wm(\d+)")
    for query, demand in ap_demands.items():
        for artifact in getattr(demand, "source_artifacts", ()):
            match = pattern.search(str(artifact.get("path", "")))
            if match:
                result[str(query)] = int(match.group(2))
                break
    return result


def _reason(exc: BaseException) -> str:
    text = str(exc)
    if "AP read fraction" in text:
        return "fio_ap_mix_out_of_domain"
    if "AP queue depth" in text:
        return "buffered_pressure_out_of_domain"
    if "finite-slot AP closure did not converge" in text:
        return "ap_closed_loop_not_converged"
    if "TP resource feature" in text or "tp_resource_feature" in text:
        return "tp_resource_feature_out_of_domain"
    if "queue depth" in text:
        return "fio_queue_domain_out_of_domain"
    if "work_mem" in text or "AP model option" in text:
        return "ap_work_mem_option_missing"
    return type(exc).__name__ + ":" + text


def _fixed_profile_configuration(
    profile: Mapping[str, object],
    benchmark: str,
    stage: str,
) -> tuple[int, Mapping[str, int]]:
    rows = [
        row for row in profile.get("stages", [])
        if isinstance(row, dict)
        and str(row.get("benchmark")) == benchmark
        and str(row.get("stage")) == stage
    ]
    if len(rows) != 1:
        raise ValueError("fixed profile lacks exactly one %s/%s row" % (benchmark, stage))
    row = rows[0]
    raw = row.get("work_mem_by_query")
    if not isinstance(raw, dict):
        raise ValueError("fixed profile row lacks work_mem_by_query")
    return int(row["shared_buffers_mb"]), {
        str(query): int(value) for query, value in raw.items()
    }


@dataclasses.dataclass(frozen=True)
class SearchContext:
    cpu_document: Mapping[str, object]
    ap_demands: Mapping[str, object]
    tp_demands: Mapping[str, object]
    ap_buffer_demands: Mapping[str, float]
    ap_options: Mapping[str, Mapping[int, Mapping[str, object]]]
    tp_feature_document: Mapping[str, object]
    tp_feature_rows: Sequence[Mapping[str, object]]
    fio_reports: Sequence[object]
    service: ServiceTimes
    buffered_surface: object
    effective_capacity: float
    capacity_limit: float
    machine_fingerprint: str
    ap_reference_work_mem: Mapping[str, int]
    ap_buffer_pressure_mode: str
    ap_cpu_work_mode: str
    allow_plan_family_extrapolation: bool
    ap_cpu_anchor_rows: Mapping[str, Sequence[Mapping[str, object]]]
    mixed_resource_anchors: Sequence[MixedResourceAnchor] = ()


def _buffered_surface_for_candidate(
    *,
    context: SearchContext,
    stage: Stage,
    candidate: Mapping[str, object],
    native_tp_buffer_accesses_per_tx: float,
) -> tuple[
    object,
    Optional[Mapping[str, object]],
    Optional[float],
    Sequence[Mapping[str, object]],
]:
    """Return the applicable buffered surface and its measured pressure.

    A surface that matches TP features but fails its AP physical-mix domain is
    a hard rejection.  Treating that case as "no buffered path" would let an
    invalid candidate win by escaping the very latency layer that matters.
    If TP features do not match the surface baseline, this particular surface
    is not applicable; another workload family may have its own surface.
    """

    if context.buffered_surface is None:
        return None, None, None, ()
    catalog = (
        tuple(context.buffered_surface)
        if isinstance(context.buffered_surface, (tuple, list))
        else (context.buffered_surface,)
    )
    matches = []
    for surface in catalog:
        match = surface.workload_feature_match(
            tp_terminals=stage.tp_terminals,
            native_tp_buffer_accesses_per_tx=native_tp_buffer_accesses_per_tx,
            ap_read_iops=float(candidate["ap_read_iops"]),
            ap_write_iops=float(candidate["ap_write_iops"]),
        )
        if match["matched"]:
            matches.append((surface, match))
    if matches:
        # A candidate may lie in more than one explicitly measured terminal
        # domain.  Prefer the closest intrinsic TP access coordinate, then
        # keep path ordering deterministic.
        matches.sort(
            key=lambda item: (
                float(item[1].get("relative_tp_buffer_access_distance", 0.0)),
                int(item[0].tp_terminals or 0),
            )
        )
        surface, match = matches[0]
        missing = [
            str(query) for query in stage.ap_queries
            if str(query) not in context.ap_buffer_demands
        ]
        if missing:
            raise CandidateRejected("ap_buffer_demand_query_missing")
        pressure, pressure_terms = _candidate_ap_buffer_pressure(
            context=context,
            stage=stage,
            candidate=candidate,
        )
        return surface, match, pressure, pressure_terms
    for surface in catalog:
        match = surface.workload_feature_match(
            tp_terminals=stage.tp_terminals,
            native_tp_buffer_accesses_per_tx=native_tp_buffer_accesses_per_tx,
            ap_read_iops=float(candidate["ap_read_iops"]),
            ap_write_iops=float(candidate["ap_write_iops"]),
        )
        if (
            match.get("terminal_reason") == "exact_terminal_match"
            and match.get("access_reason") == "baseline_access_feature_match"
            and match.get("ap_mix_match") is False
        ):
            raise CandidateRejected("buffered_path_ap_mix_out_of_domain")
    return None, None, None, ()


def _candidate_ap_buffer_pressure(
    *,
    context: SearchContext,
    stage: Stage,
    candidate: Mapping[str, object],
) -> tuple[float, Sequence[Mapping[str, object]]]:
    """Project active AP buffer pressure without a TPS correction.

    The buffered-path surface is measured at one work_mem point per AP query.
    Keeping that pressure unchanged for every candidate makes the buffered
    layer blind to work_mem, even though the independent AP bundle contains
    logical-page and residence-time features for every candidate.  The
    optional projection below uses only those physical features:

        pressure_candidate / pressure_anchor
          = (logical_pages_candidate / logical_pages_anchor)
            * (execution_time_anchor / execution_time_candidate)

    This is a rate decomposition, not a fitted multiplier: it preserves each
    measured AP buffer-rate anchor and only changes it according to the
    candidate's measured logical work and isolated residence time.  The
    default legacy mode remains available for reproducibility.
    """

    work_mem = _candidate_work_mem(candidate)
    pressure = 0.0
    terms = []
    for raw_query in stage.ap_queries:
        query = str(raw_query)
        base_rate = float(context.ap_buffer_demands[query])
        candidate_option = context.ap_options[query].get(int(work_mem[query]))
        reference_work_mem = context.ap_reference_work_mem.get(query)
        reference_option = (
            context.ap_options[query].get(reference_work_mem)
            if reference_work_mem is not None else None
        )
        if candidate_option is None:
            raise CandidateRejected(
                "ap_work_mem_option_missing_q%s_wm%s"
                % (query, work_mem[query])
            )
        if reference_option is None:
            raise CandidateRejected(
                "ap_reference_work_mem_option_missing_q%s" % query
            )

        ratio = 1.0
        if context.ap_buffer_pressure_mode == (
            "candidate_logical_page_rate_projection"
        ):
            candidate_pages = float(candidate_option["logical_read_pages"])
            reference_pages = float(reference_option["logical_read_pages"])
            candidate_seconds = float(candidate_option["execution_seconds"])
            reference_seconds = float(reference_option["execution_seconds"])
            if (
                candidate_pages <= 0
                or reference_pages <= 0
                or candidate_seconds <= 0
                or reference_seconds <= 0
            ):
                raise CandidateRejected(
                    "ap_buffer_projection_feature_nonpositive_q%s" % query
                )
            ratio = (
                candidate_pages / reference_pages
                * reference_seconds / candidate_seconds
            )
        elif context.ap_buffer_pressure_mode != (
            "measured_stage_domain_pressure_no_work_mem_extrapolation"
        ):
            raise CandidateRejected(
                "unknown_ap_buffer_pressure_mode_%s"
                % context.ap_buffer_pressure_mode
            )

        contribution = base_rate * ratio
        pressure += contribution
        terms.append({
            "query": query,
            "anchor_work_mem_mb": int(reference_work_mem),
            "candidate_work_mem_mb": int(work_mem[query]),
            "anchor_buffer_accesses_per_second": base_rate,
            "projection_ratio": ratio,
            "candidate_buffer_accesses_per_second": contribution,
            "method": context.ap_buffer_pressure_mode,
        })
    return pressure, terms


def _candidate_ap_intrinsic_features(
    *,
    context: SearchContext,
    stage: Stage,
    candidate: Mapping[str, object],
) -> Mapping[str, float]:
    """Return AP work features that are observable without mixed TPS.

    Every term is derived from the independent AP option bundle.  A single
    active AP slot contributes its measured per-query work divided by its
    isolated residence time; this is an offered resource rate, not a
    throughput calibration.  These features are deliberately kept in
    physical units so the same projection can be reused for another workload
    whose label is unknown.
    """

    work_mem = _candidate_work_mem(candidate)
    cpu_work = 0.0
    logical_pages = 0.0
    read_iops = 0.0
    write_iops = 0.0
    for raw_query in stage.ap_queries:
        query = str(raw_query)
        option = context.ap_options.get(query, {}).get(
            int(work_mem.get(query, -1))
        )
        if option is None:
            raise CandidateRejected(
                "ap_work_mem_option_missing_q%s_wm%s"
                % (query, work_mem.get(query))
            )
        seconds = float(option["execution_seconds"])
        if seconds <= 0:
            raise CandidateRejected(
                "ap_intrinsic_feature_nonpositive_residence_q%s" % query
            )
        cpu_work += float(option["cpu_operations"]) / seconds
        logical_pages += float(option["logical_read_pages"]) / seconds
        read_iops += float(option["read_requests"]) / seconds
        write_iops += float(option["write_requests"]) / seconds
    buffer_rate, _ = _candidate_ap_buffer_pressure(
        context=context,
        stage=stage,
        candidate=candidate,
    )
    return {
        "ap_cpu_work_per_second": cpu_work,
        "ap_logical_pages_per_second": logical_pages,
        "ap_read_iops": read_iops,
        "ap_write_iops": write_iops,
        "ap_buffer_accesses_per_second": buffer_rate,
    }


def _mixed_resource_projection(
    *,
    context: SearchContext,
    stage: Stage,
    candidate: Mapping[str, object],
    native_tp_buffer_accesses_per_tx: float,
) -> Optional[Mapping[str, object]]:
    """Project measured mixed resource deltas for an unseen AP candidate.

    Anchors are only compared inside the same terminal/SB/query-set domain.
    Within that measured domain, deltas are scaled by ratios of independent AP
    resource features.  In particular, no observed mixed-stage TPS and no
    benchmark-name coefficient enter the calculation.  If a candidate leaves
    the anchor domain, it remains on the original joint model rather than
    receiving a silent extrapolation.
    """

    if not context.mixed_resource_anchors:
        return None
    features = _candidate_ap_intrinsic_features(
        context=context,
        stage=stage,
        candidate=candidate,
    )
    applicable = []
    for anchor in context.mixed_resource_anchors:
        if (
            tuple(sorted(str(q) for q in anchor.ap_queries))
            != tuple(sorted(str(q) for q in stage.ap_queries))
            or int(anchor.tp_terminals) != int(stage.tp_terminals)
            or int(anchor.shared_buffers_mb)
            != int(candidate["shared_buffers_mb"])
        ):
            continue
        distances = {}
        for key in (
            "ap_cpu_work_per_second",
            "ap_logical_pages_per_second",
            "ap_read_iops",
            "ap_buffer_accesses_per_second",
        ):
            reference = float(anchor.ap_features[key])
            value = float(features[key])
            distances[key] = abs(value - reference) / max(
                abs(value), abs(reference), 1e-9
            )
        applicable.append((anchor, distances))
    if not applicable:
        return None

    # A fixed, symmetric physical-feature gate avoids extrapolating beyond
    # twice the measured resource domain.  The threshold is a domain rule,
    # not a fitted performance parameter.
    maximum_distance = 0.75
    applicable = [
        (anchor, distances)
        for anchor, distances in applicable
        if max(distances.values()) <= maximum_distance
    ]
    if not applicable:
        return None

    weights = []
    for anchor, distances in applicable:
        weights.append(
            1.0 / max(
                sum(distances.values()) / len(distances),
                1e-9,
            )
        )
    weight_sum = sum(weights)
    cpu_delta = 0.0
    read_delta = 0.0
    buffer_delta = 0.0
    selected = []
    for (anchor, distances), weight in zip(applicable, weights):
        read_ratio = float(features["ap_read_iops"]) / max(
            float(anchor.ap_features["ap_read_iops"]), 1e-12
        )
        cpu_ratio = float(
            features["ap_cpu_work_per_second"]
        ) / max(
            float(anchor.ap_features["ap_cpu_work_per_second"]),
            1e-12,
        )
        buffer_ratio = float(
            features["ap_buffer_accesses_per_second"]
        ) / max(
            float(anchor.ap_features["ap_buffer_accesses_per_second"]),
            1e-12,
        )
        normalized_weight = weight / weight_sum
        cpu_delta += (
            normalized_weight
            * anchor.shared_cpu_delta_per_tx
            * cpu_ratio
        )
        read_delta += normalized_weight * anchor.read_delta_per_tx * read_ratio
        buffer_delta += (
            normalized_weight
            * anchor.buffer_delta_per_tx
            * buffer_ratio
        )
        selected.append({
            "source_path": anchor.source_path,
            "source_sha256": anchor.source_sha256,
            "benchmark_provenance": anchor.benchmark_provenance,
            "feature_distances": distances,
            "weight": normalized_weight,
            "cpu_ratio": cpu_ratio,
            "read_ratio": read_ratio,
            "buffer_ratio": buffer_ratio,
            "shared_cpu_delta_per_tx": anchor.shared_cpu_delta_per_tx,
            "read_delta_per_tx": anchor.read_delta_per_tx,
            "buffer_delta_per_tx": anchor.buffer_delta_per_tx,
        })
    return {
        "method": "resource-only-ap-feature-scaled-interaction-v2",
        "selection_uses_benchmark_name": False,
        "prediction_uses_mixed_stage_tps": False,
        "domain_maximum_relative_feature_distance": maximum_distance,
        "candidate_ap_features": dict(features),
        "candidate_native_tp_buffer_accesses_per_tx": (
            float(native_tp_buffer_accesses_per_tx)
        ),
        "projected_shared_cpu_delta_ms_per_tx": cpu_delta,
        "projected_read_delta_per_tx": read_delta,
        "projected_buffer_delta_per_tx": buffer_delta,
        "selected_anchors": selected,
    }


def _score_candidate(
    *,
    context: SearchContext,
    stage: Stage,
    benchmark: str,
    model_document: Mapping[str, object],
    candidate: Mapping[str, object],
) -> Mapping[str, object]:
    (fio_fraction, fio_surface, fio_path), actual_fraction = _select_fio(
        context.fio_reports,
        float(candidate["ap_read_iops"]),
        float(candidate["ap_write_iops"]),
    )
    empirical = _load_empirical(model_document)
    native_accesses = _metric_at_shared_buffers(
        empirical,
        int(candidate["shared_buffers_mb"]),
        "buffer_accesses_per_tx",
    )
    tp_feature_match = select_tp_workload_by_features(
        candidate={
            "tp_terminals": stage.tp_terminals,
            "tp_cpu_ms_per_tx": 0.0,
            "tp_read_requests_per_tx": float(
                candidate["tp_read_requests_per_tx"]
            ),
            "tp_write_requests_per_tx": float(
                candidate["tp_write_requests_per_tx"]
            ),
            "tp_buffer_accesses_per_tx": float(native_accesses),
            "p_disk": float(candidate["p_disk"]),
        },
        candidates=context.tp_feature_rows,
        maximum_relative_feature_distance=float(
            context.tp_feature_document["maximum_relative_feature_distance"]
        ),
    )
    if not tp_feature_match.get("matched"):
        raise CandidateRejected("tp_resource_feature_out_of_domain")
    tp_key = str(tp_feature_match["selected_demand_key"])
    if tp_key not in context.tp_demands:
        raise CandidateRejected("selected_tp_cpu_demand_missing")

    (
        buffered_surface,
        buffered_match,
        buffered_pressure,
        buffered_pressure_terms,
    ) = (
        _buffered_surface_for_candidate(
            context=context,
            stage=stage,
            candidate=candidate,
            native_tp_buffer_accesses_per_tx=float(native_accesses),
        )
    )
    work_mem = _candidate_work_mem(candidate)
    closed_demands = []
    cpu_feature_anchors = []
    for query_number in stage.ap_queries:
        query = str(query_number)
        if query not in context.ap_demands:
            raise CandidateRejected("ap_cpu_demand_missing_q%s" % query)
        if query not in context.ap_options:
            raise CandidateRejected("ap_work_mem_options_missing_q%s" % query)
        if query not in work_mem:
            raise CandidateRejected("candidate_work_mem_missing_q%s" % query)
        option = context.ap_options[query].get(int(work_mem[query]))
        if option is None:
            raise CandidateRejected(
                "ap_work_mem_option_missing_q%s_wm%s"
                % (query, work_mem[query])
            )
        demand = context.ap_demands[query]
        cpu_seconds = float(demand.cpu_seconds_per_unit)
        cpu_feature_ratio = None
        cpu_feature_anchor = None
        cpu_source = "measured_query_anchor"
        candidate_work_mem = int(work_mem[query])
        reference_work_mem = context.ap_reference_work_mem.get(query)
        reference_option = (
            context.ap_options[query].get(reference_work_mem)
            if reference_work_mem is not None else None
        )
        if reference_option is None:
            raise CandidateRejected(
                "ap_reference_work_mem_option_missing_q%s" % query
            )
        anchor_points = list(context.ap_cpu_anchor_rows.get(query, ()))
        if len(anchor_points) >= 2:
            anchor_points.sort(
                key=lambda row: int(row["work_mem_mb"])
            )
            minimum_anchor = int(anchor_points[0]["work_mem_mb"])
            maximum_anchor = int(anchor_points[-1]["work_mem_mb"])
            if not minimum_anchor <= candidate_work_mem <= maximum_anchor:
                raise CandidateRejected(
                    "ap_cpu_anchor_work_mem_out_of_domain_q%s" % query
                )
            exact = next(
                (
                    row for row in anchor_points
                    if int(row["work_mem_mb"]) == candidate_work_mem
                ),
                None,
            )
            if exact is not None:
                cpu_seconds = float(exact["cpu_seconds_per_query"])
                cpu_source = "measured_sparse_cpu_anchor"
                cpu_feature_anchors.append({
                    "query": query,
                    "candidate_work_mem_mb": candidate_work_mem,
                    "method": cpu_source,
                    "cpu_seconds_per_query": cpu_seconds,
                    "anchor_source": exact.get("source"),
                })
            else:
                lower = max(
                    (
                        row for row in anchor_points
                        if int(row["work_mem_mb"]) < candidate_work_mem
                    ),
                    key=lambda row: int(row["work_mem_mb"]),
                )
                upper = min(
                    (
                        row for row in anchor_points
                        if int(row["work_mem_mb"]) > candidate_work_mem
                    ),
                    key=lambda row: int(row["work_mem_mb"]),
                )
                lower_wm = int(lower["work_mem_mb"])
                upper_wm = int(upper["work_mem_mb"])
                candidate_plan_family = str(
                    option.get("plan_family", "")
                )
                lower_plan_family = str(
                    lower.get("plan_family") or ""
                )
                upper_plan_family = str(
                    upper.get("plan_family") or ""
                )
                if (
                    lower_plan_family
                    and upper_plan_family
                    and lower_plan_family != upper_plan_family
                ):
                    raise CandidateRejected(
                        "ap_cpu_anchor_plan_family_interpolation_out_of_domain_q%s"
                        % query
                    )
                if lower_plan_family and (
                    candidate_plan_family != lower_plan_family
                ):
                    raise CandidateRejected(
                        "ap_cpu_anchor_plan_family_interpolation_out_of_domain_q%s"
                        % query
                    )
                if upper_plan_family and (
                    candidate_plan_family != upper_plan_family
                ):
                    raise CandidateRejected(
                        "ap_cpu_anchor_plan_family_interpolation_out_of_domain_q%s"
                        % query
                    )
                weight = (
                    (candidate_work_mem - lower_wm)
                    / float(upper_wm - lower_wm)
                )
                cpu_seconds = (
                    float(lower["cpu_seconds_per_query"])
                    + weight * (
                        float(upper["cpu_seconds_per_query"])
                        - float(lower["cpu_seconds_per_query"])
                    )
                )
                cpu_source = "measured_sparse_cpu_piecewise_linear"
                cpu_feature_anchors.append({
                    "query": query,
                    "candidate_work_mem_mb": candidate_work_mem,
                    "method": cpu_source,
                    "lower_anchor_work_mem_mb": lower_wm,
                    "upper_anchor_work_mem_mb": upper_wm,
                    "lower_cpu_seconds_per_query": float(
                        lower["cpu_seconds_per_query"]
                    ),
                    "upper_cpu_seconds_per_query": float(
                        upper["cpu_seconds_per_query"]
                    ),
                    "interpolation_weight": weight,
                })

        if (
            cpu_source == "measured_query_anchor"
            and context.ap_cpu_work_mode == "operator-feature-anchor"
        ):
            reference_cpu_operations = float(
                reference_option["cpu_operations"]
            )
            candidate_cpu_operations = float(option["cpu_operations"])
            if reference_cpu_operations <= 0 or candidate_cpu_operations <= 0:
                raise CandidateRejected(
                    "ap_cpu_work_feature_nonpositive_q%s" % query
                )
            cpu_feature_ratio = (
                candidate_cpu_operations / reference_cpu_operations
            )
            cpu_feature_anchor = {
                "query": query,
                "reference_work_mem_mb": int(reference_work_mem),
                "reference_cpu_operations": reference_cpu_operations,
                "candidate_cpu_operations": candidate_cpu_operations,
                "ratio": cpu_feature_ratio,
                "reference_plan_family": str(
                    reference_option.get("plan_family", "")
                ),
                "candidate_plan_family": str(
                    option.get("plan_family", "")
                ),
                "same_plan_family": (
                    reference_option.get("plan_family")
                    == option.get("plan_family")
                ),
            }
            if (
                not cpu_feature_anchor["same_plan_family"]
                and not context.allow_plan_family_extrapolation
            ):
                raise CandidateRejected(
                    "ap_cpu_plan_family_out_of_domain_q%s" % query
                )
            cpu_seconds *= cpu_feature_ratio
            cpu_feature_anchors.append(cpu_feature_anchor)

        if (
            cpu_source == "measured_query_anchor"
            and context.ap_cpu_work_mode == "resource-decomposition"
        ):
            candidate_plan_family = str(option.get("plan_family", ""))
            reference_plan_family = str(
                reference_option.get("plan_family", "")
            )
            if (
                candidate_plan_family != reference_plan_family
                and not context.allow_plan_family_extrapolation
            ):
                raise CandidateRejected(
                    "ap_cpu_plan_family_out_of_domain_q%s" % query
                )

            def non_device_work(option: Mapping[str, object]) -> float:
                residual = (
                    float(option["execution_seconds"])
                    - float(option["read_requests"])
                    * context.service.ap_read_ms / 1000.0
                    - float(option["write_requests"])
                    * context.service.ap_write_ms / 1000.0
                )
                return max(residual, 1e-6)

            reference_non_device_work = non_device_work(reference_option)
            candidate_non_device_work = non_device_work(option)
            # This is a deterministic resource decomposition, not a fitted
            # contention multiplier: preserve the measured CPU anchor at its
            # measured work_mem and scale only by the candidate's estimated
            # non-device work from the independent AP bundle.
            cpu_seconds *= (
                candidate_non_device_work / reference_non_device_work
            )
        closed_demands.append(
            APQueryDemand(
                key=query,
                slots=1,
                cpu_seconds_per_query=cpu_seconds,
                wall_seconds_per_query=float(option["execution_seconds"]),
                buffer_accesses_per_query=(
                    float(context.ap_buffer_demands.get(query, 0.0))
                    * (
                        float(option["logical_read_pages"])
                        / max(
                            float(reference_option["logical_read_pages"])
                            if reference_option is not None
                            else float(option["logical_read_pages"]),
                            1e-12,
                        )
                    )
                    * (
                        float(
                            reference_option["execution_seconds"]
                            if reference_option is not None
                            else option["execution_seconds"]
                        )
                    )
                ),
                read_requests_per_query=float(option["read_requests"]),
                write_requests_per_query=float(option["write_requests"]),
            )
        )
    closed_spec = APClosedLoopSpec(
        demands=tuple(closed_demands),
        active_buffer_accesses_per_second=buffered_pressure,
    )
    mixed_projection = _mixed_resource_projection(
        context=context,
        stage=stage,
        candidate=candidate,
        native_tp_buffer_accesses_per_tx=float(native_accesses),
    )
    effective_tp_read_requests = float(candidate["tp_read_requests_per_tx"])
    effective_tp_buffer_accesses = float(native_accesses)
    effective_p_disk = float(candidate["p_disk"])
    effective_tp_cpu_ms = (
        float(context.tp_demands[tp_key].cpu_seconds_per_unit) * 1000.0
    )
    if mixed_projection is not None:
        # Resource deltas are projected from independently measured AP
        # features.  The measured shared CPU increment is added to the TP
        # service demand, while AP CPU remains in the finite-slot AP closure;
        # this avoids counting AP CPU twice.  ``blks_read`` is a database
        # buffer miss counter, not a device-request counter, so it must not be
        # injected into the FIO queue coordinate (doing so would silently
        # extrapolate the measured device surface by orders of magnitude).
        effective_tp_cpu_ms += float(
            mixed_projection["projected_shared_cpu_delta_ms_per_tx"]
        )
        # The measured buffer-access delta is consumed only by a matching
        # database-buffered surface; its device-layer read demand remains the
        # native candidate's independently modelled request rate.
        effective_tp_buffer_accesses = max(
            1e-12,
            effective_tp_buffer_accesses
            + float(mixed_projection["projected_buffer_delta_per_tx"]),
        )
    prediction = predict_stage_with_cpu_io_surface(
        benchmark=benchmark,
        stage=stage.name,
        terminals=stage.tp_terminals,
        base_predicted_tps=float(candidate["predicted_tps"]),
        base_latency_ms=float(candidate["transaction_latency_ms"]),
        base_disk_latency_ms=float(candidate["disk_path_latency_ms"]),
        p_disk=effective_p_disk,
        accesses_per_tx=effective_tp_buffer_accesses,
        tp_read_requests_per_tx=effective_tp_read_requests,
        tp_write_requests_per_tx=float(candidate["tp_write_requests_per_tx"]),
        ap_read_iops=float(candidate["ap_read_iops"]),
        ap_write_iops=float(candidate["ap_write_iops"]),
        service=context.service,
        surface=fio_surface,
        buffered_surface=buffered_surface,
        ap_buffer_accesses_per_second=buffered_pressure,
        tp_cpu_ms_per_tx=effective_tp_cpu_ms,
        ap_cpu_seconds_per_second=0.0,
        cpu_capacity_seconds_per_second=context.effective_capacity,
        ap_closed_loop=closed_spec,
        native_tp_buffer_accesses_per_tx=float(native_accesses),
        baseline_tp_cpu_ms_per_tx=(
            float(context.tp_demands[tp_key].cpu_seconds_per_unit) * 1000.0
        ),
        capacity_utilization_limit=context.capacity_limit,
    )
    return {
        "candidate": dict(candidate),
        "predicted_tps": float(prediction.predicted_tps),
        "prediction": dataclasses.asdict(prediction),
        "tp_feature_match": dict(tp_feature_match),
        "buffered_path_match": (
            dict(buffered_match) if buffered_match is not None else None
        ),
        "buffered_path_applied": buffered_surface is not None,
        "fio": {
            "selected_ap_read_fraction": float(fio_fraction),
            "candidate_ap_read_fraction": float(actual_fraction),
            "surface_path": str(fio_path.resolve()),
        },
        "ap_candidate_resource_mode": (
            "measured-query-cpu-anchor-plus-candidate-physical-io-and-"
            "residence-time-v1"
        ),
        "ap_cpu_work_projection": context.ap_cpu_work_mode,
        "ap_cpu_work_feature_anchors": cpu_feature_anchors,
        "mixed_resource_projection": mixed_projection,
        "effective_tp_resource_features": {
            "tp_cpu_ms_per_tx": effective_tp_cpu_ms,
            "tp_read_requests_per_tx": effective_tp_read_requests,
            "tp_buffer_accesses_per_tx": effective_tp_buffer_accesses,
            "p_disk": effective_p_disk,
        },
        "ap_cpu_work_source": cpu_source,
        "ap_buffer_pressure_mode": (
            context.ap_buffer_pressure_mode
        ),
        "ap_buffer_pressure_terms": list(buffered_pressure_terms),
        "selection_uses_benchmark_name": False,
    }


def _load_context(args, seed_profile: Mapping[str, object]) -> SearchContext:
    machine = str(seed_profile["machine_fingerprint"])
    cpu_document, ap_demands, tp_demands = _load_cpu(args.cpu_surface)
    _, ap_buffer_demands = _load_ap_buffer_demand(
        args.ap_buffer_demand_surface,
        machine,
    )
    _, ap_options = _load_full_ap_options(args.ap_model_bundle, machine)
    ap_cpu_anchor_rows = _load_ap_cpu_anchor_surface(
        args.ap_cpu_anchor_surface,
        machine,
    )
    tp_feature_document, tp_feature_rows = _load_tp_feature_catalog(
        args.tp_workload_feature_catalog,
        machine,
    )
    _, fio_reports = _load_fio_reports(args.fio_surface_set)
    service_document = _load_json(args.service_times)
    if (
        service_document.get("schema") != "huawei7.service-times/v2"
        or service_document.get("valid") is not True
    ):
        raise ValueError("service-time artifact is invalid")
    service = ServiceTimes(**{
        key: float(service_document["service_times_ms"][key])
        for key in ("tp_read_ms", "tp_write_ms", "ap_read_ms", "ap_write_ms")
    })
    buffered_surfaces = []
    for buffered_path in getattr(args, "buffered_path_surface", ()) or ():
        buffered_path = Path(buffered_path)
        buffered_document = _load_json(buffered_path)
        buffered_surface = surface_from_document(
            buffered_document,
            machine_fingerprint=machine,
        )
        if buffered_document.get("accepted_for_recommendation") is not True:
            raise ValueError("buffered path surface is not accepted")
        buffered_surfaces.append(buffered_surface)
    effective_capacity = effective_cpu_capacity_seconds(
        cpu_document["capacity_surface"],
        int(cpu_document["logical_cpus"]),
    )
    return SearchContext(
        cpu_document=cpu_document,
        ap_demands=ap_demands,
        tp_demands=tp_demands,
        ap_buffer_demands=ap_buffer_demands,
        ap_options=ap_options,
        tp_feature_document=tp_feature_document,
        tp_feature_rows=tp_feature_rows,
        fio_reports=fio_reports,
        service=service,
        buffered_surface=tuple(buffered_surfaces) if buffered_surfaces else None,
        effective_capacity=effective_capacity,
        capacity_limit=float(cpu_document["capacity_utilization_limit"]),
        machine_fingerprint=machine,
        ap_reference_work_mem=_machine_reference_wm(ap_demands),
        ap_buffer_pressure_mode=args.ap_buffer_pressure_mode,
        ap_cpu_work_mode=args.ap_cpu_work_mode,
        allow_plan_family_extrapolation=(
            args.allow_ap_plan_family_extrapolation
        ),
        ap_cpu_anchor_rows=ap_cpu_anchor_rows,
    )


def _build_mixed_resource_anchors(
    *,
    args,
    context: SearchContext,
    stages: Sequence[Stage],
) -> Sequence[MixedResourceAnchor]:
    """Load resource-only anchors and bind them to native feature rows."""

    anchors = []
    stage_by_name = {stage.name: stage for stage in stages}
    for raw_spec in getattr(args, "mixed_resource_surface", ()):
        raw_key, raw_path = str(raw_spec).split("=", 1)
        benchmark_provenance = None
        stage_name = raw_key
        if ":" in raw_key:
            benchmark_provenance, stage_name = raw_key.split(":", 1)
            if benchmark_provenance not in ("sysbench", "benchbase-tpcc"):
                raise ValueError(
                    "unsupported mixed-resource benchmark %s"
                    % benchmark_provenance
                )
        if stage_name not in stage_by_name:
            raise ValueError("mixed-resource surface lacks PPT stage %s" % stage_name)
        path = Path(raw_path)
        document = _load_json(path)
        contract = document.get("calibration_contract")
        if (
            document.get("schema") != "huawei7.mixed-resource-surface/v1"
            or document.get("valid") is not True
            or not isinstance(contract, dict)
            or contract.get("final_stage_tps_used") is not False
            or contract.get("mixed_tp_ap_tps_used") is not False
            or contract.get("mixed_tp_ap_resource_measurement") is not True
            or contract.get(
                "target_stage_tps_used_for_calibration"
            ) is not False
            or contract.get(
                "ap_queries_repeated_for_full_measurement_window"
            ) is not True
        ):
            raise ValueError(
                "mixed resource surface is invalid or leakage-prone: %s"
                % path
            )
        repeats = document.get("repeats")
        if not isinstance(repeats, list) or len(repeats) < 3:
            raise ValueError(
                "mixed resource surface requires >=3 repeats: %s" % path
            )
        if any(
            not isinstance(row, dict)
            or row.get("calibration_contract", {}).get(
                "resource_only_output"
            ) is not True
            for row in repeats
        ):
            raise ValueError(
                "mixed resource repeats are not resource-only: %s" % path
            )
        first = repeats[0]
        if not isinstance(first, dict):
            raise ValueError("mixed resource repeat is invalid: %s" % path)
        query_specs = first.get("query_specs")
        if not isinstance(query_specs, list) or not query_specs:
            raise ValueError("mixed resource surface lacks query_specs: %s" % path)
        query_work_mem = {
            str(item["query"]): int(item["work_mem_mb"])
            for item in query_specs
            if isinstance(item, dict)
        }
        stage = stage_by_name[stage_name]
        if tuple(sorted(query_work_mem)) != tuple(
            sorted(str(query) for query in stage.ap_queries)
        ):
            raise ValueError(
                "mixed resource query set differs from PPT %s: %s"
                % (stage_name, path)
            )
        shared_buffers_mb = int(first.get("shared_buffers_mb", 0))
        terminals = int(first.get("terminals", 0))
        if terminals != int(stage.tp_terminals) or shared_buffers_mb <= 0:
            raise ValueError(
                "mixed resource topology differs from PPT %s: %s"
                % (stage_name, path)
            )
        for row in repeats:
            if (
                not isinstance(row, dict)
                or int(row.get("shared_buffers_mb", -1))
                != shared_buffers_mb
                or int(row.get("terminals", -1)) != terminals
                or [
                    (str(item.get("query")), int(item.get("work_mem_mb")))
                    for item in row.get("query_specs", [])
                    if isinstance(item, dict)
                ] != [
                    (str(item.get("query")), int(item.get("work_mem_mb")))
                    for item in query_specs
                    if isinstance(item, dict)
                ]
            ):
                raise ValueError(
                    "mixed resource repeats do not share one configuration: %s"
                    % path
                )

        # A resource anchor must be tied to a native, independently measured
        # candidate at the same stage/configuration.  The benchmark string is
        # used only to locate that source artifact; later anchor selection
        # still uses intrinsic TP/AP features.
        source_benchmarks = (
            (benchmark_provenance,)
            if benchmark_provenance is not None
            else ("sysbench", "benchbase-tpcc")
        )
        matched = []
        for benchmark in source_benchmarks:
            model_path = (
                args.candidate_root / benchmark / stage.name / "model-result.json"
            )
            if not model_path.is_file():
                continue
            model_document = _load_json(model_path)
            for candidate in model_document.get("candidates", []):
                if not isinstance(candidate, dict):
                    continue
                try:
                    candidate_work_mem = _candidate_work_mem(candidate)
                except CandidateRejected:
                    continue
                if (
                    int(candidate.get("shared_buffers_mb", -1))
                    == shared_buffers_mb
                    and candidate_work_mem == query_work_mem
                ):
                    matched.append((benchmark, model_path, model_document, candidate))
        if len(matched) != 1:
            raise ValueError(
                "mixed resource surface does not bind to exactly one native "
                "candidate (%d matches): %s" % (len(matched), path)
            )
        native_benchmark, model_path, model_document, candidate = matched[0]
        empirical = _load_empirical(model_document)
        native_accesses = _metric_at_shared_buffers(
            empirical, shared_buffers_mb, "buffer_accesses_per_tx",
        )
        tp_feature_match = select_tp_workload_by_features(
            candidate={
                "tp_terminals": terminals,
                "tp_cpu_ms_per_tx": 0.0,
                "tp_read_requests_per_tx": float(
                    candidate["tp_read_requests_per_tx"]
                ),
                "tp_write_requests_per_tx": float(
                    candidate["tp_write_requests_per_tx"]
                ),
                "tp_buffer_accesses_per_tx": float(native_accesses),
                "p_disk": float(candidate["p_disk"]),
            },
            candidates=context.tp_feature_rows,
            maximum_relative_feature_distance=float(
                context.tp_feature_document[
                    "maximum_relative_feature_distance"
                ]
            ),
        )
        if not tp_feature_match.get("matched"):
            raise ValueError(
                "mixed resource native candidate is outside TP feature domain: %s"
                % path
            )
        tp_key = str(tp_feature_match["selected_demand_key"])
        native_tp_cpu_ms = (
            float(context.tp_demands[tp_key].cpu_seconds_per_unit) * 1000.0
        )
        summary_rows = []
        for row in repeats:
            normalized = dict(row)
            normalized_contract = dict(
                normalized.get("calibration_contract", {})
            )
            # The current collector emits all of these fields.  Keeping this
            # explicit makes the manifest self-auditing if an older artifact
            # is accidentally supplied.
            normalized["calibration_contract"] = normalized_contract
            summary_rows.append(normalized)
        summary = summarize_mixed_resource(
            summary_rows,
            native_read_requests_per_tx=float(
                candidate["tp_read_requests_per_tx"]
            ),
        )
        # ``mixed_process_cpu_seconds`` is the measured database process
        # demand for the TP transaction window.  The independent AP CPU
        # estimate is removed before storing the interaction term; otherwise
        # the closed AP CPU loop below would count AP CPU a second time.
        ap_cpu_ms_values = []
        for row in summary_rows:
            transactions = float(row.get("tp_transactions", 0.0))
            if transactions <= 0:
                raise ValueError(
                    "mixed resource repeat lacks TP transaction count: %s"
                    % path
                )
            if "estimated_ap_cpu_seconds" not in row:
                raise ValueError(
                    "mixed resource repeat lacks independent AP CPU estimate: %s"
                    % path
                )
            ap_cpu_ms_values.append(
                float(row["estimated_ap_cpu_seconds"])
                / transactions
                * 1000.0
            )
        shared_cpu_delta_per_tx = max(
            0.0,
            float(summary.mixed_cpu_ms_per_tx)
            - statistics.median(ap_cpu_ms_values)
            - native_tp_cpu_ms,
        )
        # ``blks_read`` is a cache-state-sensitive miss counter rather than a
        # direct device-request measurement.  If only that counter is noisy,
        # keep the CPU/buffer anchor (the quantities consumed by this search)
        # but do not inject the read delta into the FIO queue.  Any CPU or
        # buffer instability remains a hard failure.
        unstable_reasons = [
            item.strip()
            for item in summary.rejection_reason.split(";")
            if item.strip()
        ]
        non_read_instability = [
            item for item in unstable_reasons
            if not item.startswith("physical-read CV ")
        ]
        if non_read_instability:
            raise ValueError(
                "mixed resource surface is unstable: %s: %s"
                % (path, "; ".join(non_read_instability))
            )
        ap_features = _candidate_ap_intrinsic_features(
            context=context,
            stage=stage,
            candidate=candidate,
        )
        anchors.append(MixedResourceAnchor(
            source_path=str(path.resolve()),
            source_sha256=sha256(path),
            benchmark_provenance=native_benchmark,
            stage=stage.name,
            ap_queries=tuple(str(query) for query in stage.ap_queries),
            tp_terminals=terminals,
            shared_buffers_mb=shared_buffers_mb,
            ap_features=ap_features,
            native_tp_cpu_ms_per_tx=native_tp_cpu_ms,
            shared_cpu_delta_per_tx=shared_cpu_delta_per_tx,
            native_tp_read_requests_per_tx=float(
                candidate["tp_read_requests_per_tx"]
            ),
            native_tp_buffer_accesses_per_tx=float(native_accesses),
            mixed_tp_read_requests_per_tx=float(
                summary.mixed_read_requests_per_tx
            ),
            mixed_tp_buffer_accesses_per_tx=float(
                summary.mixed_buffer_accesses_per_tx
            ),
            read_delta_per_tx=float(
                summary.mixed_read_requests_per_tx
                - float(candidate["tp_read_requests_per_tx"])
            ),
            buffer_delta_per_tx=float(
                summary.mixed_buffer_accesses_per_tx - native_accesses
            ),
            resource_summary=dataclasses.asdict(summary),
        ))
    return tuple(anchors)


def _write_candidate_wrapper(
    *,
    source_path: Path,
    candidate: Mapping[str, object],
    destination: Path,
    search_metadata: Mapping[str, object],
) -> None:
    document = dict(_load_json(source_path))
    document["best"] = dict(candidate)
    document["candidate_search_selection"] = dict(search_metadata)
    document["candidate_search_source"] = {
        "path": str(source_path.resolve()),
        "sha256": sha256(source_path),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _row_from_result(
    *,
    benchmark: str,
    stage: Stage,
    result: Mapping[str, object],
    model_result_path: Path,
    model_document: Mapping[str, object],
    rank: int,
    evaluated_count: int,
    valid_count: int,
    fixed_result: Optional[Mapping[str, object]],
) -> Mapping[str, object]:
    candidate = result["candidate"]
    work_mem = _candidate_work_mem(candidate)
    row = {
        "benchmark": benchmark,
        "stage": stage.name,
        "tp_terminals": stage.tp_terminals,
        "tp_baseline_terminals": stage.tp_baseline_terminals,
        "tp_surge_terminals": stage.tp_surge_terminals,
        "tp_surge_start_phase": (
            "measurement" if stage.tp_surge_terminals else None
        ),
        "shared_buffers_mb": int(candidate["shared_buffers_mb"]),
        "work_mem_by_query": work_mem,
        "predicted_tps": float(result["predicted_tps"]),
        "uncorrected_predicted_tps": float(candidate["predicted_tps"]),
        "query_sha256": {
            str(query): str(digest)
            for query, digest in model_document["ap_query_sha256"].items()
        },
        "dataset_fingerprint": str(model_document["dataset_fingerprint"]),
        "model_result": str(model_result_path.resolve()),
        "model_result_sha256": sha256(model_result_path),
        "joint_candidate_rank": int(rank),
        "joint_candidate_evaluated_count": int(evaluated_count),
        "joint_candidate_valid_count": int(valid_count),
        "cpu_io_contention": {
            "method": "joint-cpu-io-finite-ap-closed-loop-v3-candidate-search",
            "prediction": result["prediction"],
            "tp_feature_match": result["tp_feature_match"],
            "buffered_path_feature_match": result["buffered_path_match"],
            "buffered_path_applied": result["buffered_path_applied"],
            "fio": result["fio"],
            "ap_candidate_resource_mode": result[
                "ap_candidate_resource_mode"
            ],
            "ap_cpu_work_projection": result["ap_cpu_work_projection"],
            "ap_cpu_work_source": result["ap_cpu_work_source"],
            "ap_cpu_work_feature_anchors": result[
                "ap_cpu_work_feature_anchors"
            ],
            "mixed_resource_projection": result.get(
                "mixed_resource_projection"
            ),
            "effective_tp_resource_features": result.get(
                "effective_tp_resource_features"
            ),
            "ap_buffer_pressure_mode": result["ap_buffer_pressure_mode"],
            "ap_buffer_pressure_terms": result[
                "ap_buffer_pressure_terms"
            ],
            "selection_uses_benchmark_name": False,
            "prediction_uses_mixed_stage_tps": False,
            "candidate_search_rank": int(rank),
            "fixed_baseline_predicted_tps": (
                float(fixed_result["predicted_tps"])
                if fixed_result is not None else None
            ),
        },
    }
    return row


def _configuration(row: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "shared_buffers_mb": int(row["shared_buffers_mb"]),
        "work_mem_by_query": {
            str(key): int(value)
            for key, value in row["work_mem_by_query"].items()
        },
    }


def _has_one_global_configuration(rows: Sequence[Mapping[str, object]]) -> bool:
    """Ignore inactive AP queries when checking whether a profile is fixed."""

    shared_buffers = {
        int(row["shared_buffers_mb"]) for row in rows
    }
    work_mem_values = {}
    for row in rows:
        for query, value in row["work_mem_by_query"].items():
            work_mem_values.setdefault(str(query), set()).add(int(value))
    return (
        len(shared_buffers) == 1
        and all(len(values) == 1 for values in work_mem_values.values())
    )


def _global_fixed_baseline(
    *,
    context: SearchContext,
    stages: Sequence[Stage],
    benchmark: str,
    candidate_root: Path,
) -> Mapping[str, object]:
    """Find the best *one* configuration for all five stages.

    The fixed baseline must be optimized by the same joint model as the
    adaptive profile.  Comparing against an older frozen profile would
    incorrectly credit the adaptive search for merely changing a global
    configuration.  S4 contains all five AP queries, so its native candidate
    set is a complete, finite set of global configurations for this search
    space.  For each such configuration we look up the matching subset
    candidate in every other stage and require every stage to remain inside
    its measured resource domains.
    """

    model_documents = {
        stage.name: _load_json(
            candidate_root / benchmark / stage.name / "model-result.json"
        )
        for stage in stages
    }
    full_stage = next(
        stage for stage in stages
        if set(stage.ap_queries) == {2, 9, 13, 18, 21}
    )
    full_candidates = model_documents[full_stage.name].get("candidates")
    if not isinstance(full_candidates, list) or not full_candidates:
        raise ValueError("complete-stage candidate set is empty")

    valid = []
    rejection_counts = collections.Counter()
    for full_candidate in full_candidates:
        try:
            global_work_mem = _candidate_work_mem(full_candidate)
            stage_results = []
            for stage in stages:
                active_work_mem = {
                    str(query): global_work_mem[str(query)]
                    for query in stage.ap_queries
                }
                matching = [
                    candidate for candidate in model_documents[stage.name][
                        "candidates"
                    ]
                    if _same_configuration(
                        candidate,
                        int(full_candidate["shared_buffers_mb"]),
                        active_work_mem,
                    )
                ]
                if len(matching) != 1:
                    raise CandidateRejected(
                        "global_configuration_missing_%s" % stage.name
                    )
                stage_results.append(_score_candidate(
                    context=context,
                    stage=stage,
                    benchmark=benchmark,
                    model_document=model_documents[stage.name],
                    candidate=matching[0],
                ))
            mean = sum(
                float(result["predicted_tps"]) for result in stage_results
            ) / len(stage_results)
            valid.append({
                "mean_predicted_tps": mean,
                "candidate": dict(full_candidate),
                "stage_results": stage_results,
            })
        except Exception as exc:
            rejection_counts[_reason(exc)] += 1
    if not valid:
        raise RuntimeError(
            "no valid global fixed configuration for %s: %s"
            % (benchmark, dict(rejection_counts))
        )
    valid.sort(key=lambda item: (
        -float(item["mean_predicted_tps"]),
        -float(item["candidate"]["predicted_tps"]),
        int(item["candidate"]["shared_buffers_mb"]),
        tuple(_candidate_work_mem(item["candidate"]).items()),
    ))
    selected = valid[0]
    return {
        "method": "best-global-fixed-configuration-under-joint-model-v1",
        "benchmark": benchmark,
        "native_candidate_count": len(full_candidates),
        "valid_global_configuration_count": len(valid),
        "rejection_counts": dict(rejection_counts),
        "selected": selected,
        "top_global_configurations": [
            {
                "rank": index,
                "mean_predicted_tps": float(item["mean_predicted_tps"]),
                "configuration": {
                    "shared_buffers_mb": int(
                        item["candidate"]["shared_buffers_mb"]
                    ),
                    "work_mem_by_query": _candidate_work_mem(
                        item["candidate"]
                    ),
                },
                "stage_predicted_tps": {
                    stage.name: float(result["predicted_tps"])
                    for stage, result in zip(
                        stages, item["stage_results"]
                    )
                },
            }
            for index, item in enumerate(valid[:10], start=1)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixed-profile", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--stage-spec", type=Path, required=True)
    parser.add_argument("--cpu-surface", type=Path, required=True)
    parser.add_argument("--ap-buffer-demand-surface", type=Path, required=True)
    parser.add_argument("--ap-model-bundle", type=Path, required=True)
    parser.add_argument("--tp-workload-feature-catalog", type=Path, required=True)
    parser.add_argument("--fio-surface-set", type=Path, required=True)
    parser.add_argument("--service-times", type=Path, required=True)
    parser.add_argument(
        "--buffered-path-surface",
        action="append",
        default=[],
        type=Path,
        help="accepted resource-only DB-buffered surface; repeat for domains",
    )
    parser.add_argument("--ap-cpu-anchor-surface", type=Path)
    parser.add_argument(
        "--mixed-resource-surface",
        action="append",
        default=[],
        help=(
            "optional benchmark:stage=mixed-resource-surface.json resource "
            "anchor; only CPU/IO counters are consumed"
        ),
    )
    parser.add_argument(
        "--ap-buffer-pressure-mode",
        choices=(
            "measured_stage_domain_pressure_no_work_mem_extrapolation",
            "candidate_logical_page_rate_projection",
        ),
        default="measured_stage_domain_pressure_no_work_mem_extrapolation",
        help=(
            "AP buffered-path pressure treatment. The candidate projection "
            "uses only logical pages and isolated execution time from the "
            "independent AP bundle; it fits no TPS parameter."
        ),
    )
    parser.add_argument(
        "--ap-cpu-work-mode",
        choices=(
            "measured-anchor",
            "operator-feature-anchor",
            "resource-decomposition",
        ),
        default="operator-feature-anchor",
        help=(
            "AP CPU work treatment. operator-feature-anchor scales an "
            "isolated CPU anchor by deterministic cpu_operations from the "
            "independent AP operator model; it fits no TPS correction. "
            "measured-anchor is the conservative baseline."
        ),
    )
    parser.add_argument(
        "--allow-ap-plan-family-extrapolation",
        action="store_true",
        help=(
            "diagnostic only: allow cpu_operations scaling across a plan "
            "family without a direct CPU anchor"
        ),
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument(
        "--profile-version",
        choices=("v16", "v17", "v18", "v19"),
        default=None,
        help=(
            "output schema version; v19 records resource-only mixed anchors, "
            "v17 is used for the candidate logical-page pressure projection "
            "and v16 for the legacy mode"
        ),
    )
    args = parser.parse_args()
    if args.top_k <= 0:
        raise ValueError("--top-k must be positive")

    fixed_profile = _load_json(args.fixed_profile)
    stages = read_stage_spec(args.stage_spec)
    context = _load_context(args, fixed_profile)
    mixed_resource_anchors = _build_mixed_resource_anchors(
        args=args,
        context=context,
        stages=stages,
    )
    context = dataclasses.replace(
        context,
        mixed_resource_anchors=mixed_resource_anchors,
    )
    all_rows = []
    search_documents = {}
    global_fixed_documents = {}
    global_fixed_results = {}
    wrappers = []
    for benchmark in ("sysbench", "benchbase-tpcc"):
        global_fixed = _global_fixed_baseline(
            context=context,
            stages=stages,
            benchmark=benchmark,
            candidate_root=args.candidate_root,
        )
        global_fixed_documents[benchmark] = global_fixed
        global_fixed_results[benchmark] = {
            stage.name: result
            for stage, result in zip(
                stages, global_fixed["selected"]["stage_results"]
            )
        }
        for stage in stages:
            model_path = (
                args.candidate_root / benchmark / stage.name / "model-result.json"
            )
            model_document = _load_json(model_path)
            candidates = model_document.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                raise ValueError("no candidates in %s" % model_path)
            valid_results = []
            rejection_counts = collections.Counter()
            for candidate in candidates:
                try:
                    valid_results.append(_score_candidate(
                        context=context,
                        stage=stage,
                        benchmark=benchmark,
                        model_document=model_document,
                        candidate=candidate,
                    ))
                except Exception as exc:
                    rejection_counts[_reason(exc)] += 1
            valid_results.sort(key=lambda item: (
                -float(item["predicted_tps"]),
                -float(item["candidate"]["predicted_tps"]),
                int(item["candidate"]["shared_buffers_mb"]),
                tuple(_candidate_work_mem(item["candidate"]).items()),
            ))
            if not valid_results:
                raise RuntimeError(
                    "no valid joint candidate for %s/%s: %s"
                    % (benchmark, stage.name, dict(rejection_counts))
                )
            fixed_result = global_fixed_results[benchmark][stage.name]
            global_fixed_candidate = global_fixed["selected"]["candidate"]
            selected = valid_results[0]
            wrapper_path = (
                args.out.parent / "candidate-model-results"
                / benchmark / ("%s.json" % stage.name)
            )
            _write_candidate_wrapper(
                source_path=model_path,
                candidate=selected["candidate"],
                destination=wrapper_path,
                search_metadata={
                    "method": "joint-cpu-io-finite-ap-closed-loop-v3-candidate-search",
                    "benchmark": benchmark,
                    "stage": stage.name,
                    "candidate_rank": 1,
                    "valid_candidate_count": len(valid_results),
                    "native_candidate_count": len(candidates),
                    "selection_uses_benchmark_name": False,
                },
            )
            wrappers.append(wrapper_path)
            row = _row_from_result(
                benchmark=benchmark,
                stage=stage,
                result=selected,
                model_result_path=wrapper_path,
                model_document=model_document,
                rank=1,
                evaluated_count=len(candidates),
                valid_count=len(valid_results),
                fixed_result=fixed_result,
            )
            all_rows.append(row)
            key = "%s:%s" % (benchmark, stage.name)
            search_documents[key] = {
                "benchmark": benchmark,
                "stage": stage.name,
                "native_candidate_count": len(candidates),
                "valid_candidate_count": len(valid_results),
                "rejection_counts": dict(rejection_counts),
                "selected": {
                    "predicted_tps": float(selected["predicted_tps"]),
                    "native_predicted_tps": float(
                        selected["candidate"]["predicted_tps"]
                    ),
                    "configuration": {
                        "shared_buffers_mb": int(
                            selected["candidate"]["shared_buffers_mb"]
                        ),
                        "work_mem_by_query": _candidate_work_mem(
                            selected["candidate"]
                        ),
                    },
                },
                "fixed_baseline": {
                    "predicted_tps": float(fixed_result["predicted_tps"]),
                    "configuration": {
                        "shared_buffers_mb": int(
                            global_fixed_candidate["shared_buffers_mb"]
                        ),
                        "work_mem_by_query": _candidate_work_mem(
                            global_fixed_candidate
                        ),
                    },
                    "delta_tps": (
                        float(selected["predicted_tps"])
                        - float(fixed_result["predicted_tps"])
                    ),
                    "delta_fraction": (
                        float(selected["predicted_tps"])
                        / float(fixed_result["predicted_tps"])
                        - 1.0
                    ),
                },
                "top_candidates": [
                    {
                        "rank": index,
                        "predicted_tps": float(item["predicted_tps"]),
                        "native_predicted_tps": float(
                            item["candidate"]["predicted_tps"]
                        ),
                        "configuration": {
                            "shared_buffers_mb": int(
                                item["candidate"]["shared_buffers_mb"]
                            ),
                            "work_mem_by_query": _candidate_work_mem(
                                item["candidate"]
                            ),
                        },
                    }
                    for index, item in enumerate(
                        valid_results[:args.top_k], start=1
                    )
                ],
            }

    by_benchmark = {}
    for benchmark in ("sysbench", "benchbase-tpcc"):
        rows = [row for row in all_rows if row["benchmark"] == benchmark]
        fixed_rows = [
            search_documents["%s:%s" % (benchmark, stage.name)][
                "fixed_baseline"
            ]
            for stage in stages
        ]
        adaptive_mean = sum(float(row["predicted_tps"]) for row in rows) / len(rows)
        fixed_mean = sum(
            float(row["predicted_tps"]) for row in fixed_rows
        ) / len(fixed_rows)
        deltas = [
            float(row["predicted_tps"]) - float(fixed["predicted_tps"])
            for row, fixed in zip(rows, fixed_rows)
        ]
        by_benchmark[benchmark] = {
            "adaptive_mean_predicted_tps": adaptive_mean,
            "fixed_mean_predicted_tps": fixed_mean,
            "mean_delta_tps": adaptive_mean - fixed_mean,
            "mean_delta_fraction": adaptive_mean / fixed_mean - 1.0,
            "all_stage_non_degradation": all(delta >= -1e-9 for delta in deltas),
            "strict_mean_gain": adaptive_mean > fixed_mean + 1e-9,
            "stage_deltas_tps": {
                stage.name: delta for stage, delta in zip(stages, deltas)
            },
            "one_global_configuration_in_adaptive_profile": (
                _has_one_global_configuration(rows)
            ),
        }

    profile = dict(fixed_profile)
    profile_version = args.profile_version
    if profile_version is None:
        if mixed_resource_anchors:
            profile_version = "v19"
        else:
            profile_version = (
                "v17"
                if args.ap_buffer_pressure_mode
                == "candidate_logical_page_rate_projection"
                else "v16"
            )
    if (
        profile_version == "v16"
        and args.ap_buffer_pressure_mode
        == "candidate_logical_page_rate_projection"
    ):
        raise ValueError(
            "v16 cannot claim the candidate logical-page pressure projection"
        )
    profile["schema"] = (
        "huawei7.five-stage-recommendations/" + profile_version
    )
    profile["stages"] = all_rows
    profile["ap_model_bundle"] = {
        "path": str(args.ap_model_bundle.resolve()),
        "sha256": sha256(args.ap_model_bundle),
        "cpu_operations_feature": True,
    }
    if args.ap_cpu_anchor_surface is not None:
        profile["ap_cpu_anchor_surface"] = {
            "path": str(args.ap_cpu_anchor_surface.resolve()),
            "sha256": sha256(args.ap_cpu_anchor_surface),
            "interpolation_only_inside_measured_interval": True,
        }
    profile["base_recommendations"] = {
        "path": str(args.fixed_profile.resolve()),
        "sha256": sha256(args.fixed_profile),
    }
    profile["mixed_resource_surfaces"] = {
        anchor.source_path: {
            "path": anchor.source_path,
            "sha256": anchor.source_sha256,
            "stage": anchor.stage,
            "benchmark_provenance": anchor.benchmark_provenance,
            "selection_uses_benchmark_name": False,
        }
        for anchor in mixed_resource_anchors
    }
    profile["candidate_search"] = {
        "method": (
            "joint-cpu-io-finite-ap-closed-loop-v7-resource-anchor-search"
            if mixed_resource_anchors
            else "joint-cpu-io-finite-ap-closed-loop-v6-candidate-search"
        ),
        "selection_uses_benchmark_name": False,
        "uses_target_stage_tps": False,
        "uses_mixed_stage_tps": False,
        "uses_exact_machine_contention_factor": False,
        "ap_cpu_work_projection": args.ap_cpu_work_mode,
        "plan_family_extrapolation_allowed": bool(
            args.allow_ap_plan_family_extrapolation
        ),
        "ap_model_bundle": {
            "path": str(args.ap_model_bundle.resolve()),
            "sha256": sha256(args.ap_model_bundle),
            "cpu_operations_feature": True,
        },
        "ap_cpu_anchor_surface": (
            {
                "path": str(args.ap_cpu_anchor_surface.resolve()),
                "sha256": sha256(args.ap_cpu_anchor_surface),
            }
            if args.ap_cpu_anchor_surface is not None else None
        ),
        "ap_residence_time_source": (
            "independent_ap_model_bundle_execution_seconds"
        ),
        "ap_physical_request_source": (
            "independent_ap_model_bundle_read_write_requests"
        ),
        "ap_buffer_pressure_mode": (
            args.ap_buffer_pressure_mode
        ),
        "candidate_root": str(args.candidate_root.resolve()),
        "candidate_root_sha256_not_applicable": True,
        "fixed_profile": {
            "path": str(args.fixed_profile.resolve()),
            "sha256": sha256(args.fixed_profile),
        },
        "stages": search_documents,
        "fixed_configuration_comparison": by_benchmark,
        "best_global_fixed_baselines": global_fixed_documents,
        "candidate_model_wrappers": [
            {
                "path": str(path.resolve()),
                "sha256": sha256(path),
            }
            for path in wrappers
        ],
        "mixed_resource_anchors": [
            {
                "path": anchor.source_path,
                "sha256": anchor.source_sha256,
                "benchmark_provenance": anchor.benchmark_provenance,
                "stage": anchor.stage,
                "tp_terminals": anchor.tp_terminals,
                "shared_buffers_mb": anchor.shared_buffers_mb,
                "ap_queries": list(anchor.ap_queries),
                "ap_features": dict(anchor.ap_features),
                "resource_summary": dict(anchor.resource_summary),
                "selection_uses_benchmark_name": False,
                "prediction_uses_mixed_stage_tps": False,
            }
            for anchor in mixed_resource_anchors
        ],
    }
    profile["ap_rate_model"] = {
        "method": (
            "finite-slot-response-closed-loop-v7-resource-anchor-search"
            if mixed_resource_anchors
            else "finite-slot-response-closed-loop-v6-candidate-search"
        ),
        "uses_target_stage_tps": False,
        "uses_mixed_stage_tps": False,
        "uses_exact_machine_contention_factor": False,
    }
    profile["portable_profile"] = {
        "method": (
            "joint-cpu-io-finite-ap-closed-loop-v8-resource-anchor-search"
            if mixed_resource_anchors
            else "joint-cpu-io-finite-ap-closed-loop-v7-candidate-search"
            if args.ap_buffer_pressure_mode
            == "candidate_logical_page_rate_projection"
            else "joint-cpu-io-finite-ap-closed-loop-v6-candidate-search"
        ),
        "ap_rate_model": (
            "finite-slot-response-closed-loop-v7-resource-anchor-search"
            if mixed_resource_anchors
            else "finite-slot-response-closed-loop-v6-candidate-search"
        ),
        "target_stage_tps_used_for_calibration": False,
        "exact_config_contention_disabled": True,
        "selection_uses_benchmark_name": False,
        "accepted_for_recommendation": False,
        "validation_status": (
            "diagnostic_only_pending_real_joint_holdout_and_candidate_cpu_holdout"
        ),
        "mixed_resource_anchor_projection": bool(mixed_resource_anchors),
        "mixed_resource_anchor_selection_uses_benchmark_name": False,
        "mixed_resource_anchor_prediction_uses_mixed_stage_tps": False,
    }
    profile["selection_frozen_before_real_stage_measurements"] = True
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(profile, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "schema": profile["schema"],
        "output": str(args.out.resolve()),
        "fixed_configuration_comparison": by_benchmark,
        "best_global_fixed_baselines": {
            benchmark: {
                key: value
                for key, value in document.items()
                if key != "selected"
            }
            for benchmark, document in global_fixed_documents.items()
        },
        "adaptive_profile_is_global_fixed": {
            key: value["one_global_configuration_in_adaptive_profile"]
            for key, value in by_benchmark.items()
        },
        "accepted_for_recommendation": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
