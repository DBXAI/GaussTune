-- setup.sql: exp2 所需扩展和视图（sbtest 数据库）
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS pg_buffercache;

-- ── 视图 1：按查询类型分组的 cache miss 统计 ──────────────────────────────
-- sysbench oltp_read_only 产生点查（PRIMARY KEY）和范围扫描（k 索引）
-- oltp_write_only 产生 INSERT/UPDATE/DELETE
-- 用查询特征区分读/写类型
CREATE OR REPLACE VIEW v_cachemiss_by_type AS
WITH stmt_stats AS (
    SELECT
        query,
        calls,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_dirtied,
        total_time,
        CASE
            WHEN query ILIKE '%SELECT%' AND query ILIKE '%sbtest%' THEN 'READ'
            WHEN query ILIKE '%UPDATE%' OR query ILIKE '%INSERT%'
              OR query ILIKE '%DELETE%'                             THEN 'WRITE'
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
WHERE datname = 'sbtest'
ORDER BY datname;

-- ── 快照表 ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cachemiss_snapshots (
    snap_time       TIMESTAMPTZ DEFAULT now(),
    workload_phase  TEXT,
    query_type      TEXT,
    calls           BIGINT,
    hits            BIGINT,
    misses          BIGINT,
    miss_rate_pct   NUMERIC(8,4)
);

CREATE OR REPLACE FUNCTION take_cachemiss_snapshot(phase TEXT) RETURNS void AS $$
INSERT INTO cachemiss_snapshots(workload_phase, query_type, calls, hits, misses, miss_rate_pct)
SELECT
    phase, 'TOTAL',
    xact_commit + xact_rollback,
    blks_hit, blks_read,
    round(100.0 * blks_read / nullif(blks_hit + blks_read, 0), 4)
FROM pg_stat_database
WHERE datname = 'sbtest';

INSERT INTO cachemiss_snapshots(workload_phase, query_type, calls, hits, misses, miss_rate_pct)
SELECT phase, query_type, total_calls, total_hits, total_misses, miss_rate_pct
FROM v_cachemiss_by_type
WHERE query_type IN ('READ', 'WRITE');
$$ LANGUAGE sql;
