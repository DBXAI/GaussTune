-- tpch_queries.sql
-- TPC-H 代表性查询，用于测量不同 shared_buffers 下的 SQL 执行时间
-- 每条查询前后用 \timing 和注释标记，便于解析

-- Q1: 全表扫描 + 聚合（lineitem 全扫，最能体现 buffer size 影响）
\echo 'QUERY_START Q1'
\timing on
SELECT
    l_returnflag,
    l_linestatus,
    sum(l_quantity)                                       AS sum_qty,
    sum(l_extendedprice)                                  AS sum_base_price,
    sum(l_extendedprice * (1 - l_discount))               AS sum_disc_price,
    sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
    avg(l_quantity)                                       AS avg_qty,
    avg(l_extendedprice)                                  AS avg_price,
    avg(l_discount)                                       AS avg_disc,
    count(*)                                              AS count_order
FROM lineitem
WHERE l_shipdate <= DATE '1998-12-01' - INTERVAL '90 day'
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus;
\timing off
\echo 'QUERY_END Q1'

-- Q6: 全表扫描 + 简单过滤（无 join，纯 I/O 密集）
\echo 'QUERY_START Q6'
\timing on
SELECT
    sum(l_extendedprice * l_discount) AS revenue
FROM lineitem
WHERE l_shipdate >= DATE '1994-01-01'
  AND l_shipdate <  DATE '1994-01-01' + INTERVAL '1 year'
  AND l_discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01
  AND l_quantity < 24;
\timing off
\echo 'QUERY_END Q6'

-- Q3: lineitem + orders + customer 三表 join + 聚合
\echo 'QUERY_START Q3'
\timing on
SELECT
    l_orderkey,
    sum(l_extendedprice * (1 - l_discount)) AS revenue,
    o_orderdate,
    o_shippriority
FROM customer, orders, lineitem
WHERE c_mktsegment = 'BUILDING'
  AND c_custkey = o_custkey
  AND l_orderkey = o_orderkey
  AND o_orderdate < DATE '1995-03-15'
  AND l_shipdate  > DATE '1995-03-15'
GROUP BY l_orderkey, o_orderdate, o_shippriority
ORDER BY revenue DESC, o_orderdate
LIMIT 10;
\timing off
\echo 'QUERY_END Q3'

-- Q5: 五表 join（customer/orders/lineitem/supplier/nation/region）
\echo 'QUERY_START Q5'
\timing on
SELECT
    n_name,
    sum(l_extendedprice * (1 - l_discount)) AS revenue
FROM customer, orders, lineitem, supplier, nation, region
WHERE c_custkey    = o_custkey
  AND l_orderkey   = o_orderkey
  AND l_suppkey    = s_suppkey
  AND c_nationkey  = s_nationkey
  AND s_nationkey  = n_nationkey
  AND n_regionkey  = r_regionkey
  AND r_name       = 'ASIA'
  AND o_orderdate >= DATE '1994-01-01'
  AND o_orderdate <  DATE '1994-01-01' + INTERVAL '1 year'
GROUP BY n_name
ORDER BY revenue DESC;
\timing off
\echo 'QUERY_END Q5'

-- Q9: 六表 join + 字符串过滤（最复杂，内存压力最大）
\echo 'QUERY_START Q9'
\timing on
SELECT
    nation,
    o_year,
    sum(amount) AS sum_profit
FROM (
    SELECT
        n_name                                                          AS nation,
        extract(year FROM o_orderdate)                                  AS o_year,
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity AS amount
    FROM part, supplier, lineitem, partsupp, orders, nation
    WHERE s_suppkey  = l_suppkey
      AND ps_suppkey = l_suppkey
      AND ps_partkey = l_partkey
      AND p_partkey  = l_partkey
      AND o_orderkey = l_orderkey
      AND s_nationkey = n_nationkey
      AND p_name LIKE '%green%'
) AS profit
GROUP BY nation, o_year
ORDER BY nation, o_year DESC;
\timing off
\echo 'QUERY_END Q9'
