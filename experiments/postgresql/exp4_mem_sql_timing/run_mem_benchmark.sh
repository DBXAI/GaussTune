#!/usr/bin/env bash
# run_mem_benchmark.sh
# 测量不同 shared_buffers 下 TPC-C / TPC-H 代表性查询的执行时间
#
# 原理：
#   1. 依次将 shared_buffers 设为各测试档位，重启 PostgreSQL 使其生效
#   2. 每个档位下：先 DROP CACHE（OS page cache + PG buffer），再运行查询
#   3. 每条查询重复 3 次，记录每次耗时（来自 psql \timing 输出）
#   4. 结果写入 results_<timestamp>/timings.csv，供 analyze_mem_benchmark.py 分析
#
# 测试档位（可通过 BUFFER_SIZES 环境变量覆盖）：
#   128MB（当前）/ 512MB / 2GB / 8GB
#   覆盖：远小于工作集 → 接近工作集 → 超过部分工作集
#
# 用法：
#   bash run_mem_benchmark.sh              # 全量（约 2-3 小时）
#   BUFFER_SIZES="128 512" REPEATS=1 bash run_mem_benchmark.sh  # 快速验证
#
# 注意：需要 root 权限（修改 postgresql.conf + 清 OS cache）

set -euo pipefail

# ── 配置 ──────────────────────────────────────────────────────────────────
BUFFER_SIZES=${BUFFER_SIZES:-"128 512 2048 8192"}   # MB
REPEATS=${REPEATS:-3}                                # 每条查询重复次数
PGUSER=postgres
PGCONF=/etc/postgresql/12/main/postgresql.conf
PGSERVICE=postgresql
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="$SCRIPT_DIR/results_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

# postgres 用户无法读取 /root 下的文件，复制到 /tmp
TPCH_SQL="/tmp/exp4_tpch_queries.sql"
TPCC_SQL="/tmp/exp4_tpcc_queries.sql"
cp "$SCRIPT_DIR/tpch_queries.sql" "$TPCH_SQL"
cp "$SCRIPT_DIR/tpcc_queries.sql" "$TPCC_SQL"
chmod 644 "$TPCH_SQL" "$TPCC_SQL"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Exp4: SQL Execution Time vs shared_buffers                  ║"
echo "╠══════════════════════════════════════════════════════════════╣"
printf "║  Buffer sizes : %-45s║\n" "$BUFFER_SIZES MB"
printf "║  Repeats      : %-45s║\n" "$REPEATS per query"
printf "║  Output       : %-45s║\n" "$OUTDIR"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# CSV 头
TIMINGS_CSV="$OUTDIR/timings.csv"
echo "buffer_mb,workload,query,run,elapsed_ms,blks_hit,blks_read,miss_rate_pct" \
    > "$TIMINGS_CSV"

# ── 辅助函数 ──────────────────────────────────────────────────────────────

set_shared_buffers() {
    local mb=$1
    echo "[exp4] Setting shared_buffers=${mb}MB..."
    # 修改 postgresql.conf
    sed -i "s/^shared_buffers\s*=.*/shared_buffers = ${mb}MB/" "$PGCONF"
    # 验证修改
    grep "^shared_buffers" "$PGCONF"
    # 重启 PostgreSQL（shared_buffers 必须重启才生效）
    systemctl restart "$PGSERVICE"
    sleep 3
    # 确认生效
    local actual
    actual=$(sudo -u "$PGUSER" psql -At -c "SHOW shared_buffers;" 2>/dev/null)
    echo "[exp4] Confirmed: shared_buffers = $actual"
}

drop_caches() {
    echo "[exp4] Dropping OS page cache..."
    # 先让 PG checkpoint，确保脏页写回
    sudo -u "$PGUSER" psql -c "CHECKPOINT;" 2>/dev/null
    # 清 OS page cache（需要 root）
    sync
    echo 3 > /proc/sys/vm/drop_caches
    echo "[exp4] OS cache dropped."
}

# 运行一条查询文件，解析 \timing 输出，返回每条查询的耗时（ms）
# 输出格式：QUERY_NAME elapsed_ms
run_and_parse() {
    local db=$1
    local sql_file=$2
    local outfile=$3

    # 运行 psql，捕获完整输出（含 \timing 的 "Time: X.XXX ms" 行）
    sudo -u "$PGUSER" psql -d "$db" \
        --no-psqlrc \
        -v ON_ERROR_STOP=0 \
        -f "$sql_file" 2>&1 | tee "$outfile"
}

# 从 psql 输出中提取 (query_name, elapsed_ms) 对
parse_timings() {
    local raw_file=$1
    python3 - "$raw_file" << 'PYEOF'
import sys, re

raw = open(sys.argv[1]).read()

# 找所有 QUERY_START <name> ... Time: X.XXX ms ... QUERY_END <name>
starts = list(re.finditer(r'QUERY_START (\S+)', raw))
ends   = list(re.finditer(r'QUERY_END (\S+)', raw))
times  = list(re.finditer(r'Time:\s+([\d.]+)\s+ms', raw))

# 按位置匹配：每个 QUERY_START 之后找最近的 Time:
results = []
for s in starts:
    name = s.group(1)
    # 找在 s.end() 之后的第一个 Time:
    t = next((m for m in times if m.start() > s.end()), None)
    if t:
        results.append((name, float(t.group(1))))

for name, ms in results:
    print(f"{name}\t{ms:.3f}")
PYEOF
}

# 获取查询前后的 pg_stat_database 增量（blks_hit, blks_read）
get_blk_stats_before() {
    local db=$1
    sudo -u "$PGUSER" psql -d "$db" -At -c "
        SELECT blks_hit, blks_read
        FROM pg_stat_database WHERE datname='$db';" 2>/dev/null
}

# ── 主循环 ────────────────────────────────────────────────────────────────

ORIGINAL_BUFFERS=$(sudo -u "$PGUSER" psql -At -c "SHOW shared_buffers;" 2>/dev/null | sed 's/MB//')

for BUF_MB in $BUFFER_SIZES; do
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo " shared_buffers = ${BUF_MB}MB"
    echo "════════════════════════════════════════════════════════════════"

    set_shared_buffers "$BUF_MB"

    for WORKLOAD in tpch tpcc; do
        case "$WORKLOAD" in
            tpch) DB=tpch; SQL_FILE="$TPCH_SQL" ;;
            tpcc) DB=tpcc; SQL_FILE="$TPCC_SQL"  ;;
        esac

        echo ""
        echo "[exp4] Workload: $WORKLOAD (db=$DB)"

        for RUN in $(seq 1 "$REPEATS"); do
            echo "[exp4]   Run $RUN/$REPEATS..."

            # 第一次 run 前清 cache（cold start），后续 run 为 warm cache
            if [ "$RUN" -eq 1 ]; then
                drop_caches
                RUN_TYPE="cold"
            else
                RUN_TYPE="warm"
            fi

            # 重置 pg_stat_database 统计（只重置当前 db）
            sudo -u "$PGUSER" psql -d "$DB" -c "SELECT pg_stat_reset();" 2>/dev/null

            # 运行查询
            RAW_OUT="$OUTDIR/raw_${WORKLOAD}_buf${BUF_MB}_run${RUN}.txt"
            run_and_parse "$DB" "$SQL_FILE" "$RAW_OUT"

            # 读取 blk 统计
            BLK_STATS=$(sudo -u "$PGUSER" psql -d "$DB" -At -c "
                SELECT blks_hit, blks_read,
                       round(100.0 * blks_read / nullif(blks_hit + blks_read, 0), 4)
                FROM pg_stat_database WHERE datname='$DB';" 2>/dev/null)
            BLKS_HIT=$(echo "$BLK_STATS"  | cut -d'|' -f1)
            BLKS_READ=$(echo "$BLK_STATS" | cut -d'|' -f2)
            MISS_PCT=$(echo "$BLK_STATS"  | cut -d'|' -f3)

            # 解析每条查询的耗时，写入 CSV
            while IFS=$'\t' read -r QNAME ELAPSED_MS; do
                echo "${BUF_MB},${WORKLOAD},${QNAME},${RUN},${ELAPSED_MS},${BLKS_HIT},${BLKS_READ},${MISS_PCT}" \
                    >> "$TIMINGS_CSV"
                printf "    %-25s %8.1f ms  (run=%s type=%s)\n" \
                    "$QNAME" "$ELAPSED_MS" "$RUN" "$RUN_TYPE"
            done < <(parse_timings "$RAW_OUT")
        done
    done
done

# ── 恢复原始 shared_buffers ───────────────────────────────────────────────
echo ""
echo "[exp4] Restoring original shared_buffers=${ORIGINAL_BUFFERS}MB..."
set_shared_buffers "$ORIGINAL_BUFFERS"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo " Experiment complete."
echo " Results: $TIMINGS_CSV"
echo " Run: python3 analyze_mem_benchmark.py $OUTDIR"
echo "════════════════════════════════════════════════════════════════"
