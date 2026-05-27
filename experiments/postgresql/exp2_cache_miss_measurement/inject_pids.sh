#!/usr/bin/env bash
# inject_pids.bt
# 辅助脚本：将当前 tpcc/tpch 进程 PID 注入到 bpftrace 的 @tp_pids/@ap_pids map
# 在 bpftrace_cachemiss.bt 运行期间，另开终端执行此脚本
#
# 用法：bash inject_pids.sh

PGUSER=postgres

echo "Querying active PostgreSQL backends..."
sudo -u "$PGUSER" psql -At -c "
    SELECT pid, application_name
    FROM pg_stat_activity
    WHERE application_name IN ('tpcc','tpch')
    ORDER BY application_name, pid;" 2>/dev/null

echo ""
echo "To inject into bpftrace maps, use bpftrace's map assignment:"
echo "  For each tpcc PID: bpftrace -e '@tp_pids[<pid>] = 1'"
echo "  For each tpch PID: bpftrace -e '@ap_pids[<pid>] = 1'"
echo ""
echo "Or use the combined approach below (requires bpftrace >= 0.12):"

# 生成 bpftrace 初始化片段
TP_PIDS=$(sudo -u "$PGUSER" psql -At -c "
    SELECT pid FROM pg_stat_activity
    WHERE application_name='tpcc';" 2>/dev/null | tr '\n' ' ')
AP_PIDS=$(sudo -u "$PGUSER" psql -At -c "
    SELECT pid FROM pg_stat_activity
    WHERE application_name='tpch';" 2>/dev/null | tr '\n' ' ')

echo ""
echo "TP PIDs (tpcc): $TP_PIDS"
echo "AP PIDs (tpch): $AP_PIDS"

# 写入 pid 文件供 analyze_cachemiss.py 使用
echo "$TP_PIDS" > /tmp/tp_pids.txt
echo "$AP_PIDS" > /tmp/ap_pids.txt
echo "Written to /tmp/tp_pids.txt and /tmp/ap_pids.txt"
