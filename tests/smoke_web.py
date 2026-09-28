"""前端静态资源与契约一致性检查（web/ + server 静态挂载）

运行（必须 venv 解释器）：
    /home/Developer/workspace/.venv/bin/python tests/smoke_web.py
    /home/Developer/workspace/choice/tests/smoke_web.py --offline   # 不依赖服务，直接读 web/ 文件

检查项：
1. GET / → 200，HTML 含关键元素：输入框 id、语音按钮、结论卡容器。
2. GET /static/app.js、/static/style.css → 200。
3. app.js 中 fetch 的每个 /api/ 路径都在 contracts/api.md 端点表内（自动比对，报告不一致）。
   同时反向检查：前端引用了契约里不存在的端点 → 报"契约外引用"。
4. HTML 里引用的 /static/* 资源都能取到（避免 404 白屏）。

退出码：0 全部通过；1 有失败；2 服务/文件不可用（视为阻塞失败，由 run_all.sh 汇总）。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BASE_URL, CONTRACT_ENDPOINTS, ROOT, http, server_up  # noqa: E402

OFFLINE = "--offline" in sys.argv
SERVER_UP = server_up(timeout=4.0)
WEB_DIR = os.path.join(ROOT, "web")

# 关键元素候选（前端命名可能不同，命中任一即通过；全部未命中 → 失败并列出实际 id）
INPUT_ID_CANDIDATES = ["user-input", "input", "text-input", "input-text", "question",
                       "ask-input", "prompt", "textarea", "choice-text"]
VOICE_ID_CANDIDATES = ["voice-btn", "voice", "mic", "mic-btn", "asr-btn",
                       "record-btn", "btn-voice", "voiceButton"]
CARDS_ID_CANDIDATES = ["cards", "card-area", "cards-area", "three-cards", "cards-container",
                       "result-cards", "card-list", "cardsWrap", "decision-cards"]

ENDPOINT_PATTERNS = [
    "/api/health", "/api/parse", "/api/advise", "/api/decision/", "/api/decisions",
    "/api/feedback", "/api/profile/reset", "/api/profile", "/api/log_activity",
    "/api/asr", "/api/stream/", "/api/demo/cases",
]


def _match_contract(path: str) -> bool:
    """前端 fetch 的 path 是否落在契约端点表内。"""
    p = path.split("?")[0].split("#")[0]
    for ep in CONTRACT_ENDPOINTS:
        if ep.endswith("/*"):          # /static/*
            if p.startswith(ep[:-1]):
                return True
        elif "{" in ep:                # /api/decision/{id}
            prefix = ep.split("{")[0]
            if p.startswith(prefix) and len(p) >= len(prefix):
                return True
        elif p == ep or p.rstrip("/") == ep:
            return True
    return False



def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _fetch(path, raw=True):
    return http("GET", path, timeout=20.0, raw=raw)


def main() -> int:
    failures, skips, oks = [], [], []

    def ok(msg):
        oks.append(msg)
        print(f"  [PASS] {msg}")

    def fail(msg):
        failures.append(msg)
        print(f"  [FAIL] {msg}")

    def skip(msg):
        skips.append(msg)
        print(f"  [SKIP] {msg}")

    print(f"=== smoke_web.py  base={BASE_URL} offline={OFFLINE} server_up={SERVER_UP} ===")

    html = js = css = None
    # ---------------- 1. 取 HTML / JS / CSS ----------------
    if not OFFLINE and SERVER_UP:
        st, _, body, err = _fetch("/")
        if st == 200 and body:
            html = body.decode("utf-8", "replace")
            ok(f"GET / → 200（{len(html)} 字符）")
        else:
            fail(f"GET / 期望 200，实际 status={st} err={err}")
        for rel in ("/static/app.js", "/static/style.css"):
            st, _, body, err = _fetch(rel)
            if st == 200 and body:
                ok(f"GET {rel} → 200（{len(body)} 字节）")
                if rel.endswith(".js"):
                    js = body.decode("utf-8", "replace")
                else:
                    css = body.decode("utf-8", "replace")
            else:
                fail(f"GET {rel} 期望 200，实际 status={st} err={err}")
    else:
        if not OFFLINE:
            skip(f"服务未就绪（{BASE_URL}），改用离线读取 {WEB_DIR}")
        html = _read(os.path.join(WEB_DIR, "index.html"))
        js = _read(os.path.join(WEB_DIR, "app.js"))
        css = _read(os.path.join(WEB_DIR, "style.css"))
        for name, content in (("index.html", html), ("app.js", js), ("style.css", css)):
            if content:
                ok(f"离线读取 web/{name}（{len(content)} 字符）")
            else:
                fail(f"web/{name} 不存在或为空（前端未就绪）")

    # ---------------- 2. HTML 关键元素 ----------------
    if html:
        ids = re.findall(r'id\s*=\s*["\']([^"\']+)["\']', html)
        # 也允许前端在 JS 里动态创建元素：把 js 里的 id 一并纳入
        ids += re.findall(r'\.id\s*=\s*["\']([^"\']+)["\']', js or "")
        ids += re.findall(r'id:\s*["\']([^"\']+)["\']', js or "")
        lowered = [i.lower() for i in ids]

        def find(cands, label):
            hit = [c for c in cands if c.lower() in lowered]
            if hit:
                ok(f"{label} 命中：#{hit[0]}")
                return True
            fail(f"{label} 未找到（候选 {cands}；HTML/JS 实际 id 列表：{ids[:40]}）")
            return False

        find(INPUT_ID_CANDIDATES, "输入框")
        find(VOICE_ID_CANDIDATES, "语音按钮")
        find(CARDS_ID_CANDIDATES, "结论卡容器")
        if "textarea" not in html.lower() and 'type="text"' not in html.lower() \
                and 'contenteditable' not in html.lower():
            fail("HTML 里既无 textarea 也无 text input（用户无法输入问题）")
        else:
            ok("HTML 含输入控件（textarea / text input / contenteditable）")
        # HTML 引用的静态资源是否都存在（在线时）
        if not OFFLINE and SERVER_UP:
            refs = re.findall(r'(?:src|href)\s*=\s*["\'](/static/[^"\']+)["\']', html)
            for r_ in refs:
                st, _, _, err = _fetch(r_)
                if st == 200:
                    ok(f"HTML 引用资源 {r_} → 200")
                else:
                    fail(f"HTML 引用资源 {r_} → status={st}（会 404 白屏）err={err}")
    else:
        skip("无 HTML，跳过关键元素检查")

    # ---------------- 3. JS fetch 路径 vs 契约 ----------------
    if js:
        calls = re.findall(r'''fetch\s*\(\s*[`'"]([^`'"$]+)[`'"]''', js)
        calls += re.findall(r'''[`'"](/api/[^`'"$?\s]+)[`'"]''', js)
        api_calls = sorted({c for c in calls if c.startswith("/api") or "/api/" in c})
        if not api_calls:
            fail("app.js 里找不到任何 /api/ 调用（前端未接后端？）")
        unknown = []
        for c in api_calls:
            if _match_contract(c):
                ok(f"前端 API 调用 {c} 在契约端点表内")
            else:
                unknown.append(c)
        if unknown:
            fail(f"前端引用了契约外 API 路径（需后端补实现或前端改）：{unknown}")
        # 反向：契约里有但前端没用（信息性，不算失败）
        unused = [ep for ep in CONTRACT_ENDPOINTS
                  if ep not in ("/", "/static/*")
                  and not any(ep.split("{")[0].rstrip("/") in c for c in api_calls)]
        if unused:
            skip(f"契约中存在但前端未调用的端点（信息性）：{unused}")
        # 前端是否处理 degraded（降级演示依赖）
        if "degraded" in js:
            ok("app.js 处理了 degraded 字段（降级提示可演示）")
        else:
            fail("app.js 未处理 Decision.degraded（降级演示无 UI 反馈）")
        if "reasoning_tree" in js or "reasoningTree" in js:
            ok("app.js 渲染 reasoning_tree（幕后推理树可演示）")
        else:
            fail("app.js 未渲染 reasoning_tree（推理树演示缺 UI）")
    else:
        skip("无 app.js，跳过 API 路径比对")

    if css is None:
        fail("style.css 不可用（前端样式缺失）")

    print(f"--- smoke_web 汇总：通过 {len(oks)}，失败 {len(failures)}，跳过 {len(skips)} ---")
    for f_ in failures:
        print(f"    FAIL: {f_}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
