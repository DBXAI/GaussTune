-- sbtest_queries.sql
-- sysbench sbtest 代表性查询，用于测量不同 shared_buffers 下的 SQL 执行时间
-- 覆盖：点查（PK）、索引范围扫描、全表聚合、多表 join 等场景

-- Q1: 主键点查（sysbench oltp_read_only 核心查询，热点 page 命中率高）
\echo 'QUERY_START PK_POINT_QUERY'
\timing on
SELECT id, k, c, pad
FROM sbtest1
WHERE id IN (
    SELECT (random() * 9999999 + 1)::int FROM generate_series(1, 1000)
);
\timing off
\echo 'QUERY_END PK_POINT_QUERY'

-- Q2: 二级索引范围扫描（k 列索引，模拟 sysbench range scan）
\echo 'QUERY_START INDEX_RANGE_SCAN'
\timing on
SELECT id, k, c
FROM sbtest1
WHERE k BETWEEN 490000 AND 510000
ORDER BY k;
\timing off
\echo 'QUERY_END INDEX_RANGE_SCAN'

-- Q3: 全表聚合（sbtest1 全扫，2GB 表，最能体现 buffer size 影响）
\echo 'QUERY_START FULL_TABLE_AGG'
\timing on
SELECT
    count(*)            AS total_rows,
    avg(k)              AS avg_k,
    min(k)              AS min_k,
    max(k)              AS max_k,
    sum(k)              AS sum_k
FROM sbtest1;
\timing off
\echo 'QUERY_END FULL_TABLE_AGG'

-- Q4: 多表 join 聚合（sbtest1 + sbtest2 + sbtest3，模拟跨表分析）
\echo 'QUERY_START MULTI_TABLE_JOIN'
\timing on
SELECT
    t1.k                AS k1,
    count(*)            AS match_count,
    avg(t2.k)           AS avg_k2,
    sum(t3.k)           AS sum_k3
FROM sbtest1 t1
JOIN sbtest2 t2 ON t1.id = t2.id
JOIN sbtest3 t3 ON t1.id = t3.id
WHERE t1.k BETWEEN 400000 AND 600000
GROUP BY t1.k
ORDER BY match_count DESC
LIMIT 20;
\timing off
\echo 'QUERY_END MULTI_TABLE_JOIN'

-- Q5: 大范围全扫描聚合（10张表 union，最大内存压力，最能区分 buffer size）
\echo 'QUERY_START ALL_TABLES_SCAN'
\timing on
SELECT
    tbl,
    count(*)    AS rows,
    avg(k)      AS avg_k,
    sum(k)      AS sum_k
FROM (
    SELECT 'sbtest1'  AS tbl, k FROM sbtest1  UNION ALL
    SELECT 'sbtest2'  AS tbl, k FROM sbtest2  UNION ALL
    SELECT 'sbtest3'  AS tbl, k FROM sbtest3  UNION ALL
    SELECT 'sbtest4'  AS tbl, k FROM sbtest4  UNION ALL
    SELECT 'sbtest5'  AS tbl, k FROM sbtest5
) sub
GROUP BY tbl
ORDER BY tbl;
\timing off
\echo 'QUERY_END ALL_TABLES_SCAN'
