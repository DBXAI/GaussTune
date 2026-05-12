#!/bin/bash
# Wait for run 7 (stmm_test.py) to finish, then run TLB benchmark
echo "[$(date +%H:%M:%S)] Waiting for run 7 (PID 12772) to finish..."
while kill -0 12772 2>/dev/null; do
    sleep 10
done
echo "[$(date +%H:%M:%S)] Run 7 done. Starting TLB benchmark in 30s..."
sleep 30
echo "[$(date +%H:%M:%S)] Launching tlb_bench.py"
python3 /home/node/GaussTune/tlb_bench.py 2>&1 | tee /home/node/GaussTune/run-logs/tlb_bench.log
