"""Generic PRE-execution input adapter; never imports execution observations."""
import copy
import math

from .ap_plan_model import blind_root


def apply(document, evidence):
    if evidence.get("schema") != "jmt.preexecution-cardinality-evidence/v1":
        raise ValueError("unqualified learned input schema")
    if evidence.get("uses_target_execution_labels") is not False:
        raise ValueError("target labels cannot be predictive input")
    changed = copy.deepcopy(document)
    rows, groups = evidence["rows"], evidence["groups"]
    effects, consumed = [], set()

    def visit(node, path):
        before = float(node.get("Plan Rows", 0))
        main = [(i, c) for i, c in enumerate(node.get("Plans", []))
                if c.get("Parent Relationship") not in ("InitPlan", "SubPlan")]
        old = [float(c.get("Plan Rows", 0)) for _, c in main]
        for i, child in enumerate(node.get("Plans", [])):
            visit(child, path + "." + str(i))
        new = [float(c.get("Plan Rows", 0)) for _, c in main]
        after, source = before, None
        if path in rows:
            kind = node["Node Type"]
            existence = evidence.get("certified_existence_bindings", {}).get(path)
            sampled = evidence.get("certified_sample_join_bindings", {}).get(path)
            if sampled is not None and existence is not None:
                raise ValueError("do not mix overlapping sampled and existence cardinalities")
            if sampled is not None:
                from .join_sample import validate_binding
                validate_binding(node, sampled, rows[path])
            elif existence is not None:
                from .existence_binding import validate
                outer_side, inner_side = validate(node, existence)
            elif kind not in ("Hash Join", "Nested Loop", "Seq Scan") or (
                kind != "Seq Scan" and node.get("Join Type") != "Inner"
            ):
                raise ValueError("learned N attached outside supported join scope")
            if kind == "Seq Scan":
                binding = evidence.get("certified_scan_bindings", {}).get(path, {})
                if (not node.get("Filter") or binding.get("relation") != node.get("Relation Name")
                        or binding.get("population_fact") != node.get("Relation Name")
                        or binding.get("predicate_scope_complete") is not True
                        or not binding.get("metadata_sha256")):
                    raise ValueError("scan lacks independent-population predicate binding")
                if binding.get("source") == "offline_repeated_block_predicate_sample":
                    from .scan_sample import validate_binding
                    validate_binding(node, binding, rows[path])
            if kind == "Nested Loop" and sampled is None:
                binding = evidence.get("certified_nested_loop_bindings", {}).get(path, {})
                if (binding.get("semantics") != "certified_small_dimension_unparameterized_fk"
                        or not binding.get("certificate_sha256")
                        or not binding.get("metadata_sha256")):
                    raise ValueError("Nested Loop lacks preexecution FK/dimension certificate")
            after, source = rows[path], "pre_execution_N"
            if sampled is not None:
                source = "single_anchor_direct_join_sample_N"
            if existence is not None:
                after = min(after, new[outer_side])
                if existence["encoding"] == "unique_inner_join":
                    after = min(after, new[inner_side])
                source = "complete_key_existence_N_with_semantic_bounds"
            consumed.add(path)
        elif path in groups:
            if node["Node Type"] not in ("Aggregate", "HashAggregate") or len(new) != 1:
                raise ValueError("learned G attached outside grouped aggregate")
            after, source = min(groups[path], new[0]), "independent_complete_key_G"
            consumed.add(path)
        elif old != new and len(new) == 1:
            if node["Node Type"] in ("Hash", "Sort", "Materialize", "Result", "Subquery Scan") \
                    and not node.get("Filter"):
                after, source = new[0], "semantic_pass_through"
            elif node["Node Type"] == "Limit":
                after, source = min(before, new[0]), "limit_upper_bound"
            elif node["Node Type"] in ("Aggregate", "HashAggregate") and (
                node.get("Group By Key", node.get("Group Key"))
                and not node.get("Filter") and not node.get("Grouping Sets")
            ):
                # Coherence bound only: this does NOT estimate G from N.
                after, source = min(before, new[0]), "G_le_N_upper_bound_NOT_G_model"
        if not math.isfinite(after) or after < 0:
            raise ValueError("invalid pre-execution cardinality")
        node["Plan Rows"] = after
        if source is not None:
            effects.append({
                "path": path, "node_type": node["Node Type"], "before": before,
                "after": after, "source": source,
            })

    visit(blind_root(changed), "")
    if consumed != set(rows) | set(groups):
        raise ValueError("unknown cardinality binding path")
    return changed, effects
