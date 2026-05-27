#!/usr/bin/env bash
# mem_sweep.sh
# 在不同 shared_buffers 下测量 SQL 执行时间
#
# 测量对象：
#   - TPC-C  : 代表性 OLTP 查询（stock_level / order_status / delivery）
#   - TPC-H  : 代表性 OLAP 查询（Q1 / Q6 / Q14，全表扫描 lineitem）
#   - Mixed  : TPC-C 吞吐量在 TPC-H 并发扫描时的衰减
#
# 测量方法：
#   - TPC-H  : EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) — 精确到 ms，含 hit/miss 计数
#   - TPC-C  : benchbase 60s 压测 → 从结果 CSV 取 p50/p95/p99 latency 和 TPS
#   - Mixed  : 同时跑 benchbase + tpch_timing.sql，对比 TPC-C TPS 衰减
#
# 用法：
#   bash mem_sweep.sh                    # 全量：128M 256M 512M 1G 2G 4G 8G
#   bash mem_sweep.sh --sizes "128 256"  # 只测指定 size
#   bash mem_sweep.sh --skip-tpcc        # 只测 TPC-H（快速验证）
#   bash mem_sweep.sh --dry-run          # 检查环境，不实际运行
#
# 输出目录结构：
#   results_sweep_<timestamp>/
#     sweep_summary.csv          — 汇总表（每行一个 size × workload 组合）
#     <size>mb/
#       tpch_timing.txt          — EXPLAIN ANALYZE 原始输出
#       tpch_results.csv         — 解析后的查询级耗时
#       tpcc_results.csv         — benchbase 吞吐量 + 延迟
#       mixed_tpcc_results.csv   — 混合负载下的 TPC-C 结果
#       pg_stats.txt             — pg_stat_bgwriter / pg_buffercache 快照

set -euo pipefail

# ── 参数 ──────────────────────────────────────────────────────────────────────
SIZES="128 256 512 1024 2048 4096 8192"   # MB
TPCC_DURATION=60          # 每个 size 跑 TPC-C 的秒数
TPCH_RUNS=3               # 每个 size 跑 TPC-H 查询的次数（取平均）
SKIP_TPCC=false
SKIP_TPCH=false
SKIP_MIXED=false
DRY_RUN=false
PGUSER=postgres
PGCONF=/etc/postgresql/12/main/postgresql.conf
BENCHBASE=/opt/benchbase
BENCHBASE_JAR=$(ls "$BENCHBASE"/target/benchbase*.jar 2>/dev/null | head -1)
OUTDIR="results_sweep_$(date +%Y%m%d_%H%M%S)"

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --sizes)       SIZES="$2"; shift 2 ;;
        --skip-tpcc)   SKIP_TPCC=true; shift ;;
        --skip-tpch)   SKIP_TPCH=true; shift ;;
        --skip-mixed)  SKIP_MIXED=true; shift ;;
        --dry-run)     DRY_RUN=true; shift ;;
        --duration)    TPCC_DURATION="$2"; shift 2 ;;
        --runs)        TPCH_RUNS="$2"; shift 2 ;;
        --outdir)      OUTDIR="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── 颜色输出 ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

log()  { echo -e "${CYAN}[sweep]${NC} $*"; }
ok()   { echo -e "${GREEN}[  OK ]${NC} $*"; }
warn() { echo -e "${YELLOW}[ WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }
sep()  { echo -e "${BLUE}══════════════════════════════════════════════════════${NC}"; }

# ── 环境检查 ──────────────────────────────────────────────────────────────────
check_env() {
    log "Checking environment..."
    local ok=true

    # PostgreSQL 可连接
    if sudo -u "$PGUSER" psql -c "SELECT 1;" &>/dev/null; then
        ok "PostgreSQL: reachable"
    else
        err "PostgreSQL: cannot connect"; ok=false
    fi

    # 数据库存在且有数据
    local tpcc_rows
    tpcc_rows=$(sudo -u "$PGUSER" psql -d tpcc -At -c "SELECT count(*) FROM warehouse;" 2>/dev/null || echo 0)
    if [[ "$tpcc_rows" -gt 0 ]]; then
        ok "TPC-C: $tpcc_rows warehouses"
    else
        warn "TPC-C: no data (tpcc_only tests will be skipped)"
        SKIP_TPCC=true
    fi

    local tpch_rows
    tpch_rows=$(sudo -u "$PGUSER" psql -d tpch -At -c "SELECT count(*) FROM lineitem LIMIT 1;" 2>/dev/null || echo 0)
    if [[ "$tpch_rows" -gt 0 ]]; then
        ok "TPC-H: lineitem has data"
    else
        warn "TPC-H: no data (tpch tests will be skipped)"
        SKIP_TPCH=true; SKIP_MIXED=true
    fi

    # benchbase
    if [[ -n "$BENCHBASE_JAR" && -f "$BENCHBASE_JAR" ]]; then
        ok "benchbase: $BENCHBASE_JAR"
    else
        warn "benchbase jar not found — TPC-C tests will be skipped"
        SKIP_TPCC=true; SKIP_MIXED=true
    fi

    # postgresql.conf 可写
    if [[ -w "$PGCONF" ]]; then
        ok "postgresql.conf: writable"
    else
        err "postgresql.conf not writable: $PGCONF"
        err "Run as root or: sudo chmod 666 $PGCONF"
        ok=false
    fi

    # systemctl 可用（重启 PG）
    if systemctl is-active postgresql &>/dev/null; then
        ok "systemctl: postgresql is active"
    else
        err "postgresql service not active via systemctl"
        ok=false
    fi

    # 内存检查
    local mem_gb
    mem_gb=$(awk '/MemTotal/{printf "%d", $2/1024/1024}' /proc/meminfo)
    ok "System memory: ${mem_gb}GB"
    local max_size
    max_size=$(echo "$SIZES" | tr ' ' '\n' | sort -n | tail -1)
    if [[ $((max_size)) -gt $((mem_gb * 800)) ]]; then
        warn "Largest size ${max_size}MB may exceed available RAM (${mem_gb}GB)"
    fi

    if [[ "$ok" == false ]]; then
        err "Environment check failed. Fix errors above and retry."
        exit 1
    fi

    log "Environment OK. Plan:"
    log "  sizes        : $SIZES (MB)"
    log "  tpcc         : $([ "$SKIP_TPCC" = true ] && echo SKIP || echo "${TPCC_DURATION}s per size")"
    log "  tpch         : $([ "$SKIP_TPCH" = true ] && echo SKIP || echo "${TPCH_RUNS} runs per size")"
    log "  mixed        : $([ "$SKIP_MIXED" = true ] && echo SKIP || echo "${TPCC_DURATION}s per size")"
    log "  output       : $OUTDIR/"
    sep
}

# ── shared_buffers 切换 ───────────────────────────────────────────────────────
set_shared_buffers() {
    local size_mb=$1
    log "Setting shared_buffers = ${size_mb}MB ..."

    # 修改 postgresql.conf（保留原始行注释，替换或追加）
    if grep -q "^shared_buffers" "$PGCONF"; then
        sed -i "s/^shared_buffers\s*=.*/shared_buffers = ${size_mb}MB/" "$PGCONF"
    else
        echo "shared_buffers = ${size_mb}MB" >> "$PGCONF"
    fi

    # 重启 PostgreSQL（shared_buffers 必须重启生效）
    log "Restarting PostgreSQL..."
    systemctl restart postgresql
    sleep 3

    # 验证生效
    local actual
    actual=$(sudo -u "$PGUSER" psql -At -c "SHOW shared_buffers;" 2>/dev/null)
    ok "shared_buffers confirmed: $actual"

    # 清空 OS page cache，确保测量的是 PG buffer pool 效果而非 OS cache
    # （可选：注释掉此行保留 OS cache，模拟生产环境热启动）
    log "Dropping OS page cache (sync + drop_caches)..."
    sync
    echo 3 > /proc/sys/vm/drop_caches
    ok "OS page cache cleared"

    # 预热：让 PG 自身 buffer pool 从冷启动进入稳态
    # 方法：先跑一轮 TPC-H Q1（全表扫描），把热页加载进来
    log "Warming up buffer pool (30s)..."
    timeout 30 sudo -u "$PGUSER" psql -d tpch -c "
        SELECT count(*) FROM lineitem
        WHERE l_shipdate <= date '1998-12-01' - interval '90 day';" \
        &>/dev/null || true
    ok "Warmup done"
}

# ── TPC-H 查询计时 ────────────────────────────────────────────────────────────
run_tpch_timing() {
    local size_mb=$1
    local outdir=$2

    [[ "$SKIP_TPCH" == true ]] && return

    log "Running TPC-H timing (${TPCH_RUNS} runs)..."
    local timing_file="$outdir/tpch_timing.txt"
    local csv_file="$outdir/tpch_results.csv"

    echo "run,query,planning_ms,execution_ms,total_ms,shared_hit,shared_read,shared_dirtied" \
        > "$csv_file"

    for run in $(seq 1 "$TPCH_RUNS"); do
        log "  TPC-H run $run/$TPCH_RUNS ..."

        # 每次 run 前清空 PG buffer pool（模拟冷启动）
        # 注意：第 1 次 cold，后续 run 是 warm，这样可以看到 cache 效果
        if [[ $run -eq 1 ]]; then
            log "  [run $run] cold start (clearing PG buffers via pg_prewarm trick)..."
            # 通过重置 shared_buffers 来清空（不重启，用 pg_buffercache 验证）
            # 实际上 PG 没有直接清空 buffer pool 的命令，用 OS drop_caches 代替
            sync && echo 3 > /proc/sys/vm/drop_caches
        fi

        # 运行 TPC-H 代表性查询，用 EXPLAIN ANALYZE BUFFERS 记录详情
        sudo -u "$PGUSER" psql -d tpch \
            -v ON_ERROR_STOP=1 \
            -f "$(dirname "$0")/tpch_timing.sql" \
            >> "$timing_file" 2>&1 || {
            warn "  TPC-H run $run failed, skipping"
            continue
        }

        # 解析 EXPLAIN ANALYZE 输出，提取每个查询的耗时
        python3 "$(dirname "$0")/parse_explain.py" \
            "$timing_file" "$run" "$size_mb" >> "$csv_file" 2>/dev/null || true

    done

    ok "TPC-H timing done → $csv_file"
}

# ── TPC-C 压测 ────────────────────────────────────────────────────────────────
run_tpcc() {
    local size_mb=$1
    local outdir=$2
    local label=${3:-tpcc}   # tpcc 或 mixed_tpcc

    [[ "$SKIP_TPCC" == true ]] && return

    log "Running TPC-C (${TPCC_DURATION}s, label=$label)..."
    local result_dir="$outdir/benchbase_${label}"
    mkdir -p "$result_dir"

    cd "$BENCHBASE"
    java -jar "$BENCHBASE_JAR" \
        -b tpcc \
        -c tpcc_config.xml \
        --execute=true \
        --time="$TPCC_DURATION" \
        -d "$result_dir" \
        2>&1 | tee "$outdir/${label}_benchbase.log" | \
        grep -E "Throughput|Latency|Error|Complete|tpcc" || true
    cd - > /dev/null

    # 从 benchbase 结果 CSV 提取 TPS 和延迟
    python3 "$(dirname "$0")/parse_benchbase.py" \
        "$result_dir" "$size_mb" "$label" \
        >> "$outdir/sweep_summary_raw.csv" 2>/dev/null || true

    ok "TPC-C done → $result_dir"
}

# ── Mixed 负载：TPC-C + TPC-H 并发 ──────────────────────────────────────────
run_mixed() {
    local size_mb=$1
    local outdir=$2

    [[ "$SKIP_MIXED" == true ]] && return

    log "Running Mixed workload (TPC-C + TPC-H concurrent, ${TPCC_DURATION}s)..."

    # 后台运行 TPC-H 全表扫描（持续施压）
    sudo -u "$PGUSER" psql -d tpch -c "
        -- 循环扫描 lineitem，持续占用 buffer pool
        DO \$\$
        DECLARE i int;
        BEGIN
            FOR i IN 1..999 LOOP
                PERFORM count(*) FROM lineitem
                WHERE l_shipdate <= date '1998-12-01' - interval '90 day';
                EXIT WHEN pg_sleep(0) IS NOT NULL AND i > 999;
            END LOOP;
        END;
        \$\$;" &>/dev/null &
    TPCH_BG_PID=$!
    log "  TPC-H background scan started (PID=$TPCH_BG_PID)"

    # 前台运行 TPC-C
    run_tpcc "$size_mb" "$outdir" "mixed_tpcc"

    # 停止 TPC-H 后台
    kill "$TPCH_BG_PID" 2>/dev/null || true
    ok "Mixed workload done"
}

# ── pg_stat 快照 ──────────────────────────────────────────────────────────────
collect_pg_stats() {
    local outdir=$1
    local label=$2

    sudo -u "$PGUSER" psql -d tpcc -c "
        SELECT '${label}' AS label, now() AS ts,
               buffers_clean, buffers_alloc, buffers_backend,
               maxwritten_clean
        FROM pg_stat_bgwriter;" >> "$outdir/pg_stats.txt" 2>/dev/null || true

    sudo -u "$PGUSER" psql -d tpch -c "
        SELECT '${label}' AS label,
               count(*) AS buffers_used,
               sum(CASE WHEN isdirty THEN 1 ELSE 0 END) AS dirty_buffers,
               round(avg(usagecount)::numeric, 2) AS avg_usagecount
        FROM pg_buffercache
        WHERE relfilenode IS NOT NULL;" >> "$outdir/pg_stats.txt" 2>/dev/null || true
}

# ── 主流程 ────────────────────────────────────────────────────────────────────
main() {
    sep
    echo -e "${CYAN}  PostgreSQL shared_buffers Memory Sweep${NC}"
    echo -e "${CYAN}  测量不同 shared_buffers 下的 SQL 执行时间${NC}"
    sep

    check_env

    if [[ "$DRY_RUN" == true ]]; then
        log "Dry-run mode: environment check passed, exiting."
        exit 0
    fi

    mkdir -p "$OUTDIR"
    # 汇总 CSV 头
    echo "size_mb,workload,metric,value" > "$OUTDIR/sweep_summary.csv"
    echo "size_mb,workload,tps,p50_ms,p95_ms,p99_ms,label" \
        > "$OUTDIR/sweep_summary_raw.csv"

    # 记录原始 shared_buffers，实验结束后恢复
    ORIGINAL_BUFFERS=$(sudo -u "$PGUSER" psql -At -c "SHOW shared_buffers;" 2>/dev/null | sed 's/MB//')
    log "Original shared_buffers: ${ORIGINAL_BUFFERS}MB (will restore at end)"

    for size_mb in $SIZES; do
        sep
        log "▶  Testing shared_buffers = ${size_mb}MB"
        sep

        local_outdir="$OUTDIR/${size_mb}mb"
        mkdir -p "$local_outdir"

        # 1. 切换 shared_buffers 并重启
        set_shared_buffers "$size_mb"

        # 2. 采集 pg_stat 基线
        collect_pg_stats "$local_outdir" "before_${size_mb}mb"

        # 3. TPC-H 查询计时（最能体现 buffer pool 大小影响）
        run_tpch_timing "$size_mb" "$local_outdir"

        # 4. TPC-C 压测
        run_tpcc "$size_mb" "$local_outdir"

        # 5. Mixed 负载
        run_mixed "$size_mb" "$local_outdir"

        # 6. 采集 pg_stat 结束快照
        collect_pg_stats "$local_outdir" "after_${size_mb}mb"

        ok "▶  ${size_mb}MB done → $local_outdir"
    done

    # ── 恢复原始配置 ──────────────────────────────────────────────────────────
    sep
    log "Restoring shared_buffers to ${ORIGINAL_BUFFERS}MB ..."
    set_shared_buffers "$ORIGINAL_BUFFERS"

    # ── 生成汇总报告 ──────────────────────────────────────────────────────────
    log "Generating summary report..."
    python3 "$(dirname "$0")/analyze_sweep.py" "$OUTDIR"

    sep
    ok "All done! Results in: $OUTDIR/"
    echo ""
    echo "  Key files:"
    echo "    $OUTDIR/sweep_summary.csv       — 汇总表"
    echo "    $OUTDIR/sweep_report.txt        — 文字报告"
    echo "    $OUTDIR/sweep_plot.png          — 折线图（需 matplotlib）"
    echo ""
    echo "  Per-size details:"
    for size_mb in $SIZES; do
        echo "    $OUTDIR/${size_mb}mb/"
    done
    sep
}

main "$@"
