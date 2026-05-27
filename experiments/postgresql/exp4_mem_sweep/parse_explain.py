#!/usr/bin/env python3
"""
parse_explain.py — 解析 EXPLAIN (ANALYZE, BUFFERS) 输出，提取每个查询的耗时和 buffer 统计

输入：tpch_timing.txt（psql 运行 tpch_timing.sql 的原始输出）
输出：追加到 tpch_results.csv

CSV 列：
    size_mb, run, query, planning_ms, execution_ms, total_ms,
    shared_hit, shared_read, shared_dirtied, rows

用法：
    python3 parse_explain.py <timing_txt> <run_number> <size_mb> >> tpch_results.csv
"""

import sys
import re

def parse_explain_output(text):
    """
    从 EXPLAIN ANALYZE BUFFERS 输出中提取关键指标。
    返回 list of dict，每个 dict 对应一个查询块。
    """
    results = []

    # 按查询分割（以 "-- QUERY:" 注释行为分隔符）
    query_blocks = re.split(r'--\s*QUERY:\s*', text)

    for block in query_blocks:
        if not block.strip():
            continue

        # 提取查询名（第一行）
        lines = block.strip().split('\n')
        query_name_match = re.match(r'(\w+)', lines[0])
        query_name = query_name_match.group(1) if query_name_match else 'UNKNOWN'

        # Planning Time
        planning_ms = 0.0
        m = re.search(r'Planning Time:\s*([\d.]+)\s*ms', block)
        if m:
            planning_ms = float(m.group(1))

        # Execution Time
        execution_ms = 0.0
        m = re.search(r'Execution Time:\s*([\d.]+)\s*ms', block)
        if m:
            execution_ms = float(m.group(1))

        # Buffers: shared hit=X read=Y dirtied=Z
        shared_hit = 0
        shared_read = 0
        shared_dirtied = 0

        # 汇总所有 Buffers 行（可能有多个节点各自的 Buffers 行）
        for buf_line in re.finditer(
            r'Buffers:\s*shared\s+hit=(\d+)(?:\s+read=(\d+))?(?:\s+dirtied=(\d+))?',
            block
        ):
            shared_hit     += int(buf_line.group(1) or 0)
            shared_read    += int(buf_line.group(2) or 0)
            shared_dirtied += int(buf_line.group(3) or 0)

        # Rows（顶层节点的 actual rows）
        rows = 0
        m = re.search(r'actual time=[\d.]+\.\.[\d.]+ rows=(\d+)', block)
        if m:
            rows = int(m.group(1))

        if execution_ms > 0 or planning_ms > 0:
            results.append({
                'query':          query_name,
                'planning_ms':    round(planning_ms, 3),
                'execution_ms':   round(execution_ms, 3),
                'total_ms':       round(planning_ms + execution_ms, 3),
                'shared_hit':     shared_hit,
                'shared_read':    shared_read,
                'shared_dirtied': shared_dirtied,
                'rows':           rows,
            })

    return results


def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <timing_txt> <run_number> <size_mb>",
              file=sys.stderr)
        sys.exit(1)

    timing_file = sys.argv[1]
    run_number  = sys.argv[2]
    size_mb     = sys.argv[3]

    try:
        with open(timing_file) as f:
            text = f.read()
    except FileNotFoundError:
        print(f"File not found: {timing_file}", file=sys.stderr)
        sys.exit(1)

    results = parse_explain_output(text)

    if not results:
        print(f"Warning: no EXPLAIN ANALYZE output found in {timing_file}",
              file=sys.stderr)
        sys.exit(0)

    for r in results:
        print(
            f"{size_mb},{run_number},{r['query']},"
            f"{r['planning_ms']},{r['execution_ms']},{r['total_ms']},"
            f"{r['shared_hit']},{r['shared_read']},{r['shared_dirtied']},"
            f"{r['rows']}"
        )


if __name__ == '__main__':
    main()
