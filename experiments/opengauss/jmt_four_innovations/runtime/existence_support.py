"""Light complete-customer-key estimator for EXISTS and NOT EXISTS.

All orders of sampled customers are maintained OFFLINE. NOT EXISTS additionally
uses the matching customer universe, including customers with no orders.
No target database calls, query identifiers, or execution observations.
"""
import time

import numpy as np
import sqlglot

import legacy_service as old
from jmt.innovation_2_spill.existence_binding import scope_sha256


KEY_EDGE = frozenset(("customer.c_custkey", "orders.o_custkey"))
WRAPPERS = ("Hash", "Sort", "Materialize")


def unwrap(node):
    while node["Node Type"] in WRAPPERS:
        if node.get("Filter") or len(node.get("Plans", [])) != 1:
            raise old.Unsupported("filtered/nonunary existence wrapper")
        node = node["Plans"][0]
    return node


def recognize(node, subtree, dimension_certificate):
    if node["Node Type"] != "Hash Join" or len(node.get("Plans", [])) != 2:
        raise old.Unsupported("existence requires binary Hash Join")
    if node.get("Filter") or node.get("Join Filter"):
        raise old.Unsupported("additional existence join predicates")
    jt = node.get("Join Type")
    if jt not in ("Inner", "Semi", "Anti", "Right Semi", "Right Anti"):
        raise old.Unsupported("unsupported existence join type")
    sides = []
    for side in node["Plans"]:
        leaf = unwrap(side)
        dedup = None
        if leaf["Node Type"] in ("Aggregate", "HashAggregate"):
            if (leaf.get("Filter") or leaf.get("Grouping Sets")
                    or len(leaf.get("Plans", [])) != 1):
                raise old.Unsupported("filtered/ambiguous existence deduplication")
            dedup, leaf = leaf, leaf["Plans"][0]
        aliases, conditions, edges = subtree(leaf, dimension_certificate)
        sides.append((aliases, conditions, edges, dedup))
    parent_candidates = [
        i for i, s in enumerate(sides)
        if "customer" in s[0].values()
        and set(s[0].values()) <= {"customer", "nation"} and s[3] is None
    ]
    orders_candidates = [
        i for i, s in enumerate(sides) if set(s[0].values()) == {"orders"}
    ]
    if len(parent_candidates) != 1 or len(orders_candidates) != 1:
        raise old.Unsupported("existence requires customer and independent orders scope")
    parent_side, orders_side = parent_candidates[0], orders_candidates[0]
    if parent_side == orders_side:
        raise old.Unsupported("overlapping existence sides")
    parent, orders = sides[parent_side], sides[orders_side]
    if set(parent[0]) & set(orders[0]):
        raise old.Unsupported("ambiguous existence aliases")
    aliases = {**parent[0], **orders[0]}
    literal, edges = old.parse_condition(node.get("Hash Cond", ""), aliases)
    if literal or edges != [KEY_EDGE]:
        raise old.Unsupported("existence lacks its exact declared key equality")
    dedup = orders[3]
    if dedup is not None:
        keys = tuple(
            old.column(sqlglot.parse_one(k, read="postgres"), orders[0])
            for k in dedup.get("Group By Key", dedup.get("Group Key", []))
        )
        if keys != ("orders.o_custkey",):
            raise old.Unsupported("deduplicated orders must be unique on the join key alone")
    if jt == "Inner" and dedup is None:
        raise old.Unsupported("ordinary many-to-one join is NOT existence")
    if jt in ("Semi", "Anti") and parent_side != 0:
        raise old.Unsupported("left semi/anti parent side mismatch")
    if jt in ("Right Semi", "Right Anti") and parent_side != 1:
        raise old.Unsupported("right semi/anti parent side mismatch")
    return {
        "parent_side": parent_side, "orders_side": orders_side,
        "parent_conditions": parent[1], "orders_conditions": orders[1],
        "relations": sorted(aliases.values()),
        "encoding": {
            "Inner": "unique_inner_join", "Semi": "explicit_semi",
            "Anti": "explicit_anti", "Right Semi": "explicit_right_semi",
            "Right Anti": "explicit_right_anti",
        }[jt],
        "semantics": "customer_order_anti" if "Anti" in jt else "customer_order_semi",
    }


def estimate(node, service, metadata_sha256, subtree, cache):
    certificate = getattr(service, "existence_certificate", None)
    if (not certificate or not certificate.get("valid")
            or time.time() > certificate["expires_unix"]
            or certificate["metadata_sha256"] != metadata_sha256):
        raise old.Unsupported("missing/expired complete-key existence certificate")
    info = recognize(node, subtree, service.certificate)
    parent = service.frames["existence_customers"]
    orders = service.frames["existence_orders"]
    p = service.index["frames"]["existence_customers"]["p"]
    if p != service.index["frames"]["existence_orders"]["p"]:
        raise old.Unsupported("existence parent/order inclusion probabilities differ")
    signature = (
        info["semantics"], info["parent_conditions"], info["orders_conditions"],
    )
    if signature not in cache:
        if len(cache) >= 2048:
            raise old.Unsupported("existence request cache budget")
        parent_keys = parent.columns["customer.c_custkey"][parent.mask(info["parent_conditions"])]
        order_keys = orders.columns["orders.o_custkey"][orders.mask(info["orders_conditions"])]
        matching = np.unique(order_keys)
        # Parent keys were proved unique offline; this is one row per sampled
        # parent, not one row per matching order or lineitem.
        has_order = np.isin(parent_keys, matching, assume_unique=True)
        matched = int(np.count_nonzero(has_order))
        support = len(parent_keys) - matched if info["semantics"].endswith("anti") else matched
        cache[signature] = {
            "estimate": support / p, "support": support, "p": p,
            "sampled_eligible_parents": len(parent_keys),
            "sampled_matching_parents": matched,
            "sampled_qualifying_orders": len(order_keys),
        }
    binding = {
        "schema": "jmt.complete-key-existence-binding/v1",
        "semantics": info["semantics"], "encoding": info["encoding"],
        "parent_side": info["parent_side"], "orders_side": info["orders_side"],
        "parent_key": "customer.c_custkey", "orders_key": "orders.o_custkey",
        "scope_sha256": scope_sha256(node),
        "certificate_sha256": certificate["certificate_sha256"],
        "metadata_sha256": metadata_sha256,
    }
    return {**cache[signature], **info, "binding": binding}
