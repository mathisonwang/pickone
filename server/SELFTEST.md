# server/ SELFTEST — PickOne Web 后端自测记录

- 测试时间：2026-09-27（本机时间）
- 解释器：`/home/Developer/workspace/.venv/bin/python`（3.13.15）
- 监听：`0.0.0.0:8888`（bind 成功，未触发回退）
- 引擎状态：`{"advisor":"stub","extractor":"stub","profile":"stub","models":"stub"}`
  （测试时 `engine/` 尚不存在，全部走 bridge 的 stub；日志中每条调用都标明 REAL/STUB）
- mxagent 探测：`/api/health` 返回 `"mxagent_available": true`

## 1. 启动 / 健康检查

```
$ bash server/run.sh
启动成功 pid=222222  日志: /home/Developer/workspace/choice/data/server.log

$ curl http://127.0.0.1:8888/api/health
{"ok":true,"mxagent_available":true,"version":"0.1.0",
 "engine":{"advisor":"stub","extractor":"stub","profile":"stub","models":"stub"}}
→ HTTP 200 ✅
```

## 2. 全端点验证（正常请求）

| 端点 | 结果 |
|---|---|
| `GET /` | 200，返回 `web/index.html`（前端同事已放置真实页面；若不存在则返回内置占位提示页）✅ |
| `GET /api/health` | 200，结构如上 ✅ |
| `POST /api/parse` `{"text":"今晚去健身房跑步还是去楼下那家火锅店还是在家看电影，22点前必须到家"}` | 200，返回 DecisionRequest：`when="今晚"`，3 个 options（other/food/other），`extracted_ok=false`（stub）✅ |
| `POST /api/advise` `{"text":"...","user_id":"demo","use_llm":true}` | 200 `{"decision_id":"dec-20260927-011428-00e956","status":"running"}` ✅ |
| `GET /api/decision/{id}`（轮询） | 200，外层 `{"decision_id","status":"done","error":null,"result":Decision}`；result 含全部 12 个契约字段（cards/scored/confidence/most_uncertain/ask_user/reasoning_tree/degraded/blocked/weights_used 等），`result.decision_id` 与外层一致，JSON 可序列化 ✅ |
| `GET /api/decisions?user_id=demo&limit=5` | 200 `{"items":[摘要]}`，摘要含 decision_id/status/top_card_title/confidence/degraded ✅ |
| `POST /api/feedback` `{"decision_id":...,"chosen":"去健身房跑步","went":true,"satisfaction":4,"note":"..."}` | 200 `{"ok":true,"weights":{...}}`，权重发生微调（pleasure 0.2538），并写入 `data/feedback.jsonl` ✅ |
| `GET /api/profile?user_id=demo` | 200，Profile 全字段；feedback 后 facts 追加「曾选择「去健身房跑步」，满意度 4/5」✅ |
| `POST /api/profile/reset` `{"user_id":"demo","preset":"veteran"}` | 200，返回老用户档案（goals 含 workout_per_week=3、facts 含"楼下火锅店的老板娘很热情"）✅ |
| `POST /api/log_activity` `{"user_id":"demo","activity":"去健身房跑步","category":"workout","duration_min":60,"mood":5}` | 200 `{"ok":true}`，随后 GET profile 可见 history 追加 ✅ |
| `POST /api/asr`（任意 body） | 200 `{"text":"","unavailable":true}`（本地无 ASR，按契约不报错）✅ |
| `GET /api/demo/cases` | 200，3 个内置样例（含"健身房 vs 火锅 vs 看电影"三选项主案例）✅ |
| `GET /api/stream/{id}`（SSE，可选实现） | 已实现：推送 `event: status` + `event: result`，120s 自动结束 ✅ |

## 3. 坏请求测试（全部不 500）

| 场景 | HTTP | 响应 |
|---|---|---|
| `POST /api/advise` 空 body | 400 | `{"ok":false,"error":"缺少 text 字段：请告诉我在纠结什么"}` |
| `POST /api/advise` 非法 JSON（`{not json`） | 200 | 纯文本容错为输入，正常受理 `{"decision_id":...,"status":"running"}` |
| `POST /api/parse` 非法 JSON（`{{{bad`） | 400 | `{"ok":false,"error":"缺少 text 字段..."}` |
| `POST /api/advise` `{"text":""}` | 400 | 同上缺 text 提示 |
| `GET /api/decision/nope123` | 404 | `{"ok":false,"error":"未找到决策：nope123"}` |
| `GET /api/decision/..%2f..%2fetc%2fpasswd` | 404 | 路径参数校验拦截，无目录穿越 |
| `POST /api/feedback` 不存在的 decision_id | 404 | `{"ok":false,"error":"decision_id 不存在：nope"}` |
| `POST /api/feedback` 空 body | 404 | `{"ok":false,"error":"decision_id 不存在：(空)"}` |
| `GET /api/profile?user_id=<未知用户>` | 200 | 返回默认档案（默认权重 0.22/0.18/...） |
| `GET /api/profile?user_id=../../etc/passwd` | 200 | id 白名单校验，不触碰 data/ 之外路径 |
| `POST /api/profile/reset` 非法 preset | 400 | `{"ok":false,"error":"preset 只能是 \"new\" 或 \"veteran\""}` |
| `POST /api/log_activity` 缺 activity | 400 | `{"ok":false,"error":"缺少 activity 字段"}` |
| `POST /api/log_activity` mood=999、duration="xx" | 200 | 容错钳位（mood→5，duration→0） |
| `GET /api/advise`（方法不允许） | 405 | starlette 默认 |
| `GET /api/nonexistent` | 404 | starlette 默认 |

全程未出现任何 5xx。

## 4. 并发测试

15 并发（10× log_activity + 5× advise，同一 user "loadtest"）：
- 全部 200；`GET /api/profile?user_id=loadtest` → history 恰好 10 条（无丢失）；
- 5 个 decision 全部轮询到 `done`。文件锁（threading.RLock + fcntl.flock）生效。

> 修复记录：初版 profile 端点外层再包一层 `file_lock`，与内层 `save_profile`
> 的同 key 非重入锁死锁（表现为请求挂起）。已改为 store 内统一 `RLock` +
> 去掉外层冗余包装，复测通过。

## 5. 持久化验证（停服 → 重启）

```
$ bash server/stop.sh && bash server/run.sh
已停止 (pid=182540)
启动成功 pid=222222

$ curl .../api/decision/dec-20260927-011428-00e956   → status=done，result 与重启前一致 ✅
$ curl .../api/profile?user_id=demo                  → facts / weights 与重启前完全一致 ✅
```

## 6. stub/real 状态与日志

- 启动日志：`[pickone] 启动 0.0.0.0:8888 (web=..., engine={'advisor': 'stub', ...})`
- 每次调用：`[bridge] extract 使用 STUB` / `[bridge] advise 使用 STUB（degraded=true）`；
  engine/ 就绪后自动切换并打印 `使用 REAL engine`。
- stub 的 Decision 结构完整（cards 含 GO，分差<1.2 时含 DROP），`degraded:true`，
  前端可直接渲染。

## 7. 收尾

```
$ bash server/stop.sh
已停止 (pid=222222)
```
（集成阶段由父代理统一重启。）

## 已知缺陷 / 注意事项

1. **当前全 stub**：engine/ 未就绪，advise 结果为占位启发式（degraded=true）。engine 落位后无需改 server 代码，重启即自动切 REAL；若 real 接口签名/返回结构与契约不符，bridge 会打 WARNING 并回落 stub。
2. `/api/advise` 的非 JSON 纯文本 body 会被容错当作输入文本受理（200）；`/api/parse` 同样处理。若希望严格 400 可再收紧。
3. `GET /` 直接内联读取 `web/index.html`（前端页面引用相对路径 `style.css`/`app.js` 时由浏览器再请求 `/static/...` 或根路径；已同时挂载 `/static/*` → web/）。若前端用其他相对路径 404，属前端路径写法问题，可协商。
4. SSE `/api/stream/{id}` 为轮询落库文件的轻量实现（1s 粒度，120s 超时自动断开），非事件驱动；契约标注可选，前端主用轮询 `/api/decision/{id}` 即可。
5. profile 的 `history` 上限 500 条、`facts` 上限 50 条（防文件无限膨胀）。
6. uvicorn 单进程多线程（线程池处理同步 handler）；决策任务为 daemon 线程——**服务重启会丢失 running 中的任务**（状态停留在 running，前端轮询会一直等）。集成阶段如需可加启动时把遗留 running 标 failed 的清扫，暂未做。
