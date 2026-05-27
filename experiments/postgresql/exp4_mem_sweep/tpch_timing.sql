-- tpch_timing.sql
-- TPC-H 代表性查询，用 EXPLAIN (ANALYZE, BUFFERS) 精确记录执行时间和 buffer 使用
--
-- 选取原则：
--   Q1  — lineitem 全表扫描 + 聚合，最大 I/O 压力，最能体现 buffer pool 大小
--   Q6  — lineitem 过滤聚合（选择率 ~2%），测量选择性扫描的 cache 效果
--   Q14 — lineitem JOIN part，测量多表 join 的 buffer 竞争
--
-- 输出格式：每个查询前打印分隔符，EXPLAIN ANALYZE 输出含 Buffers 行
-- parse_explain.py 依赖此格式解析

-- 关闭并行（让结果更稳定，排除并行度差异）
SET max_parallel_workers_per_gather = 0;
-- 关闭 JIT（排除 JIT 编译时间干扰）
SET jit = off;

-- ════════════════════════════════════════════════════════════
-- QUERY: Q1  DESC: lineitem full scan + group-by aggregation
-- ════════════════════════════════════════════════════════════
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
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
WHERE l_shipdate <= date '1998-12-01' - interval '90 day'
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus;

-- ════════════════════════════════════════════════════════════
-- QUERY: Q6  DESC: lineitem filter + sum (selective scan)
-- ════════════════════════════════════════════════════════════
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT
    sum(l_extendedprice * l_discount) AS revenue
FROM lineitem
WHERE l_shipdate >= date '1994-01-01'
  AND l_shipdate  < date '1994-01-01' + interval '1 year'
  AND l_discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01
  AND l_quantity < 24;

-- ════════════════════════════════════════════════════════════
-- QUERY: Q14 DESC: lineitem JOIN part + case aggregation
-- ════════════════════════════════════════════════════════════
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT
    100.00 * sum(CASE
        WHEN p_type LIKE 'PROMO%'
        THEN l_extendedprice * (1 - l_discount)
        ELSE 0
    END) / sum(l_extendedprice * (1 - l_discount)) AS promo_revenue
FROM lineitem
JOIN part ON l_partkey = p_partkey
WHERE l_shipdate >= date '1995-09-01'
  AND l_shipdate  < date '1995-09-01' + interval '1 month';

-- ════════════════════════════════════════════════════════════
-- QUERY: Q3  DESC: customer + orders + lineitem 3-way join
-- ════════════════════════════════════════════════════════════
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT
    l_orderkey,
    sum(l_extendedprice * (1 - l_discount)) AS revenue,
    o_orderdate,
    o_shippriority
FROM customer
JOIN orders   ON c_custkey = o_custkey
JOIN lineitem ON l_orderkey = o_orderkey
WHERE c_mktsegment = 'BUILDING'
  AND o_orderdate  < date '1995-03-15'
  AND l_shipdate   > date '1995-03-15'
GROUP BY l_orderkey, o_orderdate, o_shippriority
ORDER BY revenue DESC, o_orderdate
LIMIT 10;
