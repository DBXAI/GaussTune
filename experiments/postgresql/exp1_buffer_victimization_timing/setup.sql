-- setup.sql: 安装 exp1 所需扩展和辅助视图
-- 在 tpcc 和 tpch 数据库中分别执行

CREATE EXTENSION IF NOT EXISTS pg_buffercache;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- 快照 bgwriter 统计（用于计算驱逐率）
CREATE TABLE IF NOT EXISTS bgwriter_snapshots (
    snap_time       TIMESTAMPTZ DEFAULT now(),
    buffers_clean   BIGINT,
    buffers_alloc   BIGINT,
    buffers_backend BIGINT,
    maxwritten_clean BIGINT
);

-- 采集一次快照
CREATE OR REPLACE FUNCTION take_bgwriter_snapshot() RETURNS void AS $$
INSERT INTO bgwriter_snapshots(buffers_clean, buffers_alloc, buffers_backend, maxwritten_clean)
SELECT buffers_clean, buffers_alloc, buffers_backend, maxwritten_clean
FROM pg_stat_bgwriter;
$$ LANGUAGE sql;

-- 查看 buffer pool 当前使用情况
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

-- 查看 usage_count 分布（clock-sweep 驱逐优先选 usagecount=0 的页）
CREATE OR REPLACE VIEW v_usagecount_dist AS
SELECT
    usagecount,
    count(*)                                    AS num_buffers,
    round(100.0 * count(*) /
          (SELECT setting::int FROM pg_settings WHERE name='shared_buffers'), 2) AS pct
FROM pg_buffercache
GROUP BY usagecount
ORDER BY usagecount;
