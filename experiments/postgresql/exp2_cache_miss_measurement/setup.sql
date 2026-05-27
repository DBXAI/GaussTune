-- setup.sql: exp2 所需扩展和视图
-- 在 tpcc 和 tpch 数据库中分别执行

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS pg_buffercache;

-- 重置统计（实验开始前调用）
-- SELECT pg_stat_statements_reset();
-- SELECT pg_stat_reset();

-- ── 视图 1：按查询类型分组的 cache miss 统计 ──────────────────────────────
-- 用 application_name 区分 TP/AP（benchbase 会设置 ApplicationName）
CREATE OR REPLACE VIEW v_cachemiss_by_type AS
WITH stmt_stats AS (
    SELECT
        query,
        calls,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_dirtied,
        total_time,
        -- 根据 query 特征判断类型
        CASE
            WHEN query ILIKE '%order_line%' OR query ILIKE '%new_order%'
              OR query ILIKE '%payment%'    OR query ILIKE '%stock%'
              OR query ILIKE '%customer%'   OR query ILIKE '%district%'
              OR query ILIKE '%warehouse%'
            THEN 'TP'
            WHEN query ILIKE '%lineitem%'   OR query ILIKE '%partsupp%'
              OR query ILIKE '%nation%'     OR query ILIKE '%region%'
              OR query ILIKE '%supplier%'   OR query ILIKE '%revenue%'
            THEN 'AP'
            ELSE 'OTHER'
        END AS query_type
    FROM pg_stat_statements
    WHERE shared_blks_hit + shared_blks_read > 0
)
SELECT
    query_type,
    count(*)                                        AS distinct_queries,
    sum(calls)                                      AS total_calls,
    sum(shared_blks_hit)                            AS total_hits,
    sum(shared_blks_read)                           AS total_misses,
    round(
        100.0 * sum(shared_blks_read) /
        nullif(sum(shared_blks_hit) + sum(shared_blks_read), 0),
        4
    )                                               AS miss_rate_pct,
    round(sum(total_time)::numeric / nullif(sum(calls), 0), 3) AS avg_ms_per_call
FROM stmt_stats
GROUP BY query_type
ORDER BY query_type;

-- ── 视图 2：Top-N 高 miss 查询 ────────────────────────────────────────────
CREATE OR REPLACE VIEW v_top_miss_queries AS
SELECT
    left(query, 80)                                 AS query_snippet,
    calls,
    shared_blks_hit,
    shared_blks_read,
    round(
        100.0 * shared_blks_read /
        nullif(shared_blks_hit + shared_blks_read, 0),
        2
    )                                               AS miss_rate_pct,
    round(total_time::numeric / nullif(calls, 0), 3) AS avg_ms
FROM pg_stat_statements
WHERE shared_blks_hit + shared_blks_read > 100
ORDER BY shared_blks_read DESC
LIMIT 20;

-- ── 视图 3：数据库级别 miss 率（总览）────────────────────────────────────
CREATE OR REPLACE VIEW v_db_cachemiss AS
SELECT
    datname,
    blks_hit,
    blks_read,
    round(100.0 * blks_read / nullif(blks_hit + blks_read, 0), 4) AS miss_rate_pct,
    blks_hit + blks_read                            AS total_accesses
FROM pg_stat_database
WHERE datname IN ('tpcc', 'tpch', 'postgres')
ORDER BY datname;

-- ── 快照表（用于时间序列采集）────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cachemiss_snapshots (
    snap_time       TIMESTAMPTZ DEFAULT now(),
    workload_phase  TEXT,           -- 'tpcc_only', 'tpch_only', 'mixed'
    query_type      TEXT,           -- 'TP', 'AP', 'TOTAL'
    calls           BIGINT,
    hits            BIGINT,
    misses          BIGINT,
    miss_rate_pct   NUMERIC(8,4)
);

-- 采集一次快照的函数
CREATE OR REPLACE FUNCTION take_cachemiss_snapshot(phase TEXT) RETURNS void AS $$
-- 数据库级别总量
INSERT INTO cachemiss_snapshots(workload_phase, query_type, calls, hits, misses, miss_rate_pct)
SELECT
    phase,
    'TOTAL',
    xact_commit + xact_rollback,
    blks_hit,
    blks_read,
    round(100.0 * blks_read / nullif(blks_hit + blks_read, 0), 4)
FROM pg_stat_database
WHERE datname = current_database();

-- pg_stat_statements 分类
INSERT INTO cachemiss_snapshots(workload_phase, query_type, calls, hits, misses, miss_rate_pct)
SELECT
    phase,
    query_type,
    total_calls,
    total_hits,
    total_misses,
    miss_rate_pct
FROM v_cachemiss_by_type
WHERE query_type IN ('TP', 'AP');
$$ LANGUAGE sql;
