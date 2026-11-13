"""Bounded CPU-only expression work; does not change spill N/G or IO formulas."""
import math


def corrected_cpu_work(document, operators, catalog):
    import sqlglot
    from sqlglot import exp
    from jmt.innovation_2_spill.ap_plan_model import aggregate_node_calls, blind_root
    root = blind_root(document)
    nodes = {}
    aliases = {}
    def visit(node, path=""):
        if len(nodes) >= 4096:
            raise ValueError("CPU plan-node budget")
        nodes[path] = node
        if node.get("Relation Name"):
            aliases[node.get("Alias", node["Relation Name"])] = node["Relation Name"]
        for i, child in enumerate(node.get("Plans", [])):
            visit(child, path + "." + str(i))
    visit(root)
    numeric_columns = {
        (row["tablename"], row["attname"]) for row in catalog.get("columns", [])
        if row["typname"] in ("numeric", "decimal")
    }
    def parse(text):
        expression = sqlglot.parse_one(text, read="postgres")
        if sum(1 for _ in expression.walk()) > 512:
            raise ValueError("CPU expression AST budget")
        return expression
    def numeric(expression):
        return any(
            (aliases.get(col.table, col.table), col.name) in numeric_columns
            or not col.table and sum(name == col.name for _, name in numeric_columns) == 1
            for col in expression.find_all(exp.Column)
        )
    def cost(expression):
        if isinstance(expression, (exp.Literal, exp.Column, exp.Identifier, exp.DataType)):
            return 0
        if isinstance(expression, (exp.Paren, exp.Alias)):
            return cost(expression.this)
        if isinstance(expression, exp.Cast) and not list(expression.this.find_all(exp.Column)):
            return 0  # planner constant cast is not performed once per tuple
        children = list(expression.iter_expressions())
        if isinstance(expression, (exp.And, exp.Or)):
            return sum(cost(child) for child in children)
        return 1 + sum(cost(child) for child in children)
    def conjuncts(expression):
        while isinstance(expression, exp.Paren):
            expression = expression.this
        if isinstance(expression, exp.And):
            return conjuncts(expression.this) + conjuncts(expression.expression)
        return [expression]
    predicate = sum(op["features"]["predicate_evaluations"] for op in operators)
    numeric_arithmetic = numeric_avg = 0.0
    corrections = []
    for op in operators:
        node = nodes.get(op["path"], {})
        calls = op["calls"]
        if op["node_type"] == "Seq Scan" and node.get("Filter"):
            expression = parse(node["Filter"])
            terms = conjuncts(expression)
            scanned = op["features"]["scan_tuples"]
            emitted = op["output_rows_per_call"] * calls
            selection = min(1.0, emitted / scanned) if scanned else 0.0
            prefix_selectivity = selection ** (1.0 / len(terms))
            units = sum(max(1, cost(term)) * prefix_selectivity**i
                        for i, term in enumerate(terms))
            corrected = scanned * units
            predicate += corrected - op["features"]["predicate_evaluations"]
            corrections.append({
                "path": op["path"], "kind": "constant-cast/comparison normalization",
                "old_units": op["features"]["predicate_evaluations"],
                "new_units": corrected,
                "AND_prefix_model": (
                    "equal per-atom selectivity approximation, NOT measured prefix truth"
                    if len(terms) > 1 else "single predicate"),
            })
        if op["node_type"] not in ("Aggregate", "HashAggregate"):
            continue
        for call in aggregate_node_calls(node):
            arguments = call["arguments"]
            if arguments.strip() in ("", "*"):
                continue
            try:
                expression = parse(arguments)
            except Exception:
                corrections.append({"path": op["path"], "kind": "unresolved aggregate argument type"})
                continue
            if not numeric(expression):
                continue
            rows = op["input_rows_per_call"] * calls
            numeric_arithmetic += rows * sum(
                isinstance(term, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod))
                and numeric(term) for term in expression.walk())
            if call["function"] == "avg":
                numeric_avg += rows
    return {
        "predicate_evaluations": max(0.0, predicate),
        "numeric_arithmetic_M": numeric_arithmetic / 1e6,
        "numeric_avg_M": numeric_avg / 1e6,
        "corrections": corrections,
        "qualification": "CPU work proxies only; short-circuit selectivity is an explicit approximation.",
    }
