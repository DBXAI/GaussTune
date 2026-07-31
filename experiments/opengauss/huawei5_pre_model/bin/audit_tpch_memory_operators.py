#!/usr/bin/env python3
"""Audit memory-sensitive operators in the eight Huawei5 AP queries."""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import tpc5stage  # noqa: E402


QUERIES = {
    1: """
SELECT l_returnflag, l_linestatus,
       SUM(l_quantity), SUM(l_extendedprice),
       SUM(l_extendedprice * (1-l_discount)),
       SUM(l_extendedprice * (1-l_discount) * (1+l_tax)),
       AVG(l_quantity), AVG(l_extendedprice), AVG(l_discount), COUNT(*)
FROM lineitem
WHERE l_shipdate <= DATE '1998-12-01' - INTERVAL '90' DAY
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus
""",
    3: """
SELECT l_orderkey, SUM(l_extendedprice * (1-l_discount)) AS revenue,
       o_orderdate, o_shippriority
FROM customer, orders, lineitem
WHERE c_mktsegment = 'BUILDING'
  AND c_custkey = o_custkey
  AND l_orderkey = o_orderkey
  AND o_orderdate < DATE '1995-03-15'
  AND l_shipdate > DATE '1995-03-15'
GROUP BY l_orderkey, o_orderdate, o_shippriority
ORDER BY revenue DESC, o_orderdate
LIMIT 10
""",
    5: """
SELECT n_name, SUM(l_extendedprice * (1-l_discount)) AS revenue
FROM customer, orders, lineitem, supplier, nation, region
WHERE c_custkey = o_custkey
  AND l_orderkey = o_orderkey
  AND l_suppkey = s_suppkey
  AND c_nationkey = s_nationkey
  AND s_nationkey = n_nationkey
  AND n_regionkey = r_regionkey
  AND r_name = 'ASIA'
  AND o_orderdate >= DATE '1994-01-01'
  AND o_orderdate < DATE '1994-01-01' + INTERVAL '1' YEAR
GROUP BY n_name
ORDER BY revenue DESC
""",
    7: """
SELECT supp_nation, cust_nation, l_year, SUM(volume) AS revenue
FROM (
    SELECT n1.n_name AS supp_nation, n2.n_name AS cust_nation,
           EXTRACT(YEAR FROM l_shipdate) AS l_year,
           l_extendedprice * (1-l_discount) AS volume
    FROM supplier, lineitem, orders, customer, nation n1, nation n2
    WHERE s_suppkey = l_suppkey
      AND o_orderkey = l_orderkey
      AND c_custkey = o_custkey
      AND s_nationkey = n1.n_nationkey
      AND c_nationkey = n2.n_nationkey
      AND ((n1.n_name='FRANCE' AND n2.n_name='GERMANY')
        OR (n1.n_name='GERMANY' AND n2.n_name='FRANCE'))
      AND l_shipdate BETWEEN DATE '1995-01-01' AND DATE '1996-12-31'
) shipping
GROUP BY supp_nation, cust_nation, l_year
ORDER BY supp_nation, cust_nation, l_year
""",
    9: """
SELECT nation, o_year, SUM(amount) AS sum_profit
FROM (
    SELECT n_name AS nation, EXTRACT(YEAR FROM o_orderdate) AS o_year,
           l_extendedprice * (1-l_discount) - ps_supplycost*l_quantity AS amount
    FROM part, supplier, lineitem, partsupp, orders, nation
    WHERE s_suppkey = l_suppkey
      AND ps_suppkey = l_suppkey
      AND ps_partkey = l_partkey
      AND p_partkey = l_partkey
      AND o_orderkey = l_orderkey
      AND s_nationkey = n_nationkey
      AND p_name LIKE '%green%'
) profit
GROUP BY nation, o_year
ORDER BY nation, o_year DESC
""",
    13: """
SELECT c_count, COUNT(*) AS custdist
FROM (
    SELECT c_custkey, COUNT(o_orderkey) AS c_count
    FROM customer LEFT OUTER JOIN orders
      ON c_custkey = o_custkey
     AND o_comment NOT LIKE '%special%requests%'
    GROUP BY c_custkey
) c_orders
GROUP BY c_count
ORDER BY custdist DESC, c_count DESC
""",
    18: """
SELECT c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice, SUM(l_quantity)
FROM customer, orders, lineitem
WHERE o_orderkey IN (
    SELECT l_orderkey FROM lineitem
    GROUP BY l_orderkey HAVING SUM(l_quantity) > 313
)
  AND c_custkey = o_custkey
  AND o_orderkey = l_orderkey
GROUP BY c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice
ORDER BY o_totalprice DESC, o_orderdate
LIMIT 100
""",
    21: """
SELECT s_name, COUNT(*) AS numwait
FROM supplier, lineitem l1, orders, nation
WHERE s_suppkey = l1.l_suppkey
  AND o_orderkey = l1.l_orderkey
  AND o_orderstatus = 'F'
  AND l1.l_receiptdate > l1.l_commitdate
  AND EXISTS (
      SELECT 1 FROM lineitem l2
      WHERE l2.l_orderkey = l1.l_orderkey
        AND l2.l_suppkey <> l1.l_suppkey
  )
  AND NOT EXISTS (
      SELECT 1 FROM lineitem l3
      WHERE l3.l_orderkey = l1.l_orderkey
        AND l3.l_suppkey <> l1.l_suppkey
        AND l3.l_receiptdate > l3.l_commitdate
  )
  AND s_nationkey = n_nationkey
  AND n_name = 'SAUDI ARABIA'
GROUP BY s_name
ORDER BY numwait DESC, s_name
LIMIT 100
""",
}


PATTERNS = {
    "hash_join": r"\bHash (?:Left |Right |Full |Semi |Anti )?Join\b",
    "hash_build": r"(?:^|->\s+)Hash\s+\(",
    "hash_aggregate": r"\bHashAggregate\b",
    "sort": r"(?:^|->\s+)Sort\s+\(",
    "group_aggregate": r"\bGroupAggregate\b",
    "materialize": r"\bMaterialize\b",
    "nested_loop": r"\bNested Loop\b",
    "merge_join": r"\bMerge Join\b",
    "vector_operator": r"\bVec(?:tor)?\w*",
    "sonic_operator": r"\bSonic\w*",
}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out_dir = root / "results" / "tpch_memory_operator_audit_20260721"
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = tpc5stage.gsql_output(
        "SHOW query_dop; SHOW enable_vector_engine; SHOW work_mem;", db="h5_tpch"
    ).splitlines()

    rows = []
    for query_id, query in QUERIES.items():
        sql = query.strip().rstrip(";")
        plan = tpc5stage.gsql_output(
            "SET enable_hashjoin=on; SET enable_mergejoin=on; SET enable_nestloop=on;\n"
            f"EXPLAIN {sql};\n",
            db="h5_tpch",
        )
        (out_dir / f"q{query_id}.sql").write_text(sql + ";\n", encoding="utf-8")
        (out_dir / f"q{query_id}_explain.txt").write_text(plan + "\n", encoding="utf-8")
        row: dict[str, object] = {"query_id": query_id}
        for name, pattern in PATTERNS.items():
            row[name] = len(re.findall(pattern, plan, flags=re.MULTILINE))
        row["memory_operator_count"] = (
            int(row["hash_build"])
            + int(row["hash_aggregate"])
            + int(row["sort"])
        )
        rows.append(row)

    with (out_dir / "operator_coverage.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    totals = {name: sum(int(row[name]) for row in rows) for name in PATTERNS}
    summary = {
        "queries": sorted(QUERIES),
        "query_dop": int(settings[0]),
        "enable_vector_engine": settings[1],
        "default_work_mem": settings[2],
        "operator_totals": totals,
        "queries_with_hash_join": sum(int(row["hash_join"]) > 0 for row in rows),
        "queries_with_hash_aggregate": sum(int(row["hash_aggregate"]) > 0 for row in rows),
        "queries_with_sort": sum(int(row["sort"]) > 0 for row in rows),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(out_dir / "operator_coverage.csv")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
