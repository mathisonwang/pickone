# 前端自测记录（web/SELFTEST.md）

日期：2026-09-26 ｜ 范围：`web/index.html` + `web/app.js` + `web/style.css`（纯原生，无构建、无 CDN）

## 1. 静态服务器 + 页面完整性

```bash
/home/Developer/workspace/.venv/bin/python -m http.server 8899 --directory web --bind 127.0.0.1 &
curl -s -o /dev/null -w "%{http_code} %{size_download}B\n" http://127.0.0.1:8899/
curl -s http://127.0.0.1:8899/ | tail -c 120
kill %1
```

结果：`200 12799B`，HTML 以 `</html>` 完整结尾。✅ 测完已关闭服务器。

## 2. JS 语法机检（本机有 node v22.22.2）

```bash
node --check web/app.js   # → OK（exit 0）
```

## 3. DOM id 一致性 + API 路径契约 + 离线自包含

自动化脚本：`tests/selfcheck_web.py`（起临时服务器、比对 id、比对契约、node --check、外链检查，测完自动关服务）。

```bash
/home/Developer/workspace/.venv/bin/python tests/selfcheck_web.py
```

结果（全部通过）：
```
[HTTP] /: 200, contains '</html>': True, 11056 bytes
[HTTP] /app.js: 200, contains 'webkitSpeechRecognition': True, 40847 bytes
[HTTP] /style.css: 200, contains 'glass': True, 24895 bytes
[IDS] JS 引用 52 个静态 id, HTML 定义 67 个, 缺失: 无
[NODE] node --check app.js: OK
[OFFLINE] HTML 外部资源引用: 无（自包含 OK）
==> ALL PASS
```

API 路径核对：JS 实际使用 `/api/health`、`/api/demo/cases`、`/api/advise`、
`/api/decision/{id}`、`/api/decisions?user_id=`、`/api/feedback`、`/api/profile?user_id=`、
`/api/profile/reset`、`/api/log_activity` —— 全部在 `contracts/api.md` 端点表内。
（注：`/api/demo/cases` 是任务要求新增的端点，契约表 api.md 中**没有**该端点——
前端已做失败兜底：拉不到就用内置 3 个样例。已在此记录，未改契约。）

## 4. mock 模式端到端冒烟（Node 模拟 DOM）

自动化脚本：`tests/smoke_app.js` —— 用 Node `vm` + 最小 DOM/fetch stub 加载 app.js，
以 `?mock=1` 驱动"输入 → 提交 → 轮询 → 结果渲染"全流程。

```bash
cd /home/Developer/workspace/choice && node tests/smoke_app.js
```

结果：
```
[SMOKE] app.js loaded in mock mode, no exception
[SMOKE] PASS - 三张牌渲染 GO / SWITCH / DROP
[SMOKE] PASS - 置信度渲染
[SMOKE] PASS - 对比表渲染（含维度条形）
[SMOKE] PASS - 推理树渲染（Timeline 节点存在）
[SMOKE] PASS - degraded 提示隐藏
[SMOKE] PASS - 结果视图切换
[SMOKE] PASS - 无真实 fetch 发生（mock 完全离线）
[SMOKE] RESULT: ALL PASS
```

## 5. 行为要点自查

- 所有 fetch 同源相对路径，无硬编码 host；单次 15s AbortController 超时。
- advise 轮询 1.5s 间隔、300s 上限；单次轮询失败不中断，自动重试至超时。
- `/api/advise` 兼容三种返回：完整 Decision / `{decision_id,status,result}` 包装 / 仅 id。
- 后端不可达：顶栏红 pill"后端未就绪"，失败页优雅提示 + 重试按钮 + mock 入口，无 alert、无白屏。
- 语音：webkitSpeechRecognition/SpeechRecognition，zh-CN，不支持时按钮置灰提示"不支持语音"。
- mock 数据严格按 data_model.md 字段（Option/ScoredOption/Card/Decision/Profile，
  reasoning_tree 4 层，含 blocked/dims/weights_used）。

## 已知缺陷

1. 浏览器真实渲染效果（动画、375px 溢出）未做截图级验证，仅代码级审查 + Node 冒烟。
2. `/api/demo/cases` 不在 api.md 契约表中（任务书要求），需后端补充实现；前端已有内置兜底。
3. 历史详情弹层复用 Decision 渲染，若后端列表项缺 `raw_text` 会显示"(无内容)"。
4. 追问"补充信息"实现为把原文+补充合并后重新 POST /api/advise（契约无 followup 端点）。
