#!/usr/bin/env python3
"""前端自检脚本：
1) 静态服务器 200 + 文件完整性
2) app.js 引用的 DOM id 是否都存在于 index.html
3) app.js 请求的 /api/* 路径是否都在 contracts/api.md 端点表中
4) node --check 语法机检
5) 离线自包含检查（无外部 CDN 引用）
"""
import re, subprocess, sys, time, urllib.request, pathlib
import http.server, socketserver, threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
html = (WEB / "index.html").read_text(encoding="utf-8")
js = (WEB / "app.js").read_text(encoding="utf-8")
api_md = (ROOT / "contracts" / "api.md").read_text(encoding="utf-8")

failures = []

# ---------- 1. 静态服务器 ----------
socketserver.TCPServer.allow_reuse_address = True
PORT = 8899
Handler = lambda *a: http.server.SimpleHTTPRequestHandler(*a, directory=str(WEB))
httpd = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.4)
try:
    for path, needle in [("/", "</html>"), ("/app.js", "webkitSpeechRecognition"), ("/style.css", "glass")]:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
            body = r.read().decode("utf-8", "replace")
            ok = r.status == 200 and needle in body
            print(f"[HTTP] {path}: {r.status}, contains '{needle}': {ok}, {len(body)} bytes")
            if not ok:
                failures.append(f"HTTP {path}")
finally:
    httpd.shutdown(); httpd.server_close()

# ---------- 2. DOM id 检查 ----------
html_ids = set(re.findall(r'id="([^"]+)"', html))
js_ids = set(re.findall(r'\$\("([^"]+)"\)', js))
js_ids = {i for i in js_ids if not i.startswith("wstep-")}  # 运行时动态生成
missing = sorted(js_ids - html_ids)
print(f"[IDS] JS 引用 {len(js_ids)} 个静态 id, HTML 定义 {len(html_ids)} 个, 缺失: {missing or '无'}")
if missing:
    failures.append(f"missing ids: {missing}")

# ---------- 3. API 路径检查 ----------
js_paths = set(re.findall(r'["\'](/api/[^"\'$]*)', js))
contract = []
for _, p in re.findall(r"^\|\s*(GET|POST)\s*\|\s*`([^`]+)`", api_md, re.M):
    pat = re.escape(p)
    for var in ("decision_id", "user_id"):
        pat = pat.replace("\\{" + var + "\\}", "[^/]+")
    pat = pat.replace("\\*", ".*")  # /static/*
    # 契约中带查询串的端点（如 /api/decisions?user_id=demo&limit=20）：
    # 只取路径部分匹配，查询参数由前端拼接；/api/decision/{id} 允许前缀拼接形式
    path_part = pat.split("\\?")[0]
    # 契约路径转正则：{var} -> [^/]+，尾部允许 id / 查询串缺省
    # （前端常写成 "/api/decision/" + id 或 "/api/profile?user_id=" + uid）
    rx = path_part
    if "{decision_id}" in p:
        # 前端常写成 "/api/decision/" + id，允许以裸前缀形式出现在代码里
        rx = rx.replace("[^/]+", "([^/]+)?")
    if "?" in p:
        rx += "\\??($|\\?)"
    else:
        rx += "/?\\??$"
    # 兼容代码里的拼接前缀（如 "/api/decision/"、"/api/profile?user_id="）
    if not rx.endswith("$"):
        contract.append((p, re.compile("^" + rx)))
    else:
        base = rx[:-1]  # 去掉尾部 $
        contract.append((p, re.compile("^" + base + "$")))
        prefix = base.replace("([^/]+)?", "").replace("/?\\??", "").rstrip("\\?/?")
        contract.append((p + " (concat-prefix)", re.compile("^" + re.escape(prefix.rstrip("/")) + "/?$")))




bad = []
for jp in sorted(js_paths):
    # 前端常以 "/api/decision/" + id 形式拼接，去掉尾随 / 或 ? 后再比对
    jp_clean = jp.rstrip("/?")
    if not any(rx.match(jp_clean) for _, rx in contract):
        bad.append(jp)

if bad:
    failures.append(f"api paths not in contract: {bad}")


# ---------- 4. node --check ----------
r = subprocess.run(["node", "--check", str(WEB / "app.js")], capture_output=True, text=True)
print(f"[NODE] node --check app.js: {'OK' if r.returncode == 0 else 'FAIL ' + r.stderr}")
if r.returncode != 0:
    failures.append("node --check")

# ---------- 5. 离线自包含 ----------
ext = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
print(f"[OFFLINE] HTML 外部资源引用: {ext or '无（自包含 OK）'}")
if ext:
    failures.append(f"external refs: {ext}")

print("\n==>", "ALL PASS" if not failures else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
