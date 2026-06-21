#!/bin/bash
cd "$(dirname "$0")"
source ../venv/bin/activate 2>/dev/null
export BACKEND=direct
export WEB_PORT="${WEB_PORT:-8081}"
echo "sunmrrc → http://localhost:$WEB_PORT"
exec python3 server.py
