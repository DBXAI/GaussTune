#!/usr/bin/env python3
"""
validate_sbpx.py — 验证 SBPX 预测准确性

方法：
  1. 用小 shared_buffers（如 64MB）运行工作负载，记录实际 miss 率
  2. 用大 shared_buffers（如 256MB）运行同样工作负载，记录实际 miss 率
  3. 用 SBPX 从 64MB trace 预测 256MB 下的 miss 率
  4. 对比预测值 vs 实际值

用法：
  # Step 1: 在 64MB 下采集 trace 并记录实际 miss
  sudo -u postgres psql -c "ALTER SYSTEM SET shared_buffers='64MB'; SELECT pg_reload_conf();"
  bash collect_trace.sh tpcc 60 > trace_64mb.csv
  sudo -u postgres psql -d tpcc -c "SELECT * FROM v_db_cachemiss;" > actual_64mb.txt

  # Step 2: 在 256MB 下记录实际 miss（不需要 trace）
  sudo -u postgres psql -c "ALTER SYSTEM SET shared_buffers='256MB'; SELECT pg_reload_conf();"
  # 重启 PG 使 shared_buffers 生效
  sudo systemctl restart postgresql
  # 运行同样工作负载
  sudo -u postgres psql -d tpcc -c "SELECT * FROM v_db_cachemiss;" > actual_256mb.txt

  # Step 3: 验证
  python3 validate_sbpx.py trace_64mb.csv actual_64mb.txt actual_256mb.txt

注意：改变 shared_buffers 需要重启 PostgreSQL（不是 reload）
"""

import sys
import re
from pathlib import Path


def parse_miss_rate(txt_file):
    """从 v_db_cachemiss 输出中提取 miss_rate_pct"""
    with open(txt_file) as f:
        content = f.read()
    # 查找 miss_rate_pct 列的值
    m = re.search(r'(\d+\.\d+)', content)
    return float(m.group(1)) if m else None


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    trace_file   = sys.argv[1]
    actual_small = sys.argv[2]
    actual_large = sys.argv[3]

    # 从文件名推断 buffer size
    small_mb = 64
    large_mb = 256

    # 运行 SBPX 预测
    import subprocess
    result = subprocess.run(
        [sys.executable, 'sbpx_mrc.py', trace_file,
         '--current-buffers', str(small_mb),
         '--disk-latency-us', '1'],  # 只要 miss rate，不关心时间
        capture_output=True, text=True
    )

    # 从输出中提取预测的 miss rate for large_mb
    predicted_rate = None
    for line in result.stdout.split('\n'):
        if f'{large_mb} MB' in line:
            parts = line.split()
            for p in parts:
                try:
                    v = float(p.rstrip('%'))
                    if 0 <= v <= 100:
                        predicted_rate = v / 100
                        break
                except ValueError:
                    pass

    actual_small_rate = parse_miss_rate(actual_small)
    actual_large_rate = parse_miss_rate(actual_large)

    print(f"\n=== SBPX Validation ===")
    print(f"  Actual miss rate @ {small_mb}MB  : {actual_small_rate:.4f}%")
    print(f"  Actual miss rate @ {large_mb}MB  : {actual_large_rate:.4f}%")
    print(f"  SBPX predicted  @ {large_mb}MB  : {predicted_rate*100:.4f}%"
          if predicted_rate else "  SBPX prediction: N/A")

    if predicted_rate and actual_large_rate:
        error = abs(predicted_rate * 100 - actual_large_rate) / max(actual_large_rate, 0.001) * 100
        print(f"  Prediction error              : {error:.1f}%")
        if error < 10:
            print(f"  Result: GOOD (error < 10%)")
        elif error < 25:
            print(f"  Result: ACCEPTABLE (error < 25%)")
        else:
            print(f"  Result: POOR — consider increasing trace duration or sample rate")


if __name__ == '__main__':
    main()
