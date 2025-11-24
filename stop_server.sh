#!/usr/bin/env bash
# Stop any running uvicorn instances

echo "Stopping uvicorn processes..."
pkill -f uvicorn
sleep 1

# Also kill any python processes on port 8000
if ss -tlnp | grep -q :8000; then
    PID=$(ss -tlnp | grep :8000 | grep -oP 'pid=\K[0-9]+' | head -1)
    if [ -n "$PID" ]; then
        echo "Killing process $PID on port 8000..."
        kill -9 "$PID" 2>/dev/null
    fi
fi

sleep 1
if pgrep -f uvicorn > /dev/null; then
    echo "⚠ Some processes may still be running"
else
    echo "✓ All uvicorn processes stopped"
fi

