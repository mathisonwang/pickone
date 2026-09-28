#!/usr/bin/env bash
# tests/preflight.sh — 演示当天早上"一键自检"（30 秒内出结论）
#
# 用法：
#   bash tests/preflight.sh              # 用默认地址 127.0.0.1:8888
#   PICKONE_BASE=http://127.0.0.1:8888 bash tests/preflight.sh
#
# 检查 5 件事：
#   (a) 服务是否在跑            curl /api/health
#   (b) LLM 端点是否可用        打 http://127.0.0.1:8080/v1/chat/completions（10s 超时）
#   (c) 前端静态资源是否 200    / 、/static/app.js、/static/style.css
#   (d) engine 是 REAL 还是 stub 从 /api/health 的 engine 字段读
#   (e) 本地 ASR 是否可用       asr/transcribe.py + 模型文件
#
# 退出码：0 = 全部正常；1 = 有问题（末尾会给出明确建议）
# 安全：绝不打印 api_key，只打印 endpoint 主机名。

set -uo pipefail

ROOT="/home/Developer/workspace/choice"
PY="/home/Developer/workspace/.venv/bin/python"
BASE="${PICKONE_BASE:-http://127.0.0.1:8888}"
TOKENS="${HOME}/.my_tokens.yaml"
LLM_URL="http://127.0.0.1:8080/v1/chat/completions"
LLM_MODEL="qwen3.8-flash-next"

# 颜色（非 TTY 时自动退化为纯文本，方便重定向到文件）
if [ -t 1 ]; then
  G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else
  G=""; R=""; Y=""; B=""; N=""
fi

PROBLEMS=()      # 收集问题描述
NOTES=()         # 收集非阻塞提示
START_TS=$(date +%s)

hr() { printf '%s\n' "------------------------------------------------------------"; }

echo "${B}PickOne · 演示前自检${N}  $(date '+%Y-%m-%d %H:%M:%S')"
echo "目标服务: $BASE"
hr

# ===========================================================================
# (a) 服务是否在跑
# ===========================================================================
HEALTH_JSON=""
HEALTH_CODE=$(curl -s -m 8 -o /tmp/_pf_health.$$ -w '%{http_code}' "$BASE/api/health" 2>/dev/null)
if [ "$HEALTH_CODE" = "200" ] && grep -q '"ok"' /tmp/_pf_health.$$ 2>/dev/null; then
  HEALTH_JSON=$(cat /tmp/_pf_health.$$)
  echo "[${G}OK${N}]   (a) 服务在跑：$BASE/api/health → HTTP 200"
else
  echo "[${R}FAIL${N}] (a) 服务没起来：$BASE/api/health → HTTP ${HEALTH_CODE:-无响应}"
  PROBLEMS+=("Web 服务未运行（HTTP ${HEALTH_CODE:-无响应}）")
  echo "       修复：bash server/run.sh   （日志 tail -n 30 data/server.log）"
fi
rm -f /tmp/_pf_health.$$

# ===========================================================================
# (b) LLM 端点是否可用（不打印 key）
# ===========================================================================
LLM_EP=""
LLM_KEY_SET="no"
if [ -f "$TOKENS" ]; then
  # 只取 endpoint 段的 endpoint / api_key 是否存在；绝不打印 key 本体
  LLM_EP=$("$PY" - "$TOKENS" <<'PYEOF' 2>/dev/null
import sys
try:
    import yaml
except Exception:
    print(""); sys.exit(0)
try:
    d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
except Exception:
    print(""); sys.exit(0)
ep = d.get("endpoint") or {}
print((ep.get("endpoint") or "").strip())
PYEOF
)
  if "$PY" - "$TOKENS" <<'PYEOF' 2>/dev/null
import sys
try:
    import yaml
except Exception:
    sys.exit(1)
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
ep = d.get("endpoint") or {}
k = ep.get("api_key")
sys.exit(0 if (k and str(k).strip() and str(k).strip().lower() != "none") else 1)
PYEOF
  then LLM_KEY_SET="yes"; fi
fi

if [ -z "$LLM_EP" ]; then LLM_EP="$LLM_URL"; fi

# 直接打本地 llama-server；带 10s 超时。key 通过 header 传入但不落盘不打印。
AUTH_HDR=()
if [ "$LLM_KEY_SET" = "yes" ]; then
  KEY_VAL=$("$PY" - "$TOKENS" <<'PYEOF' 2>/dev/null
import sys
try:
    import yaml
except Exception:
    print(""); sys.exit(0)
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print(str((d.get("endpoint") or {}).get("api_key") or "").strip())
PYEOF
)
  [ -n "$KEY_VAL" ] && AUTH_HDR=(-H "Authorization: Bearer $KEY_VAL")
fi

LLM_T0=$(date +%s)
LLM_CODE=$(curl -s -m 10 "${AUTH_HDR[@]}" -H 'Content-Type: application/json' \
  -o /tmp/_pf_llm.$$ -w '%{http_code}' \
  "$LLM_EP/chat/completions" \
  -d "{\"model\":\"$LLM_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8,\"stream\":false}" 2>/dev/null)
LLM_MS=$(( ($(date +%s) - LLM_T0) * 1000 ))

if [ "$LLM_CODE" = "200" ] && grep -q '"choices"' /tmp/_pf_llm.$$ 2>/dev/null; then
  echo "[${G}OK${N}]   (b) LLM 端点可用：${LLM_EP} （${LLM_MS}ms，key 已配置=${LLM_KEY_SET}）"
else
  echo "[${R}FAIL${N}] (b) LLM 端点不可用：${LLM_EP} → HTTP ${LLM_CODE:-超时/无响应}（${LLM_MS}ms）"
  PROBLEMS+=("LLM 端点不可用（${LLM_EP} → ${LLM_CODE:-超时}）")
  echo "       影响：完整推演模式会降级；演示请改走离线快速模式或 ?mock=1"
fi
rm -f /tmp/_pf_llm.$$

# ===========================================================================
# (c) 前端静态资源
# ===========================================================================
STATIC_FAIL=0
for p in "/" "/static/app.js" "/static/style.css"; do
  c=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$BASE$p" 2>/dev/null)
  if [ "$c" = "200" ]; then
    echo "[${G}OK${N}]   (c) 静态资源 $p → 200"
  else
    echo "[${R}FAIL${N}] (c) 静态资源 $p → ${c:-无响应}"
    STATIC_FAIL=1
  fi
done
[ "$STATIC_FAIL" = "1" ] && PROBLEMS+=("前端静态资源缺失（/ 、/static/app.js、/static/style.css 未全部 200）")

# ===========================================================================
# (d) engine REAL 还是 stub
# ===========================================================================
ENGINE_MODE="unknown"
if [ -n "$HEALTH_JSON" ]; then
  ENGINE_MODE=$("$PY" - <<PYEOF 2>/dev/null
import json
try:
    d = json.loads(r'''$HEALTH_JSON''')
except Exception:
    print("unknown"); raise SystemExit
eng = d.get("engine") or {}
if not isinstance(eng, dict) or not eng:
    print("unknown"); raise SystemExit
vals = [str(v).lower() for v in eng.values()]
if all(v in ("real", "true") for v in vals):
    print("REAL")
elif any(v in ("real", "true") for v in vals):
    print("MIXED")
else:
    print("stub")
PYEOF
)
fi

case "$ENGINE_MODE" in
  REAL)
    echo "[${G}OK${N}]   (d) engine = REAL（真实 mxagent 决策链路）"
    ;;
  MIXED)
    echo "[${Y}WARN${N}] (d) engine = MIXED（决策链路 advisor/extractor 仍为 stub）"
    NOTES+=("engine 决策链路仍为 stub：结果页会带降级提示，需按预案话术讲解")
    ;;
  stub)
    echo "[${Y}WARN${N}] (d) engine = stub（决策为占位启发式，degraded=true）"
    NOTES+=("engine 仍为 stub：结果页会带降级提示，需按预案话术讲解")
    ;;
  *)
    echo "[${Y}WARN${N}] (d) engine 状态未知（/api/health 无 engine 字段）"
    NOTES+=("无法从 /api/health 判断 engine 模式")
    ;;
esac

# ===========================================================================
# (e) 本地 ASR
# ===========================================================================
ASR_SCRIPT="$ROOT/asr/transcribe.py"
ASR_MODEL_DIR="$ROOT/asr/models/faster-whisper-small"
ASR_OK=1
if [ -f "$ASR_SCRIPT" ]; then
  echo "[${G}OK${N}]   (e) ASR 脚本存在：asr/transcribe.py"
else
  echo "[${R}FAIL${N}] (e) ASR 脚本缺失：asr/transcribe.py"
  ASR_OK=0
fi
if [ -s "$ASR_MODEL_DIR/model.bin" ]; then
  SZ=$(du -m "$ASR_MODEL_DIR/model.bin" 2>/dev/null | cut -f1)
  echo "[${G}OK${N}]   (e) ASR 模型就绪：model.bin（约 ${SZ:-?}MB）"
else
  echo "[${R}FAIL${N}] (e) ASR 模型缺失：$ASR_MODEL_DIR/model.bin"
  ASR_OK=0
fi
if [ "$ASR_OK" = "0" ]; then
  PROBLEMS+=("本地 ASR 不可用（脚本或模型缺失）")
  echo "       影响：语音输入演示不可用，改用打字输入（不影响主流程）"
fi

# ===========================================================================
# 结论
# ===========================================================================
ELAPSED=$(( $(date +%s) - START_TS ))
hr
echo "自检耗时 ${ELAPSED}s"

if [ ${#NOTES[@]} -gt 0 ]; then
  echo ""
  echo "提示（不阻塞演示）："
  for n in "${NOTES[@]}"; do echo "  · $n"; done
fi

echo ""
if [ ${#PROBLEMS[@]} -eq 0 ]; then
  echo "${B}结论：可以走完整模式${N}"
  echo "  （服务 / LLM / 静态资源 / ASR 全部就绪${ENGINE_MODE:+，engine=$ENGINE_MODE}）"
  exit 0
fi

echo "${B}结论：建议走离线快速模式${N}"
echo "  原因："
for p in "${PROBLEMS[@]}"; do echo "   · $p"; done
echo ""
echo "  处置："
echo "   1) LLM 慢/不可用 → 前端点「⚡ 太久了，先用快速模式看看」，或直接 ?mock=1"
echo "   2) 服务没起 → bash server/run.sh"
echo "   3) ASR 不可用 → 语音环节改打字，主流程不受影响"
exit 1
