#!/usr/bin/env bash
# server/run.sh — 后台常驻启动"PickOne"后端
#   日志: data/server.log   PID: data/server.pid
set -u

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SERVER_DIR")"
PY="/home/Developer/workspace/.venv/bin/python"
DATA="$ROOT/data"
LOG="$DATA/server.log"
PIDFILE="$DATA/server.pid"

mkdir -p "$DATA"

# 已在运行则不重复启动
if [ -f "$PIDFILE" ]; then
    OLD_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "服务已在运行 (pid=$OLD_PID)，如需重启请先 bash server/stop.sh"
        exit 0
    fi
    rm -f "$PIDFILE"
fi

cd "$ROOT"
export PYTHONUNBUFFERED=1
# 默认监听 0.0.0.0:8888（可用 PICKONE_HOST / PICKONE_PORT 覆盖）
export PICKONE_HOST="${PICKONE_HOST:-0.0.0.0}"
export PICKONE_PORT="${PICKONE_PORT:-8888}"
nohup "$PY" "$SERVER_DIR/app.py" >> "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"

# 等待健康检查（最多 15s）；探活走 127.0.0.1，避免 host 解析问题
for i in $(seq 1 30); do
    if curl -s -m 2 "http://127.0.0.1:${PICKONE_PORT}/api/health" | grep -q '"ok"'; then
        echo "启动成功 pid=$PID  监听 ${PICKONE_HOST}:${PICKONE_PORT}  日志: $LOG"
        exit 0
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "启动失败：进程已退出，见 $LOG"
        tail -n 20 "$LOG"
        rm -f "$PIDFILE"
        exit 1
    fi
    sleep 0.5
done
echo "警告：15s 内健康检查未通过，但进程仍在 (pid=$PID)，请查看 $LOG"
exit 1
