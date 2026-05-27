-- setup.sql: exp1 所需扩展和辅助视图（sbtest 数据库）
CREATE EXTENSION IF NOT EXISTS pg_buffercache;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE IF NOT EXISTS bgwriter_snapshots (
    snap_time        TIMESTAMPTZ DEFAULT now(),
    buffers_clean    BIGINT,
    buffers_alloc    BIGINT,
    buffers_backend  BIGINT,
    maxwritten_clean BIGINT
);

CREATE OR REPLACE FUNCTION take_bgwriter_snapshot() RETURNS void AS $$
INSERT INTO bgwriter_snapshots(buffers_clean, buffers_alloc, buffers_backend, maxwritten_clean)
SELECT buffers_clean, buffers_alloc, buffers_backend, maxwritten_clean
FROM pg_stat_bgwriter;
$$ LANGUAGE sql;

CREATE OR REPLACE VIEW v_buffer_usage AS
SELECT
    c.relname,
    count(*)                                    AS buffers_in_pool,
    round(count(*) * 8.0 / 1024, 2)            AS size_mb,
    round(100.0 * count(*) /
          (SELECT setting::int FROM pg_settings WHERE name='shared_buffers'), 2) AS pct_of_pool
FROM pg_buffercache b
JOIN pg_class c ON c.relfilenode = b.relfilenode
WHERE b.reldatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND b.usagecount IS NOT NULL
GROUP BY c.relname
ORDER BY buffers_in_pool DESC
LIMIT 30;

CREATE OR REPLACE VIEW v_usagecount_dist AS
SELECT
    usagecount,
    count(*)                                    AS num_buffers,
    round(100.0 * count(*) /
          (SELECT setting::int FROM pg_settings WHERE name='shared_buffers'), 2) AS pct
FROM pg_buffercache
GROUP BY usagecount
ORDER BY usagecount;
