#!/bin/bash
# Watchdog: relaunches stmm_test.py if it's not running.
# Run via cron: */5 * * * * /home/node/GaussTune/stmm_watchdog.sh >> /home/node/GaussTune/run-logs/stmm_watchdog.log 2>&1

cd /home/node/GaussTune

RUNDIR=/home/node/GaussTune/run-logs
mkdir -p "$RUNDIR"

PID_FILE=$RUNDIR/stmm_test.pid
CURRENT_LOG=$RUNDIR/stmm_current.log

# Check if already running
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "[$(date '+%H:%M:%S')] stmm_test running (PID=$PID)"
        exit 0
    fi
fi

# Also check by process name
if pgrep -f "python3 stmm_test.py" > /dev/null 2>&1; then
    echo "[$(date '+%H:%M:%S')] stmm_test running (by pgrep)"
    exit 0
fi

# Not running — check if previous run completed successfully
if [ -f "$CURRENT_LOG" ] && grep -q "All workloads complete\|^Saved:" "$CURRENT_LOG" 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] Previous run completed successfully — not relaunching"
    exit 0
fi

# Relaunch
MAX_NUM=$(ls "$RUNDIR"/stmm_run*.log 2>/dev/null | grep -oP '(?<=stmm_run)\d+' | sort -n | tail -1)
MAX_NUM=${MAX_NUM:-0}
NEW_LOG="$RUNDIR/stmm_run$((MAX_NUM+1)).log"
echo "[$(date '+%H:%M:%S')] stmm_test not running — relaunching → $NEW_LOG"
nohup python3 stmm_test.py > "$NEW_LOG" 2>&1 &
LAUNCHED_PID=$!
echo $LAUNCHED_PID > "$PID_FILE"
ln -sf "$NEW_LOG" "$CURRENT_LOG"
echo "[$(date '+%H:%M:%S')] Launched PID=$LAUNCHED_PID"
