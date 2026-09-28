#!/usr/bin/env bash
# server/stop.sh — 停止"PickOne"后端
set -u

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SERVER_DIR")"
PIDFILE="$ROOT/data/server.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "未找到 $PIDFILE，服务可能未运行"
    exit 0
fi

PID="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -z "${PID:-}" ]; then
    echo "PID 文件为空，清理"
    rm -f "$PIDFILE"
    exit 0
fi

if ! kill -0 "$PID" 2>/dev/null; then
    echo "进程 $PID 不存在，清理 PID 文件"
    rm -f "$PIDFILE"
    exit 0
fi

kill "$PID" 2>/dev/null
for i in $(seq 1 20); do
    if ! kill -0 "$PID" 2>/dev/null; then
        rm -f "$PIDFILE"
        echo "已停止 (pid=$PID)"
        exit 0
    fi
    sleep 0.3
done

echo "优雅停止超时，强制 kill -9 $PID"
kill -9 "$PID" 2>/dev/null
sleep 0.5
rm -f "$PIDFILE"
echo "已强制停止 (pid=$PID)"
