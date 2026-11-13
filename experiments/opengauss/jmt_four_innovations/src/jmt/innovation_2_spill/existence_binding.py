"""Structural binding for complete-key existence estimates.

This module does not estimate from query names or inspect runtime observations.
The offline provider proves the population/key scope; this adapter prevents its
evidence from being accidentally attached to a different plan or join meaning.
"""
import hashlib
import json


def scope_sha256(node):
    fields = (
        "Node Type", "Join Type", "Relation Name", "Schema", "Alias",
        "Parent Relationship", "Group By Key", "Group Key", "Grouping Sets",
        "Filter", "Hash Cond", "Join Filter", "Index Cond",
    )

    def semantic(current):
        result = {k: current[k] for k in fields if k in current}
        result["Plans"] = [semantic(c) for c in current.get("Plans", [])]
        return result

    return hashlib.sha256(json.dumps(semantic(node), sort_keys=True).encode()).hexdigest()


def validate(node, binding):
    if binding.get("schema") != "jmt.complete-key-existence-binding/v1":
        raise ValueError("unqualified existence binding schema")
    if node.get("Node Type") != "Hash Join" or len(node.get("Plans", [])) != 2:
        raise ValueError("existence evidence only supports binary Hash Join")
    if not binding.get("certificate_sha256") or not binding.get("metadata_sha256"):
        raise ValueError("existence evidence lacks a live offline key certificate")
    if binding.get("scope_sha256") != scope_sha256(node):
        raise ValueError("existence evidence belongs to a different semantic subtree")
    encoding = binding.get("encoding")
    semantics = binding.get("semantics")
    join_type = node.get("Join Type")
    if (encoding, semantics, join_type) not in (
        ("unique_inner_join", "customer_order_semi", "Inner"),
        ("explicit_semi", "customer_order_semi", "Semi"),
        ("explicit_anti", "customer_order_anti", "Anti"),
        ("explicit_right_semi", "customer_order_semi", "Right Semi"),
        ("explicit_right_anti", "customer_order_anti", "Right Anti"),
    ):
        raise ValueError("existence binding/join semantics mismatch")
    outer, inner = binding.get("parent_side"), binding.get("orders_side")
    if type(outer) is not int or type(inner) is not int or {outer, inner} != {0, 1}:
        raise ValueError("invalid existence sides")
    if encoding in ("explicit_semi", "explicit_anti") and outer != 0:
        raise ValueError("left semi/anti parent side mismatch")
    if encoding in ("explicit_right_semi", "explicit_right_anti") and outer != 1:
        raise ValueError("right semi/anti parent side mismatch")
    if binding.get("parent_key") != "customer.c_custkey" \
            or binding.get("orders_key") != "orders.o_custkey":
        raise ValueError("unsupported existence key population")
    return outer, inner
