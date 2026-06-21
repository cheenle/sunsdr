#!/bin/bash
# SunSDR2 DX — Web Control restart script
# Kills old process by port, frees device ports, starts fresh.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON=/Users/cheenle/HAM/sunsdr/py311_env/bin/python
WEB_PORT="${WEB_PORT:-8080}"

echo "=== SunSDR2 DX Web Control ==="

# Kill existing server
echo -n "Stopping old server... "
lsof -ti:$WEB_PORT 2>/dev/null | xargs kill -9 2>/dev/null && echo "killed" || echo "none"

# Free device ports if held by stale Python
for port in 50001 50002; do
    lsof -iUDP:$port -t 2>/dev/null | while read pid; do
        if ps -p $pid -o command= 2>/dev/null | grep -q python; then
            kill -9 $pid 2>/dev/null
        fi
    done
done

sleep 1

# Start
echo -n "Starting on :$WEB_PORT... "
nohup $PYTHON server.py > /tmp/sunsdr_web.log 2>&1 &
sleep 4

# Verify
if curl -s -o /dev/null -w "%{http_code}" http://localhost:$WEB_PORT/ | grep -q 200; then
    echo "OK"
    echo "  http://localhost:$WEB_PORT"
else
    echo "FAIL — check /tmp/sunsdr_web.log"
    exit 1
fi
