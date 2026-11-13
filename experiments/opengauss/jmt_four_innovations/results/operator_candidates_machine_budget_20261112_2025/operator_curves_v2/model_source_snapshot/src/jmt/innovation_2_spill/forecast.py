"""Request-local AP forecasting; pure library, no validation-script or DB imports."""
import time
import math
import copy
from datetime import datetime, timezone
from .ap_plan_model import Context, estimate_plan, blind_root
from .ap_light import SCHEMA as LIGHT_SCHEMA, apply_light_estimates
from .ap_group_stats import filtered_group_estimates, simple_group_occupancy
from .ap_joint import JointSynopsis, SynopsisCollection, apply_joint_evidence
from .ap_expr_types import ExpressionTypes, SCHEMA as TYPE_SCHEMA
from .ap_unique_groups import estimates as unique_groups, apply as apply_unique_groups
from .ap_preflight import apply_estimates, canonical, sql_metadata, sqlglot, walk
from .group_synopsis import apply_group_frequencies
from jmt.common.memory_units import MAX_WORK_MEM_MIB

def now():
    return datetime.now(timezone.utc).isoformat()

def bind_estimates(document, cached):
    bound = []
    for path, node in walk(document[0]["Plan"]):
        keys = node.get("Group By Key", node.get("Group Key", []))
        if node.get("Grouping Sets") and len(node["Grouping Sets"]) == 1:
            sets = node["Grouping Sets"][0].get("Group Keys", [])
            keys = max(sets, key=len) if sets else []
        if node["Node Type"] in ("Aggregate", "HashAggregate") and keys:
            signature = tuple(sorted(canonical(key) for key in keys))
            if signature in cached:
                estimate = dict(cached[signature])
                estimate["node_path"] = path
                bound.append(estimate)
    return bound

class ForecastSession:
    """Request-local preparation, shared across plans/WMs; no global SQL cache."""

    def __init__(self, sql, catalog, joint_synopsis=None, group_synopsis=None,
                 cardinality_provider=None, key_group_sample=None):
        started = time.perf_counter()
        self.sql, self.catalog = sql, catalog
        self.joint_synopsis = joint_synopsis
        self.group_synopsis = group_synopsis
        self.cardinality_provider = cardinality_provider
        self.key_group_sample = key_group_sample
        if key_group_sample is not None and (joint_synopsis is not None or group_synopsis is not None):
            raise ValueError("do not overlap independent key-group sample with group/joint synopsis")
        if cardinality_provider is not None and (joint_synopsis is not None or group_synopsis is not None):
            raise ValueError("do not combine overlapping cardinality input providers")
        cache_database = getattr(joint_synopsis, "database", None)
        if (cache_database and catalog.get("database") and
                cache_database != catalog["database"]):
            raise ValueError("key synopsis and forecast catalog databases do not match")
        self.base_context = Context.from_catalog(catalog)
        self.ast = sqlglot.parse_one(sql, read="postgres") if sqlglot is not None else None
        self.expression_types = ExpressionTypes(self.ast, catalog) if self.ast is not None else None
        self.preparation_ms = (time.perf_counter() - started) * 1000

    def forecast(self, document, wm, sample_estimates=None, dimension_evidence=()):
        return self._evaluate(document, wm, sample_estimates, dimension_evidence)

    def having_input_bound_scenario(self, document, wm):
        """Hypothetical G=N at grouped HAVING sites, NOT a cardinality estimate.

        This exposes sensitivity to a bad catalog NDV without substituting an
        old execution label or pretending that a row sample identifies NDV.
        The bound is conditional on the modeled input rows. Join/input row
        estimates, skew and operator grants remain unqualified.
        """
        return self._evaluate(document, wm, having_input_bound_scenario=True)

    def _evaluate(self, document, wm, sample_estimates=None, dimension_evidence=(),
                  diagnostic=None, having_input_bound_scenario=False):
        """Internal shared kernel. Target observations are audit-only, never forecast()."""
        if (isinstance(wm, bool) or not isinstance(wm, (int, float))
                or not math.isfinite(wm) or not 0 < wm <= MAX_WORK_MEM_MIB):
            raise ValueError("work_mem must be finite, positive and within the engine GUC range")
        if diagnostic is not None and having_input_bound_scenario:
            raise ValueError("do not mix hypothetical scenarios with target labels")
        started = time.perf_counter()
        context = Context(relations=self.base_context.relations,
                          columns=self.base_context.columns)
        light_effects, group_estimates = [], {}
        unique_evidence, unique_effects = {}, []
        joint_evidence, joint_effects = None, []
        original_hash_groups = {path: node.get("Plan Rows", 1)
                                for path, node in walk(blind_root(document))
                                if node["Node Type"] in ("Aggregate", "HashAggregate", "SetOp")}
        original_hash_inputs = {
            path: (node["Plans"][0].get("Plan Rows", 0), node["Plans"][0].get("Plan Width", 0))
            for path, node in walk(blind_root(document))
            if node["Node Type"] == "Hash" and len(node.get("Plans", [])) == 1}
        if self.ast is not None:
            context.node_facts = sql_metadata(document, self.sql, self.ast)
            # Scoped inference is authoritative. The historical alias-only
            # lineage ignored unknown UNION arms and could leak a known arm's
            # type into the whole derived output.
            context.columns = {**self.expression_types.derived_columns(document), **context.columns}
            context.expression_types = self.expression_types.plan_types(document, context)
            for path, values in self.expression_types.internal_sort_facts(document, context).items():
                context.node_facts.setdefault(path, {})["internal_aggregate_inputs"] = values
            for path, evidence in self.expression_types.group_frequency_facts(document).items():
                context.node_facts.setdefault(path, {})["internal_sort_group_frequency"] = evidence
            for path, evidence in self.expression_types.root_append_projection_width(document).items():
                context.node_facts.setdefault(path, {})["input_tuple_width_evidence"] = evidence
            group_estimates = simple_group_occupancy(document, self.catalog)
            document, light_effects = apply_light_estimates(
                document, self.sql, dimension_evidence, self.ast, self.catalog, group_estimates)
            for path, evidence in filtered_group_estimates(document, self.catalog).items():
                context.node_facts.setdefault(path, {})["pre_having_group_estimate"] = evidence
            unique_evidence = unique_groups(document, self.catalog)
            document, unique_effects = apply_unique_groups(document, unique_evidence)
        elif dimension_evidence:
            raise ImportError("light preflight requires requirements-ap-preflight.txt")
        if self.joint_synopsis is not None:
            if sample_estimates:
                raise ValueError("do not mix cached joint statistics with paid per-query samples")
            joint_evidence = self.joint_synopsis.evidence(document)
            document, joint_effects = apply_joint_evidence(document, joint_evidence)
            for path, histogram in joint_evidence["histograms"].items():
                context.node_facts.setdefault(path, {})["group_size_histogram"] = histogram
            for path, item in joint_evidence["setop_inputs"].items():
                context.node_facts.setdefault(path, {})["setop_cardinality"] = item
        bound = bind_estimates(document, sample_estimates) if sample_estimates else []
        changed, effects = apply_estimates(document, bound) if bound else (document, [])
        for evidence in bound:
            context.node_facts.setdefault(evidence["node_path"], {})[
                "group_size_histogram"] = evidence.get("frequency_histogram")
        for path, groups in original_hash_groups.items():
            context.node_facts.setdefault(path, {})["hash_planner_groups_proxy"] = groups
        for path, (rows, width) in original_hash_inputs.items():
            context.node_facts.setdefault(path, {}).update({
                "hash_planner_input_rows_proxy": rows, "hash_planner_input_width_proxy": width})
        group_evidence, group_effects = {}, []
        if self.group_synopsis is not None:
            if bound or joint_evidence:
                raise ValueError("do not combine overlapping group evidence providers")
            group_evidence = self.group_synopsis.evidence(changed, self.catalog, context.node_facts)
            changed, group_effects = apply_group_frequencies(changed, group_evidence)
            for path, evidence in group_evidence.items():
                context.node_facts.setdefault(path, {}).update({
                    "group_size_histogram": evidence["histogram"],
                    "internal_sort_group_frequency": evidence,
                })
                if evidence["node_type"] == "WindowAgg":
                    context.node_facts[path]["partition_frequency_evidence"] = evidence
                elif evidence["has_having"]:
                    context.node_facts[path]["pre_having_group_estimate"] = {
                        **evidence, "estimated_pre_having_groups": evidence["groups"]}
        learned_evidence, learned_effects = None, []
        cardinality_base = changed
        if self.cardinality_provider is not None:
            from .learned_cardinality import apply as apply_learned_cardinality
            learned_evidence = self.cardinality_provider.evidence(changed)
            learned_database = learned_evidence.get("database")
            if learned_database != self.catalog.get("database"):
                raise ValueError("learned input database does not match catalog")
            changed, learned_effects = apply_learned_cardinality(changed, learned_evidence)
            if learned_evidence.get("group_histograms"):
                from .cardinality_guard import frequency_facts
                for path, values in frequency_facts(learned_evidence).items():
                    context.node_facts.setdefault(path, {}).update(values)
        scenario_paths = []
        key_group_evidence = {}
        if self.key_group_sample is not None:
            key_group_evidence = self.key_group_sample.evidence(changed, self.catalog)
            for path, evidence in key_group_evidence.items():
                if (any(b["node_path"] == path for b in bound)
                        or learned_evidence is not None and path in learned_evidence.get("groups", {})):
                    raise ValueError("overlapping pre-HAVING group sample providers")
                context.node_facts.setdefault(path, {}).update({
                    "pre_having_group_estimate": evidence,
                    "group_size_histogram": evidence["frequency_histogram"],
                })
        if having_input_bound_scenario:
            for path, node in walk(blind_root(changed)):
                if (node["Node Type"] in ("Aggregate", "HashAggregate")
                        and node.get("Filter")
                        and node.get("Group By Key", node.get("Group Key", []))):
                    if node.get("Grouping Sets"):
                        raise ValueError("HAVING input-bound scenario excludes grouping sets")
                    context.node_facts.setdefault(path, {})[
                        "hypothetical_having_groups_equal_input"] = True
                    scenario_paths.append(path)
        if diagnostic is not None:
            changed = copy.deepcopy(changed)
            by_path = dict(walk(blind_root(changed)))
            for path, rows in diagnostic.get("rows", {}).items():
                if path not in by_path or not math.isfinite(rows) or rows < 0:
                    raise ValueError("invalid POSTEXECUTION cardinality replacement")
                by_path[path]["Plan Rows"] = rows
            for path, values in diagnostic.get("facts", {}).items():
                if path not in by_path:
                    raise ValueError("invalid POSTEXECUTION node path")
                context.node_facts.setdefault(path, {}).update(values)
            passthrough, propagated_paths = [], set()
            def propagate(node, path):
                main = [(i, c) for i, c in enumerate(node.get("Plans", []))
                        if c.get("Parent Relationship") not in ("InitPlan", "SubPlan")]
                for i, child in enumerate(node.get("Plans", [])):
                    propagate(child, path + "." + str(i))
                if (path not in diagnostic.get("rows", {}) and len(main) == 1
                        and not node.get("Filter") and node["Node Type"] in (
                            "Hash", "Sort", "Materialize", "Result", "Subquery Scan")):
                    child_path = path + "." + str(main[0][0])
                    if child_path in diagnostic.get("rows", {}) or child_path in propagated_paths:
                        before, after = node.get("Plan Rows", 0), main[0][1].get("Plan Rows", 0)
                        node["Plan Rows"] = after
                        passthrough.append({"path": path, "before": before, "after": after,
                                            "source": "semantic_pass_through_of_POSTEXECUTION_rows"})
                        propagated_paths.add(path)
            propagate(blind_root(changed), "")
        result = estimate_plan(changed, wm, context)
        input_guard = None
        if (learned_evidence is not None and learned_evidence.get("sensitivity_profile")
                and diagnostic is None):
            from .cardinality_guard import assess
            input_guard = assess(cardinality_base, learned_evidence, wm, context, result)
        join_nodes = [
            {"path": path, "node_type": node["Node Type"]}
            for path, node in walk(blind_root(changed))
            if "Join" in node["Node Type"] or node["Node Type"] == "Nested Loop"
        ]
        result.update({
            "experimental_cardinality_input": learned_evidence,
            "spill_input_guard": input_guard,
            "experimental_cardinality_effects": learned_effects,
            "preexecution_key_group_sample_evidence": key_group_evidence,
            "release_schema": "jmt.ap-forecast/1.0.0rc4",
            "reliability": {
                "operator_coverage_complete": result["logical_temp_estimate_complete"],
                "join_cardinality_independently_validated": False if join_nodes else None,
                "join_nodes_requiring_input_validation": join_nodes[:128],
                "classification": (
                    "unvalidated_join_cardinality" if join_nodes
                    else "experimental_operator_estimate"
                ),
                "qualification": (
                    "An implementation-complete numeric result is NOT a confidence interval, "
                    "a safety guarantee or qualification for automatic WM recommendation."
                ),
            },
            "forecasted_at": now(), "forecast_pipeline_schema": LIGHT_SCHEMA,
            "uses_preflight_statistics": bool(bound or dimension_evidence or key_group_evidence or
                                              joint_evidence and joint_evidence["points"] or
                                              group_evidence or learned_effects),
            "cardinality_evidence_status": (
                "complete_key_group_hash_sample_applied" if key_group_evidence else
                "offline_base_group_frequency_applied" if group_evidence else
                "joint_synopsis_applied" if joint_evidence and joint_evidence["points"] else
                "preexecution_row_provider_applied" if learned_effects else
                "partial_preflight_applied" if bound or dimension_evidence else
                "semantic_only_correction" if light_effects else "planner_only"
            ),
            "automatic_recommendation_qualified": False,
            "preflight_estimates": bound, "cardinality_propagation": effects,
            "light_cardinality_effects": light_effects,
            "dimension_evidence": list(dimension_evidence),
            "group_occupancy_evidence": group_estimates,
            "offline_group_frequency_evidence": group_evidence,
            "offline_group_frequency_effects": group_effects,
            "offline_group_frequency_assumptions": sorted({
                e["frequency_model"] for e in group_evidence.values()
                if "ASSUMPTION" in e["frequency_model"]}),
            "unique_union_group_evidence": unique_evidence,
            "unique_union_group_effects": unique_effects,
            "joint_synopsis_evidence": joint_evidence,
            "joint_cardinality_effects": joint_effects,
            "sql_metadata_available": self.ast is not None,
            "expression_type_evidence": {
                "schema": TYPE_SCHEMA, "available": self.expression_types is not None,
                "scope_preparation_available": (
                    self.expression_types.preparation_available if self.expression_types else False),
                "resolved_argument_expressions": {
                    k: v for k, v in context.expression_types.items() if v is not None},
                "unresolved_argument_expressions": [
                    k for k, v in context.expression_types.items() if v is None],
                "qualification": "type-only bounded AST inference; no new row/NDV/width evidence",
            },
            "formula_compute_ms": (time.perf_counter() - started) * 1000,
            "shared_preparation_ms": self.preparation_ms,
            "forecast_qualification": (
                "experimental logical temp buffer-equivalent bytes; no target runtime labels; "
                "a numeric estimate is not a certified accuracy or zero-spill guarantee"
            ),
        })
        if having_input_bound_scenario:
            result.update({
                "release_schema": "jmt.hypothetical-having-input-bound/v1",
                "is_hypothetical_scenario": True,
                "uses_target_execution_labels": False,
                "scenario_node_paths": scenario_paths,
                "scenario_assumption": "G=N at grouped HAVING sites; conditional on modeled N",
                "scenario_is_new_statistical_evidence": False,
                "automatic_recommendation_qualified": False,
                "forecast_qualification": (
                    "RESOURCE SENSITIVITY SCENARIO ONLY, not a new cardinality estimate, "
                    "truth label, calibrated confidence interval or physical upper bound. "
                    "Input row estimates, joins, skew, grants and widths are not validated."),
            })
        if diagnostic is not None:
            result.update({
                "release_schema": "jmt.POSTEXECUTION-oracle-diagnostic/v1",
                "uses_target_execution_labels": True,
                "automatic_recommendation_qualified": False,
                "forecast_qualification": (
                    "POSTEXECUTION ORACLE DIAGNOSTIC ONLY; target observations replaced "
                    "inputs AFTER statistical preflight. NOT a deployable forecast, "
                    "new holdout score, or model improvement."),
                "diagnostic_replacements": diagnostic,
                "diagnostic_pass_through_row_effects": passthrough,
            })
        return result


def forecast(document, sql, wm, catalog, sample_estimates=None, dimension_evidence=()):
    return ForecastSession(sql, catalog).forecast(document, wm, sample_estimates, dimension_evidence)
