#!/usr/bin/env bash
# 一键端到端测试：引擎单测 + HTTP 集成测试 + 前端 smoke
#
# 用法：
#   bash tests/run_all.sh                 # 常规（快速链路，不调真实 LLM）
#   PICKONE_LLM=1 bash tests/run_all.sh   # 加跑真实 LLM 全链路（单条最长 300s）
#   PICKONE_SLOW=1 bash tests/run_all.sh  # 加跑超长文本 advise 等慢用例
#   PICKONE_KEEP=0 bash tests/run_all.sh  # 测完关掉本脚本自己起的服务（默认保留）
#   PICKONE_NO_START=1 ...                # 不去尝试启动服务（只测已在线的）
#
# 输出：控制台中文汇总 + 追加到 tests/LATEST_RUN.txt（带时间戳，保留历史趋势）
# 退出码：0 = 无失败；1 = 有失败；2 = 环境阻塞（解释器错误等）

set -uo pipefail

ROOT="/home/Developer/workspace/choice"
PY="/home/Developer/workspace/.venv/bin/python"
BASE="${PICKONE_BASE:-http://127.0.0.1:8888}"
LOG="$ROOT/tests/LATEST_RUN.txt"
TMPDIR_RUN="$(mktemp -d /tmp/pickone_run.XXXXXX)"
SERVER_LOG="$TMPDIR_RUN/server.log"
SERVER_STARTED=0
SERVER_PID=""

cd "$ROOT" || { echo "[FATAL] 无法进入 $ROOT"; exit 2; }

# --- 解释器硬校验 ---
if [ ! -x "$PY" ]; then
  echo "[FATAL] 找不到 venv 解释器 $PY"; exit 2
fi
export TMPDIR_RUN


stamp() { date '+%Y-%m-%d %H:%M:%S'; }

{
  echo "=============================================================="
  echo "运行时间: $(stamp)"
  echo "解释器: $($PY -V 2>&1) @ $PY"
  echo "服务地址: $BASE"
  echo "开关: PICKONE_LLM=${PICKONE_LLM:-0} PICKONE_SLOW=${PICKONE_SLOW:-0} PICKONE_NO_START=${PICKONE_NO_START:-0}"
  echo "=============================================================="
} >> "$LOG"

# ---------------------------------------------------------------------------
# 0. 服务可用性：不通则尝试 server/run.sh 启动
# ---------------------------------------------------------------------------
probe() {
  "$PY" - <<'PYEOF' >/dev/null 2>&1
import sys, urllib.request
sys.path.insert(0, "/home/Developer/workspace/choice/tests")
from common import server_up
sys.exit(0 if server_up(timeout=3.0) else 1)
PYEOF
}

if probe; then
  echo "[准备] 服务已在线：$BASE" | tee -a "$LOG"
elif [ "${PICKONE_NO_START:-0}" = "1" ]; then
  echo "[准备] PICKONE_NO_START=1，不启动服务（API/前端测试将整块跳过）" | tee -a "$LOG"
elif [ -f "$ROOT/server/run.sh" ]; then
  echo "[准备] 服务未在线，尝试 bash server/run.sh 启动（最多等 25s）..." | tee -a "$LOG"
  nohup bash "$ROOT/server/run.sh" >"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  SERVER_STARTED=1
  for i in $(seq 1 25); do
    sleep 1
    if probe; then echo "[准备] 服务已启动（${i}s，pid=$SERVER_PID）" | tee -a "$LOG"; break; fi
  done
  probe || echo "[准备] 启动失败：server/run.sh 未能让服务就绪，详见 $SERVER_LOG" | tee -a "$LOG"
else
  echo "[准备] server/run.sh 不存在（后端未就绪），API/前端测试将整块跳过" | tee -a "$LOG"
fi

# ---------------------------------------------------------------------------
# 1. 引擎单测
# ---------------------------------------------------------------------------
echo "" | tee -a "$LOG"
echo "########## [1/3] 引擎单测 tests/test_engine.py ##########" | tee -a "$LOG"
MXAGENT_BIN=/nonexistent/mxagent "$PY" -m unittest -v tests.test_engine \
  >"$TMPDIR_RUN/engine.txt" 2>&1
ENGINE_RC=$?
cat "$TMPDIR_RUN/engine.txt" >> "$LOG"
grep -E '^(test_|OK|FAILED|ERROR|Ran )' "$TMPDIR_RUN/engine.txt" | tail -60 | tee -a "$LOG"

# ---------------------------------------------------------------------------
# 2. API 集成测试
# ---------------------------------------------------------------------------
echo "" | tee -a "$LOG"
echo "########## [2/3] API 集成测试 tests/test_api.py ##########" | tee -a "$LOG"
"$PY" -m unittest -v tests.test_api >"$TMPDIR_RUN/api.txt" 2>&1
API_RC=$?
cat "$TMPDIR_RUN/api.txt" >> "$LOG"
grep -E '^(test_|OK|FAILED|ERROR|Ran )' "$TMPDIR_RUN/api.txt" | tail -80 | tee -a "$LOG"

# ---------------------------------------------------------------------------
# 3. 前端 smoke
# ---------------------------------------------------------------------------
echo "" | tee -a "$LOG"
echo "########## [3/3] 前端 smoke tests/smoke_web.py ##########" | tee -a "$LOG"
"$PY" tests/smoke_web.py >"$TMPDIR_RUN/web.txt" 2>&1
WEB_RC=$?
cat "$TMPDIR_RUN/web.txt" >> "$LOG"
grep -E '^  \[(PASS|FAIL|SKIP)\]|^---' "$TMPDIR_RUN/web.txt" | tee -a "$LOG"

# ---------------------------------------------------------------------------
# 4. 汇总（unittest 输出解析 + smoke_web 的 PASS/FAIL/SKIP）
# ---------------------------------------------------------------------------
count() { # file pattern
  grep -cE "$2" "$1" 2>/dev/null || true
}
{
  echo ""
  echo "=============================================================="
  echo "汇总 $(stamp)"
  echo "=============================================================="
} >> "$LOG"

"$PY" - <<PYEOF | tee -a "$LOG"
import os, re, sys
tmp = os.environ["TMPDIR_RUN"]

def parse(path):
    """返回 (passed, failed, skipped, errors_list, fail_list)
    以 unittest 末尾的 'FAILED (failures=N, errors=M, skipped=K)' 为权威计数，
    避免多行测试名导致逐行正则漏计。"""
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return 0, 0, 0, [], []
    fails = re.findall(r'^(FAIL|ERROR): (\S+)', txt, re.M)
    m = re.search(r'^Ran (\d+) tests? in', txt, re.M)
    ran = int(m.group(1)) if m else 0
    fm = re.search(r'^FAILED \(.*?\)', txt, re.M)
    if fm:
        s = fm.group(0)
        f = int(re.search(r'failures=(\d+)', s).group(1)) if 'failures=' in s else 0
        e = int(re.search(r'errors=(\d+)', s).group(1)) if 'errors=' in s else 0
        sk = int(re.search(r'skipped=(\d+)', s).group(1)) if 'skipped=' in s else 0
        p = ran - f - e - sk
    elif re.search(r'^OK(?: \(skipped=\d+\))?$', txt, re.M):
        om = re.search(r'^OK \(skipped=(\d+)\)$', txt, re.M)
        sk = int(om.group(1)) if om else 0
        f = e = 0
        p = ran - sk
    else:
        lines = re.findall(r'^(test_[^\n]*?) \.\.\. (ok|FAIL|ERROR|skipped.*)$', txt, re.M)
        p = sum(1 for _, s in lines if s == "ok")
        f = sum(1 for _, s in lines if s == "FAIL")
        e = sum(1 for _, s in lines if s == "ERROR")
        sk = sum(1 for _, s in lines if s.startswith("skipped"))
    fail_list = [n for _, n in fails]
    return p, f + e, sk, [], fail_list


def parse_smoke(path):
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return 0, 0, 0, []
    p = len(re.findall(r'^  \[PASS\]', txt, re.M))
    f = len(re.findall(r'^  \[FAIL\]', txt, re.M))
    s = len(re.findall(r'^  \[SKIP\]', txt, re.M))
    fails = re.findall(r'^  \[FAIL\] (.*)$', txt, re.M)
    return p, f, s, fails

eng = parse(os.path.join(tmp, "engine.txt"))
api = parse(os.path.join(tmp, "api.txt"))
web = parse_smoke(os.path.join(tmp, "web.txt"))

def why_skipped(path):
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return []
    return re.findall(r'^test_[^\n]*? \.\.\. skipped(?:\s+\d+\.\d+s)?\s*(?:\(.*?\))?:?(.*)$', txt, re.M)

reasons = why_skipped(os.path.join(tmp, "engine.txt")) + why_skipped(os.path.join(tmp, "api.txt"))
# 归并同类跳过原因
merged = {}
for r in reasons:
    r = r.strip().strip("'\"") or "(无原因)"
    key = r[:70]
    merged[key] = merged.get(key, 0) + 1

tp = eng[0] + api[0] + web[0]
tf = eng[1] + api[1] + web[1]
ts = eng[2] + api[2] + web[2]

print("本轮跳过的模块/端点清单：")
if merged:
    for k, v in sorted(merged.items(), key=lambda x: -x[1]):
        print(f"  · [{v}项] {k}")
else:
    print("  （无跳过）")
print()
print(f"[1/3] 引擎单测   ：通过 {eng[0]}，失败 {eng[1]}，跳过 {eng[2]}")
print(f"[2/3] API 集成   ：通过 {api[0]}，失败 {api[1]}，跳过 {api[2]}")
print(f"[3/3] 前端 smoke ：通过 {web[0]}，失败 {web[1]}，跳过 {web[2]}")
print("-" * 62)
print(f"总计：通过 {tp}，失败 {tf}，跳过 {ts}")
if tf:
    print()
    print("失败明细：")
    for n in eng[4]:
        print(f"  [engine] {n}")
    for n in api[4]:
        print(f"  [api]    {n}")
    for n in web[3]:
        print(f"  [web]    {n}")
print()
print("结论：" + ("存在失败，退出码 1" if tf else "全部通过（跳过项见上，需实现就绪后复跑）"))
PYEOF

# 失败详情原文（方便定位）
if [ "${PICKONE_VERBOSE:-1}" = "1" ]; then
  {
    echo ""
    echo "----- 失败详情原文（engine） -----"
    awk '/^====+$/{p=1} p' "$TMPDIR_RUN/engine.txt" | grep -A 25 -E '^(FAIL|ERROR):' | head -200
    echo ""
    echo "----- 失败详情原文（api） -----"
    awk '/^====+$/{p=1} p' "$TMPDIR_RUN/api.txt" | grep -A 25 -E '^(FAIL|ERROR):' | head -300
  } >> "$LOG"
fi

# ---------------------------------------------------------------------------
# 5. 服务保留/清理
# ---------------------------------------------------------------------------
if [ "$SERVER_STARTED" = "1" ]; then
  if [ "${PICKONE_KEEP:-1}" = "1" ]; then
    echo "" | tee -a "$LOG"
    echo "[清理] 服务由本脚本启动（pid=$SERVER_PID），按 PICKONE_KEEP=1 保留运行" | tee -a "$LOG"
  else
    kill "$SERVER_PID" 2>/dev/null && echo "[清理] 已关闭本脚本启动的服务 (pid=$SERVER_PID)" | tee -a "$LOG"
  fi
fi

echo "" | tee -a "$LOG"
echo "[日志] 完整输出已追加到 $LOG（本轮临时目录 $TMPDIR_RUN）" | tee -a "$LOG"

# 退出码以三个子测试的真实退出码为准（汇总脚本仅做展示，不参与判定）
if [ "$ENGINE_RC" -ne 0 ] || [ "$API_RC" -ne 0 ] || [ "$WEB_RC" -ne 0 ]; then
  echo "[退出码] 1（engine_rc=$ENGINE_RC api_rc=$API_RC web_rc=$WEB_RC）" | tee -a "$LOG"
  exit 1
fi
exit 0
