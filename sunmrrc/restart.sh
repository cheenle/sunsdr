#!/bin/bash
# sunmrrc — 重启服务脚本
# ========================
# 精确杀掉本目录下的 server.py（按工作目录匹配，不误伤 web_control 的同名进程），
# 然后后台重启，日志写到 sunmrrc/server.log。
#
# 用法:
#   ./restart.sh            # 默认端口 8889
#   WEB_PORT=8889 ./restart.sh
#   ./restart.sh -f         # 前台运行（Ctrl-C 退出，便于看实时日志）

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

WEB_PORT="${WEB_PORT:-8889}"
LOG_FILE="$SCRIPT_DIR/server.log"
FOREGROUND=0
[ "${1:-}" = "-f" ] && FOREGROUND=1

# ── 1. 找出并杀掉本目录运行的 server.py ──────────────────────────
# 用 lsof 按 cwd 精确定位（避免误杀 web_control/server.py）。
echo "→ 查找运行中的 sunmrrc server.py ..."
OLD_PIDS=""
for pid in $(pgrep -f "python3? .*server\.py" 2>/dev/null); do
    cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | cut -c2-)
    if [ "$cwd" = "$SCRIPT_DIR" ]; then
        OLD_PIDS="$OLD_PIDS $pid"
    fi
done

if [ -n "$OLD_PIDS" ]; then
    echo "→ 终止旧进程:$OLD_PIDS"
    kill $OLD_PIDS 2>/dev/null
    # 等待优雅退出，最多 5 秒
    for _ in 1 2 3 4 5; do
        still=""
        for pid in $OLD_PIDS; do
            kill -0 "$pid" 2>/dev/null && still="$still $pid"
        done
        [ -z "$still" ] && break
        sleep 1
    done
    # 仍未退出则强杀
    for pid in $OLD_PIDS; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "→ 进程 $pid 未响应,强制结束 (kill -9)"
            kill -9 "$pid" 2>/dev/null
        fi
    done
else
    echo "→ 没有运行中的旧进程"
fi

# ── 2. 释放端口（兜底，只杀 LISTEN 的服务端，不误伤客户端连接）──
# 注意必须加 -sTCP:LISTEN：否则 lsof 会把浏览器等"连到本端口的客户端"
# 进程也列出来一起杀掉（曾误杀 Chrome 网络进程）。
PORT_PID=$(lsof -ti tcp:"$WEB_PORT" -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$PORT_PID" ]; then
    echo "→ 端口 $WEB_PORT 仍被监听占用 (pid $PORT_PID),清理中"
    kill -9 $PORT_PID 2>/dev/null || true
    sleep 1
fi

# ── 3. 启动 ─────────────────────────────────────────────────────
source ../venv/bin/activate 2>/dev/null || true
export BACKEND="${BACKEND:-direct}"
export WEB_PORT
export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"

if [ "$FOREGROUND" = "1" ]; then
    echo "→ 前台启动 sunmrrc → http://localhost:$WEB_PORT (Ctrl-C 退出)"
    echo "   WEB_PASSWORD=${WEB_PASSWORD:-(default: sunmrrc)}"
    exec python3 server.py
else
    echo "→ 后台启动 sunmrrc → http://localhost:$WEB_PORT"
    echo "   WEB_PASSWORD=${WEB_PASSWORD:-(default: sunmrrc)}"
    nohup python3 -u server.py > "$LOG_FILE" 2>&1 < /dev/null &
    NEW_PID=$!
    disown "$NEW_PID" 2>/dev/null || true
    sleep 2
    if kill -0 "$NEW_PID" 2>/dev/null; then
        echo "✓ 已启动 (pid $NEW_PID),日志: $LOG_FILE"
        echo "  实时查看: tail -f $LOG_FILE"
    else
        echo "✗ 启动失败,最近日志:"
        tail -n 20 "$LOG_FILE"
        exit 1
    fi
fi
