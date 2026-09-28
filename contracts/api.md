# HTTP API 契约

Base: `http://127.0.0.1:8888`（服务监听 `0.0.0.0:8888`，局域网用 `http://192.168.110.55:8888`）  全部 JSON UTF-8。静态文件从 `web/` 挂载在 `/`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 返回 `web/index.html` |
| GET | `/static/*` | 静态资源 |
| GET | `/api/health` | `{"ok":true,"mxagent_available":bool,"version":"..."}` |
| POST | `/api/parse` | body `{"text":"..."}` → `DecisionRequest`（只做抽取，快，<30s） |
| POST | `/api/advise` | body `{"text":"...","user_id":"demo","use_llm":true}` → `Decision`（完整决策，可能 60~180s） |
| GET | `/api/decision/{decision_id}` | → `Decision` |
| GET | `/api/decisions?user_id=demo&limit=20` | → `{"items":[Decision 摘要]}` |
| POST | `/api/feedback` | body `{"decision_id","chosen","went":bool,"satisfaction":1-5,"note"}` → `{"ok":true,"weights":{...}}` |
| GET | `/api/profile?user_id=demo` | → `Profile` |
| POST | `/api/profile/reset` | body `{"user_id":"demo","preset":"new"|"veteran"}` → `Profile`（演示"新用户 vs 老用户"） |
| POST | `/api/log_activity` | body `{"user_id","activity","category","duration_min","mood"}` → `{"ok":true}`（记录日常，供 rhythm 维度使用） |
| POST | `/api/asr` | body: multipart 音频 → `{"text":"..."}`；不可用时返回 `{"text":"","unavailable":true}`（前端自动退回手输） |
| GET | `/api/demo/cases` | → `{"ok":true,"items":[{"id","title","text"}]}`，内置纠结样例（≥3 条，text 中文非空 >20 字），供前端"一键填入"。必含"健身房 vs 火锅 vs 看电影"三选项主案例 |
| GET | `/api/stream/{decision_id}` | SSE：推送决策进度事件（可选实现；未实现则前端轮询 `/api/decision/{id}`） |

## /api/asr 实现约定
- 若存在 `/home/Developer/workspace/choice/asr/transcribe.py`，用 subprocess 调用：
  `/home/Developer/workspace/.venv/bin/python <该脚本> <audio_file> [--lang zh]`；
  该脚本契约：stdout 仅识别文本，失败时 stdout 为空且 **exit code 仍为 0**。
- **超时 30s**；脚本不存在 / 超时 / 空输出 / 任何异常 → `200 {"text":"","unavailable":true}`。
- 上传音频存 `data/uploads/`，大小上限 10MB，后缀白名单 `webm|ogg|mp4|wav`。

## 引擎侧配套约定（影响测试语义，勿改）
- 引擎通过环境变量 **`MXAGENT_BIN`** 决定调用哪个 mxagent 可执行文件（默认 `mxagent`）。
  这是演示"LLM 不可用"的开关：`MXAGENT_BIN=/nonexistent/mxagent` → 必须返回
  `degraded:true` 的合法 Decision，绝不抛异常。
- 硬约束 `hard_constraints` 是**自然语言字符串列表**，不使用结构化 DSL。
  确定性要求：预算超限、或 `start_hint + duration_min + commute_min` 齐全且推算到家时刻晚于
  deadline → `feasible=false` 且写入 `blocked`、`blocked_reason` 非空。
  信息不全时保留 `feasible=true`，但必须**降低 `confidence`** 并把缺口写入
  `missing_info` / `most_uncertain`。

## 行为要求
1. **任何情况不返回 500**。LLM 失败 → 200 + `degraded:true`。
2. `/api/advise` 是长请求：先落库返回 `decision_id` 也行，但**必须支持同步返回完整 Decision**（前端用长超时 300s）。
   实现建议：后台线程执行 + 立即返回 `{"decision_id":...,"status":"running"}`，
   前端轮询 `/api/decision/{id}` 直到 `status` 为 `done`/`failed`。
   → 因此 `Decision` 落库时外层包一层：`{"decision_id","status":"pending|running|done|failed","error":null|str,"result":Decision|null}`
3. 并发：不同 decision 可并行；同一 user 的 profile 写入加文件锁。
4. 数据落盘：`data/decisions/<id>.json`、`data/profiles/<user_id>.json`、`data/feedback.jsonl`。
5. CORS：允许所有来源（演示需要）。

## 错误响应格式
`{"ok":false,"error":"human readable 中文消息"}` + 合适的 4xx；5xx 仅在不可恢复时使用。
