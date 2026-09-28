# PickOne

> ## Attention is what we save.
> **把注意力，花在值得的事情上。**
>
> 帮选择困难的人，把纠结变成决定。不是聊天机器人，是一个**可解释的决策引擎**。

---

## 这是什么

一个跑在 [mxagent](https://nvidia.atlassian.net/wiki/spaces/MXAIPROD)（MMPLEX 分层多智能体框架）之上的 **Web App**：
用户用自然语言（文字或语音）说出自己的纠结，系统综合**长期档案**、**每个选项的未来 48 小时推演**、
**硬约束校验**，最后**只给一个结论**和它的好处。

**为什么只给一个结论**：展示多个选项本身就是内耗。给三张牌，用户会开始比牌；
给对比表，用户会去核对每一维。我们做了半分钟推演，最后又把纠结原样还给了用户——那推演就白做了。
信息不全也无所谓：抽不出选项就从原话里抓一个，再不行给个"先出门走走"。
**选择困难的解药常常不是想清楚，是承认想不清楚，然后动起来。**

## 快速开始

```bash
# 依赖：Python 3.13 venv（含 starlette / uvicorn / faster-whisper）
# 详见 docs/ARCHITECTURE.md 第 1 节

bash server/run.sh          # 启动，默认监听 0.0.0.0:8888
bash tests/preflight.sh     # 演示前一键自检（1 秒出结论）
bash tests/run_all.sh       # 端到端测试（67 项）
```

打开 `http://127.0.0.1:8888`（局域网用 `http://<本机IP>:8888`）。

## 决策流程

```
自然语言输入
  ① Extractor   LLM 结构化抽取 → DecisionRequest（选项/纠结点/硬约束/缺失信息）
  ② Timeline    mxagent --level 2 层级推演 → 每个选项的未来 48h 后果
  ③ Scorer      纯 Python 确定性 7 维加权打分（LLM 不算数，同输入同输出）
  ④ Censor      硬约束闸门（预算 / 截止时间），在生成结论前拦截
  ⑤ Explainer   唯一结论（GO）+ 只讲优点 + 置信度 + 劝退纠结的收尾句
```

7 个维度：`rhythm`（生活节律缺口）/ `social` / `commute` / `cost` / `pleasure` / `health` / `future`。

## 为什么必须用 mxagent

| 能力 | 在产品里的位置 |
|---|---|
| `--level 2` 层级递归委派 | 每个选项一条独立时间线，推演到未来 48h |
| `--snapshot` + `<snapshot>.d/agents/*.jsonl` | 长期记忆 + 每步决策可审计回放（可解释性的数据来源） |
| `--censor` | 硬约束在**生成结论前**拦截 |
| `agent2tool` / `--as_mcp` | 抽取器/推演器/解释器作为可复用 agent 能力分层组合 |
| `--result_check` | 自动验收 |
| `MXAGENT_BIN` | 降级开关：指向不存在的可执行文件 → 走启发式，产品仍可用 |

**LLM 只做理解与推演，算术一律在 Python**：分数、排名、通勤成本换算全部确定性计算，
同样输入永远同样输出，可单测、可复现。这是对"AI 胡说八道"的工程防御。

## 目录

```
engine/    mxagent 决策引擎（models / schema / mxcli / extractor / timeline /
           scorer / censor / explainer / advisor / profile / cli）
server/    starlette + uvicorn，监听 0.0.0.0:8888；REAL/stub 自动切换 bridge
web/       手机优先 UI（原生 HTML/CSS/JS，无构建、无 CDN）
asr/       本地 whisper 离线语音识别（可选，faster-whisper small）
tests/     67 项测试 + preflight.sh 演示前自检
demo/      5 分钟演示台本 + 8 个纠结案例
docs/      ARCHITECTURE.md（唯一事实来源）+ CONTEXT_FOR_TEAM.md
contracts/ API 契约 + 数据模型契约
```

## 产品韧性

- LLM 不可用 / 超时 → 自动降级为启发式模式，**返回结构完整的结论**，服务永不 500
- 后端 150s 硬超时看门狗；前端 60s 后出现"⚡ 先用快速模式"按钮
- `?mock=1` 纯离线演示模式（内置假数据，零请求）
- 服务重启自动清扫遗留未完成任务

## 已知限制

- LLM 端点是共享资源（本机 llama-server `--parallel 1`），完整推演 60~180s
- ASR 中文准确率依赖输入音频质量；浏览器语音用 Web Speech API（Firefox 不支持）
- `asr/models/` 未入库（464MB），按 `asr/README.md` 本地安装

## 文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 架构、5 段流程、7 维打分、6 条不可妥协约束
- [`contracts/api.md`](contracts/api.md) — HTTP API 契约
- [`contracts/data_model.md`](contracts/data_model.md) — 数据模型契约
- [`demo/script.md`](demo/script.md) — 5 分钟现场演示台本（含应急预案与评委 Q&A）

---

*基于 2026-09-27 实测状态编写。所有命令均已实际运行验证。*

---

# 附：完整项目文档

## 1. 一句话定位与痛点

用户用自然语言（文字或语音）说清"今晚/这周要面对的几个选项 + 各自的纠结点"，
系统综合**用户长期档案**（运动频次、偏好、约束）、**选项间的相互影响**、
**对未来安排的连锁后果**，给出可解释的建议，并在事后回收满意度让推荐越用越准。

**解决的痛点**：选择困难之所以难，不是因为选项少，而是因为三个通用问答工具都不处理的事：

1. **选项互相影响**——"去健身房路上能顺路买菜"、"一个人吃火锅有点尴尬"，
   选项不是独立条目，是一张互相咬合的网。
2. **需要考虑长期后果**——此刻的选择会改变未来 48 小时的精神状态、后天的安排。
3. **需要考虑个人节律**——"每周想运动 3 次、已经 4 天没动"这种只有你自己知道的事。

ChatGPT 类工具对这三个都是无状态的：它不记你、不推演后果、不给结论（只列优缺点）。
**我们给结论，并且告诉你为什么，还敢说"这条我不确定"。**

---

## 2. 产品设计说明

### 2.1 只给一个选项（2026-09-27 修订：从"三张牌"改为"单结论"）

| 输出 | 含义 | 出现条件 |
|---|---|---|
| **GO · 就它了** | 一个选项 + **只讲它的优点**（引用档案证据，如"你已经 4 天没运动了"） | 每次都有，且**只有这一张** |

**为什么改成只给一个**（这是产品最重要的一次判断）：

- **展示多个选项本身就是内耗。** 给三张牌，用户会开始比牌；给对比表，用户会去核对每一维。
  我们做了半小时推演，最后又把纠结原样还给了用户——那推演的意义就没了。
- **信息不全也无所谓，随便选一个。** 抽不出选项就从原话里抓一个，再不行给低成本默认建议
  （"先出门走走"），配一句"信息不多也能定：先动起来，比继续想更有价值"。
  选择困难的解药常常不是"想清楚"，是"承认想不清楚，然后动起来"。
- **只讲优点，不提缺点。** 说"通勤是硬伤"除了让人犹豫，没有任何作用。
  系统内部照常用 7 个维度打分、照常跑硬约束闸门，但**只把结论和它的好处告诉用户**。
- **`epsilon`（伪选择阈值）仍在，但用在话术上**：top2 分差 < `epsilon` 时，
  收尾句说"这个选择不值得你想半小时，先去试试"，而不是多给一张牌。
- **敢说"别想了"仍是核心**：市面上几乎所有产品都在努力让你更纠结——
  纠结 = 停留时长 = 广告库存。**我们把纠结吃掉，而不是还给你。**

### 2.2 置信度 + 收尾一句
  （≥75%「这次，AI 替你拍板了 ✅」/ ≥50%「大方向定了，还有一点小变量」/ <50%「信息不多也能定，别在这上面耗了」）
- `most_uncertain`——哪个未知信息最影响结论（**只用于内部与调试，不逼用户补**）
- `ask_user`——**一句劝退纠结的收尾**："大方向没问题，细节不用抠了。"

**为什么这样设计**：低置信度不是缺陷，是诚实。但我们**不把不确定性转嫁给用户**——
不说"你补这条信息我立刻改答案"（那是在诱导二次纠结），
而是说"信息不多也能定，先去试试"。
可解释 = 可信 = 敢用；而**敢替你拍板** = 真的帮你省下了注意力。

每次决策落库 `decision_id`，事后回收 `{chosen, went, satisfaction 1-5, note}`：

- `profile.apply_feedback()` 按"实际选择 vs 推荐"、"满意度"微调维度权重
  （学习率 0.05，权重和归一化，单维限制在 [0.05, 0.35]）
- 档案变化**必须可查询**（前端「👤 我的档案」页实时展示）
- 用户可一键重置为新用户 / 载入老用户预设

**为什么这样设计**：模型谁都能调，**"我认识你"这件事，竞品要从零开始攒**。
用户换产品的成本不是订阅费，是重新养一个"认识自己"的系统——这个迁移成本比订阅费高得多。
而且数据主权在用户手里：随时能看到 AI 学到了什么，也能一键重置。**这才叫可信。**

---

## 3. 为什么必须用 mxagent（比赛评分重点）

> **Attention is what we save.** —— 这个标题不只是文案，它决定了技术选型：
> 要"省下人的注意力"，系统就必须**敢自己推演**（而不是把人当搜索引擎）、
> **敢记住你**（而不是每次从零开始）、**敢说"这条我不确定"**（而不是硬编一个答案）。
> 这三件事分别对应 mxagent 的层级递归、snapshot 长期记忆、以及可审计的决策轨迹。
> 对照市面框架（LangChain / LangGraph / 裸调 API），下面四个能力是**决策类产品的生死线**，
> 每一个都对应产品里的一个具体画面，不是抽象优势。
### 3.1 层级递归推演（`--level 2`）→ 每个选项一条时间线

```
mxagent --task "..." --level 2 --snapshot data/snapshots/<decision_id>/
```

- 编排 agent 把"推演每个选项的后果"委派给子 agent，**每个选项一条独立时间线**，
  推演到未来 48 小时的连锁影响（`OptionConsequence.effects`）。
- 框架哲学是**串行更准**：同一决策内多个选项串行执行，不同决策之间可并发。
- 这是前端等待页那六步（理解 → 推演今晚 → 推演未来48h → 权衡节律与通勤 → 安全校验 → 生成建议）的来源。
- **LangGraph 对照**：LangGraph 给你编排原语，但**不给你"层级决策专案组"的语义**。
  要自己造子 agent 生命周期、自己造 trace 格式、自己处理层级快照。

### 3.2 snapshot 长期记忆 + per-agent 可审计轨迹 → 可解释性的数据来源

```
<snapshot>.d/agents/*.jsonl     # 每个 agent 的完整决策轨迹
```

- **长期记忆**：档案（运动频次、偏好、约束、历史活动）跨决策持久化，
  这是"新用户 vs 老用户给出不同建议"的硬件基础。
- **可审计回放**：每次层级推演的每一步都落盘成 jsonl，前端「🔍 幕后：AI 是怎么想的」
  折叠面板直接把它渲染成树，每个节点可再折叠。
- **监管价值**：被问"你为什么推荐这个"，我们能把它走过的路一步一步摊开。
- **LangGraph 对照**：LangGraph 的 checkpointer 是给**图状态**用的，
  不是给"每个子 agent 的推理轨迹可回放"用的。要自己造一整套 trace schema。

### 3.3 censor 安全闸门 → 硬约束在**生成前**拦截

```
--custom_tools censor.py    # @add_censor 拦截工具调用
```

- 健康 / 预算 / 时间（`start_hint + duration_min + commute_min` 推算到家时刻 vs deadline）
  三类硬约束，**在生成前**拦截。
- 被拦选项 `feasible=false`，**structurally 当不上 GO**，并写入 `blocked` / `blocked_reason`。
- 信息不全时保留 `feasible=true`，但**必须降低 `confidence`** 并把缺口写入 `missing_info`。
- **LangGraph 对照**：自己拼框架只能在**生成后**过滤——
  "生成完了再删掉不合规选项"和"不合规选项根本没被生成出来"是两回事。
  后者才是安全，前者是补救。

### 3.4 result_check 自动质检 + agent2tool 能力复用 + cost_limit

- **`--result_check`**：自动校验输出的理由是否自洽，失败自动重试（架构约定：重试 1 次，再失败走降级）。
- **`agent2tool` / `--as_mcp`**：抽取器 / 推演器 / 解释器作为**可复用 agent 能力**分层组合，
  不是三个耦合的 Python 函数。换模型、换抽取策略不动推演器。
- **`--cost_limit`**：每次决策成本透明，可展示"这次建议花了 ¥0.03"。

### 3.5 一句话总结

> **通用框架给你"能跑"，mxagent 给我们"可解释、可审计、可降级、可复用"。**
> 而这四样，正是决策类产品能不能被人信任的分水岭。

---

## 4. 架构

```
                    ┌──────────────────────────────────────────────┐
                    │              用户输入（文字 / 语音）            │
                    │     web/index.html + app.js（原生 JS 无构建）  │
                    └───────────────────┬──────────────────────────┘
                                        │  POST /api/advise
                                        ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  server/app.py  (starlette + uvicorn, 0.0.0.0:8888)                 │
│  ┌────────────┐  ┌──────────────┐  ┌─────────────┐  ┌──────────────────┐  │
│  │ /api/parse │→ │ /api/advise  │→ │ /api/decision│→ │ /api/feedback   │  │
│  │ /api/asr   │  │ (后台线程)    │  │ /api/decisions│ │ /api/profile    │  │
│  └────────────┘  └──────┬───────┘  └─────────────┘  └──────────────────┘  │
└─────────────────────────┼─────────────────────────────────────────────────┘
                          │  server/engine_bridge.py（REAL / stub 自动切换）
                          ▼
╔═══════════════════════════════════════════════════════════════════════════╗
║  engine/  —  mxagent 决策引擎（5 段流程）                                   ║
║                                                                           ║
║   自然语言输入                                                              ║
║      │                                                                     ║
║      ▼  ① Extractor   LLM 结构化抽取 → DecisionRequest                     ║
║      │                 （选项、纠结点、硬约束、缺失信息）                    ║
║      ▼  ② Timeline    LLM(--level 2 委派) 每选项推演 48h → OptionConsequence║
║      │                                                                     ║
║      ▼  ③ Scorer      纯 Python 确定性打分 → 各维度分 + 总分               ║
║      │                 （LLM 不做算术，同样输入同样输出）                    ║
║      ▼  ④ Censor      纯函数硬约束闸门 → 违规选项 feasible=false            ║
║      │                 （健康 / 预算 / 安全，生成前拦截）                    ║
║      ▼  ⑤ Explainer   唯一结论 + 只讲优点 + 置信度 + 推理树 + 收尾一句      ║
║      │                                                                     ║
║      ▼                                                                     ║
║   ┌──────────┐   ┌──────────┐   ┌──────────┐                               ║
║   │            GO · 就它了（只有这一张）             │                     ║
║   │ 引用档案  │   │第三种可能│   │伪选择    │                               ║
║   └──────────┘   └──────────┘   └──────────┘                               ║
╚═══════════════════════════════════════════════════════════════════════════╝
                          │
                          ▼
              data/  （decisions / profiles / feedback.jsonl / snapshots）
```

**关键设计约束（不可妥协）**：

1. **LLM 只做理解与推演，算术一律在 Python**：分数、排名、通勤成本换算全部确定性计算，
   保证同样输入同样输出（可测试）。LLM 输出的数值只作为"主观权重估计"，且必须 clamp 到合法区间。
2. **降级可用**：mxagent 调用失败（超时/报错）→ 回退纯启发式模式 + `degraded: true`。
   **服务绝不能 500。**
3. **结构化输出容错**：task 里要求"只输出 JSON"，Python 侧剥 ```json 围栏、正则抓第一个 `{...}`、
   失败重试 1 次、再失败走降级。
4. **每次 LLM 调用必须带超时**（默认 120s；后端另有 150s 硬超时看门狗）。

---

## 5. 快速开始

### 5.1 起服务

```bash
# 已在常驻（pid 见 data/server.pid）。如需重启：
cd /home/Developer/workspace/choice
bash server/stop.sh          # 停止（优雅 6s，超时 kill -9）
bash server/run.sh           # 后台启动，日志 data/server.log，最多等 15s 健康检查
```

`server/run.sh` 实测行为：已在运行则不重复启动（提示 pid）；启动成功打印
`启动成功 pid=<PID>  日志: /home/Developer/workspace/choice/data/server.log`。

### 5.2 访问

```bash
# 健康检查
curl http://127.0.0.1:8888/api/health
# → {"ok":true,"mxagent_available":true,"version":"0.1.0",
#    "engine":{"advisor":"real","extractor":"real","profile":"real","models":"real"}}

# 浏览器
http://127.0.0.1:8888
```

`engine` 字段四个 key：`advisor` / `extractor` / `profile` / `models`，
值为 `real` 或 `stub`。engine 导入失败或调用异常时自动回落 stub（结构合法 +
`degraded=true`），服务永不 500。

### 5.3 跑测试

```bash
# 一键端到端（引擎单测 + API 集成 + 前端 smoke），约 10 秒
bash tests/run_all.sh

# 加跑真实 LLM 全链路（单条最长 300s，演示当天不要跑）
PICKONE_LLM=1 bash tests/run_all.sh

# 只测已在线的服务，不尝试启动
PICKONE_NO_START=1 bash tests/run_all.sh
```

最近一次实测（2026-09-27 11:50，engine = REAL）：

```
[1/3] 引擎单测   ：通过 23，失败 0，跳过 0
[2/3] API 集成   ：通过 26，失败 0，跳过 2
[3/3] 前端 smoke ：通过 18，失败 0，跳过 1
--------------------------------------------------------------
总计：通过 67，失败 0，跳过 3
结论：全部通过
```

开启真实 LLM 全链路慢用例后（`PICKONE_LLM=1 PICKONE_SLOW=1`）：

```
[1/3] 引擎单测   ：通过 23，失败 0，跳过 0
[2/3] API 集成   ：通过 28，失败 0，跳过 0
[3/3] 前端 smoke ：通过 18，失败 0，跳过 1
--------------------------------------------------------------
总计：通过 69，失败 0，跳过 1
结论：全部通过
```

### 5.4 演示当天早上：一键自检

```bash
bash tests/preflight.sh       # 约 2 秒，退出码 0=可走完整模式，1=需降级
```

检查 5 项：服务在跑 / LLM 端点可用 / 静态资源 200 / engine REAL 还是 stub / 本地 ASR 就绪。
末尾给出明确建议："可以走完整模式" 或 "建议走离线快速模式（原因：...）"。
**实测输出样例见 `demo/script.md` 第 0 节。**

### 5.5 切 mock 模式（纯离线演示）

```
http://127.0.0.1:8888/?mock=1
```

- 顶栏出现紫色 pill「演示数据」
- 内置假数据覆盖全部功能：单结论、置信度、维度条、推理树、打卡、档案、历史
- **零网络请求，断网可演**
- 前端 `web/app.js` 第 15 行 `var MOCK = /[?&]mock=1(&|$)/.test(location.search);`

### 5.6 切快速模式（不调 LLM，秒回）

两种方式：

1. **前端**：提交后等待超过 60 秒，等待页底部自动出现「⚡ 太久了，先用快速模式看看」按钮，点击即切。
2. **API**：`POST /api/advise` body 带 `"use_llm": false`。

```bash
curl -X POST http://127.0.0.1:8888/api/advise \
  -H 'Content-Type: application/json' \
  -d '{"text":"今晚去健身房跑步还是去楼下那家火锅店","user_id":"demo","use_llm":false}'
# → {"decision_id":"dec-...","status":"running"}   然后轮询 /api/decision/{id}
```

后端还有 150s 硬超时看门狗（`PICKONE_ADVISE_TIMEOUT` 可调）：
超时自动转降级结果并标注"AI 推理超过 150 秒未返回，已自动切换为离线快速模式"。

### 5.7 语音识别

```bash
# 本地 CLI（完全离线，faster-whisper small，461MB，CPU int8）
/home/Developer/workspace/.venv/bin/python asr/transcribe.py asr/.tmp/e1_workout.webm --lang zh
# → 今晚是去健身房跑步,还是去楼下那家火锅店。

# HTTP（后端已接通，multipart file 字段）
curl -X POST http://127.0.0.1:8888/api/asr -F "file=@asr/.tmp/e1_workout.webm" -F "lang=zh"
# → {"text":"今晚是去健身房跑步,还是去楼下那家火锅店。"}
```

契约：脚本不存在 / 超时 30s / 空输出 / 任何异常 → `200 {"text":"","unavailable":true}`，**绝不 500**。
详见 `asr/README.md`。

### 5.8 演示开关（新用户 vs 老用户）

```bash
# 新用户（空档案，默认权重）
curl -X POST http://127.0.0.1:8888/api/profile/reset \
  -H 'Content-Type: application/json' -d '{"user_id":"demo","preset":"new"}'

# 3 周老用户（goals: 每周运动3次；facts: 火锅店老板娘很热情/不吃辣/预算敏感/偏好步行15分钟能到的地方；9 条历史）
curl -X POST http://127.0.0.1:8888/api/profile/reset \
  -H 'Content-Type: application/json' -d '{"user_id":"demo","preset":"veteran"}'
```

前端在「👤 我的档案」页底部「🎛 演示开关」卡片有两个按钮，点击即切。

---

## 6. 目录结构

```
choice/
├── docs/
│   ├── ARCHITECTURE.md          # 唯一事实来源：定位、5 段流程、打分维度、验收标准
│   └── CONTEXT_FOR_TEAM.md      # 用户原始需求 + 比赛叙事
├── contracts/
│   ├── api.md                   # HTTP API 契约（端点表 + 行为要求）
│   └── data_model.md            # JSON Schema
├── engine/                      # 【引擎组】mxagent 决策引擎
│   ├── models.py                # 数据类（纯 stdlib）✅ 已就绪
│   ├── schema.py                # response_schema + 校验
│   ├── extractor.py             # 自然语言 → DecisionRequest
│   ├── scorer.py                # 确定性多维打分（纯 stdlib，不调 LLM）
│   ├── timeline.py              # 时间线推演（mxagent --level 委派）
│   ├── censor.py                # 安全/健康/预算闸门
│   ├── explainer.py             # 唯一 GO 牌 + 只讲优点 + 收尾一句
│   ├── advisor.py               # 编排入口 advise(request, profile) -> Decision
│   ├── profile.py               # 档案读写 + 满意度回灌
│   ├── cli.py                   # 命令行自测入口
│   └── agents/                  # mxagent config yaml（agent2tool 用）
├── server/                      # 【后端组】Web 服务
│   ├── app.py                   # starlette app，监听 0.0.0.0:8888
│   ├── engine_bridge.py         # REAL/stub 自动切换 + stub 兜底
│   ├── store.py                 # 持久化（JSON 文件，RLock + fcntl.flock）
│   ├── run.sh / stop.sh         # 启动 / 停止
│   └── SELFTEST.md              # 后端实测记录
├── web/                         # 【前端组】静态页面（原生 JS，无构建无 CDN）
│   ├── index.html               # 3 个 Tab：帮我选 / 我的档案 / 历史决策
│   ├── app.js                   # ~1068 行，含 mock 模式 / 快速模式 / 推理树渲染
│   ├── style.css                # 玻璃拟态深色主题
│   └── SELFTEST.md              # 前端实测记录
├── asr/                         # 本地离线语音识别（可选模块）
│   ├── transcribe.py            # CLI：stdout 仅文本，exit code 永远 0
│   ├── install.sh               # 幂等安装（依赖 + 模型下载）
│   ├── models/faster-whisper-small/   # model.bin 461MB
│   ├── .tmp/                    # 预录测试音频（e1_workout / e3_dinner 识别准确）
│   └── README.md / SELFTEST.md
├── demo/                        # 【演示素材】
│   ├── script.md                # 5 分钟现场演示台本（逐句讲解词 + 应急预案 + Q&A）
│   └── cases.json               # 8 个真实感纠结案例
├── tests/
│   ├── preflight.sh             # ⭐ 演示当天早上一键自检（2 秒出结论）
│   ├── run_all.sh               # 一键端到端测试
│   ├── test_engine.py / test_api.py
│   ├── selfcheck_web.py / smoke_web.py / smoke_app.js
│   ├── common.py                # 测试公共工具
│   └── LATEST_RUN.txt           # 测试历史趋势
├── data/                        # 运行期数据（gitignore）
│   ├── decisions/<id>.json      # 决策结果（外层包 status/result）
│   ├── profiles/<user_id>.json  # 用户档案
│   ├── feedback.jsonl           # 打卡流水
│   ├── snapshots/               # mxagent 层级推演快照
│   ├── uploads/                 # ASR 临时音频（用完即删）
│   ├── server.log / server.pid
│   └── locks/
└── README.md                    # 本文件
```

---

## 7. 当前状态与已知限制（诚实版）

### 7.1 engine 进度（2026-09-27 11:50 实测：**全部就绪，engine = REAL**）

`engine/` 全链路已落位并通过测试，`/api/health` 返回：

```json
{"ok":true,"mxagent_available":true,"version":"0.1.0",
 "engine":{"advisor":"real","extractor":"real","profile":"real","models":"real"}}
```

| 模块 | 状态 | 职责 |
|---|---|---|
| `models.py` | ✅ | 数据类（Option / DecisionRequest / ScoredOption / Card / Decision / Profile） |
| `schema.py` | ✅ | response_schema + 容错 JSON 解析（含终端折行修复） |
| `scorer.py` | ✅ | 确定性多维打分（**LLM 不算数**，同输入同输出） |
| `censor.py` | ✅ | 硬约束闸门（预算 / 截止时间） |
| `extractor.py` | ✅ | 自然语言 → DecisionRequest（LLM + 正则兜底） |
| `profile.py` | ✅ | 档案读写 + 满意度回灌 + new/veteran 预设 |
| `mxcli.py` | ✅ | mxagent CLI 封装（`MXAGENT_BIN` 可切换） |
| `timeline.py` | ✅ | 时间线推演（`--level 2` 层级 + 降级确定性推演） |
| `explainer.py` | ✅ | 唯一 GO 牌 + 只讲优点 + 置信度 + 推理树 |
| `advisor.py` | ✅ | 编排入口（任何输入都返回合法 Decision） |
| `cli.py` | ✅ | 命令行自测（`--demo` / `--text` / `--no-llm`） |

**实测行为**（2026-09-27 11:48，veteran 档案，真实 LLM，耗时 85s）：
- GO 牌理由引用档案证据：「你已经 4 天没运动了」「本周运动目标 3 次，目前只完成 1 次」
  「你的档案里记着：楼下火锅店的老板娘很热情」；
- 推理树 5 个真实节点（Extractor / Timeline / Scorer / Censor / Explainer）；
- 硬约束拦截生效：「按开始 18:30 + 240 分钟 + 单程通勤 40 分钟推算，到家约 23:10，
  晚于截止 22:30」→ 该选项 `feasible=false`；
- 后悔闭环生效：打卡后 `weights_used` 从默认值变为
  `{'rhythm': 0.208, 'social': 0.182, 'commute': 0.167, ...}`，重问同一问题推荐依据随之变化。

**stub 兜底仍在**：`server/engine_bridge.py` 在 engine 导入失败或调用异常时自动回落 stub
（结构合法 + `degraded=true`），保证服务永不 500。

- 本机 llama-server 是 `--parallel 1`，**随时可能被别人占满**。
- 完整推演模式设计预期 60~180s；后端有 150s 硬超时看门狗，前端有 60s 快速模式按钮。
- **演示当天早上务必跑 `bash tests/preflight.sh`**，它会实测 LLM 端点延迟。

### 7.3 ASR 依赖音频质量

- faster-whisper `small` 对口语/专有名词会有偏差：
  实测「在健身房练基本功」被误识为「地链机本功」。
- 要更准可换 `medium`（约 1.5GB，内存够但下载极慢——huggingface.co 不可达，需走 hf-mirror.com）。
- CPU int8 占 4 核，和 llama-server 并发时会互相拖慢。
- **演示只用 `asr/.tmp/e1_workout.webm` 和 `e3_dinner.webm`**（实测识别准确）。

### 7.4 公网映射当前不可用（重要）

- 文档记载公网映射为 `http://106.13.186.155:8051`。
- **2026-09-27 实测：该端口返回的是另一个应用 "Union Export Agent · 工业非标外贸工作台" 的页面
  （SPA fallback，所有路径都返回同一个 941 字节 HTML；POST 一律 405）。不是我们的服务。**
- 本机 frpc 由 root 运行（`/home/xsuper/frp/frpc.toml`，无读权限），无法确认映射配置。
- **演示一律以 `http://127.0.0.1:8888`（本机）或 `http://192.168.110.55:8888`（局域网）为准。** 若需公网，需重新配置 frp 映射。

### 7.5 其他已知限制

- **浏览器语音**：前端用 Web Speech API，**Firefox 不支持**（按钮置灰）。
  Chrome / Edge 可用。离线 ASR 走 `/api/asr`，不受浏览器限制。
- **服务重启会丢 running 任务**：决策任务是 daemon 线程。启动时有
  `sweep_stale_decisions()` 把遗留 running 标 failed，但**演示中不要重启服务**。
- **`/api/demo/cases` 目前返回 3 条**（`server/app.py` 的 `h_demo_cases` 硬编码）。
  完整 8 条在 `demo/cases.json`，更新方法见下节。
- **未知 API 路径 404**：starlette 默认返回纯文本 `Not Found`，
  契约要求 `{"ok":false,"error":"中文消息"}`。已有 `_not_found` handler 但挂在 app 级，
  部分路径（如 Mount 之外的 404）仍走框架默认。属测试已知偏差，不影响演示。
- **前端未做真实浏览器截图级验证**（动画、375px 溢出），仅代码级审查 + Node 冒烟。

---

## 8. 演示素材：如何把 `demo/cases.json` 更新到后端

> **不要改 `server/` 代码。** 本节说明如何把 `demo/cases.json` 的 8 条案例同步到后端
> `GET /api/demo/cases`（当前返回 3 条硬编码样例）。

### 8.1 文件格式

`demo/cases.json` 结构：

```json
{
  "meta": { "...": "格式说明，不要透传给前端" },
  "cases": [
    {
      "id": "workout_vs_hotpot_vs_movie",
      "title": "健身房 vs 火锅 vs 看电影",
      "text": "今晚好纠结：方案一去健身房跑步，单程通勤40分钟……选哪个好？",
      "highlight": "用户原始需求那条，主案例。……"
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 唯一，小写蛇形 |
| `title` | string | ≤20 字，前端下拉框显示（超出被截断为 26 字符 + …） |
| `text` | string | 中文自然语言长句，>60 字，含通勤/花费/时间/社交/纠结点 |
| `highlight` | string | **仅演示者阅读**，预期展示点，**不要**放进 API 响应 |

### 8.2 后端需要的格式

`GET /api/demo/cases` → `{"ok":true,"items":[{"id","title","text"}]}`

即：取 `cases` 数组，**只保留 `id` / `title` / `text` 三个字段，丢掉 `highlight` 和 `meta`**。

### 8.3 更新方式（三选一）

**方式 A：改 `server/app.py` 的 `h_demo_cases`（推荐，最直接）**

把 `items = [...]` 那 3 条硬编码替换为从 `demo/cases.json` 读取：

```python
import json
from pathlib import Path

_CASES_FILE = Path(__file__).resolve().parent.parent / "demo" / "cases.json"

def _load_demo_cases():
    try:
        data = json.loads(_CASES_FILE.read_text(encoding="utf-8"))
        items = [
            {"id": c["id"], "title": c["title"], "text": c["text"]}
            for c in data.get("cases", [])
            if c.get("id") and c.get("title") and c.get("text")
        ]
        if items:
            return items
    except Exception as e:  # noqa: BLE001
        log.warning("[demo/cases] 读取 demo/cases.json 失败，用内置样例: %r", e)
    return [ ...原有的 3 条兜底... ]
```

**方式 B：启动时加载一次（避免每次请求读文件）**

在 `app.py` 模块级 `DEMO_CASES = _load_demo_cases()`，handler 里直接返回。

**方式 C：让前端直接读 `demo/cases.json`（零后端改动）**

`web/app.js` 的 `loadDemoCases()` 已有兜底：拉不到 `/api/demo/cases` 就用内置 3 条。
可以让后端把 `demo/` 也挂成静态目录，前端 fetch `/demo/cases.json`。
（需要改挂载配置，属 server 改动，按方式 A 更干净。）

### 8.4 验证

```bash
# 更新后重启服务，然后：
curl -s http://127.0.0.1:8888/api/demo/cases | \
  /home/Developer/workspace/.venv/bin/python -c "
import json,sys
d=json.load(sys.stdin)
items=d['items']
print('条数:', len(items))
for it in items:
    assert it.get('id') and it.get('title') and it.get('text'), it
    assert 'highlight' not in it, 'highlight 不该透传'
    print(' -', it['id'], '|', it['title'], '|', len(it['text']), '字')
print('OK')
"
```

预期：`条数: 8`，且每条都含 `id`/`title`/`text`、不含 `highlight`。

**本地预校验（不依赖服务，直接读 `demo/cases.json`）：**

```bash
/home/Developer/workspace/.venv/bin/python -c "
import json
raw = json.load(open('demo/cases.json', encoding='utf-8'))['cases']
items = [{'id': c['id'], 'title': c['title'], 'text': c['text']} for c in raw]
print('条数:', len(items))
for it in items:
    assert it.get('id') and it.get('title') and it.get('text'), it
    assert 'highlight' not in it, 'highlight 不该透传'
    print(' -', it['id'], '|', it['title'], '|', len(it['text']), '字')
print('OK')
"
```

实测输出：

```
条数: 8
 - workout_vs_hotpot_vs_movie | 健身房 vs 火锅 vs 看电影 | 135 字
 - gym_card_vs_treadmill | 办健身卡 vs 买跑步机 | 129 字
 - delivery_vs_cook | 点外卖 vs 回家做 | 111 字
 - study_vs_family | 学英语 vs 陪爸妈 | 88 字
 - shopping_vs_save | 看中的鞋买不买 | 99 字
 - party_vs_rest | 同事聚餐去不去 | 95 字
 - book_vs_video | 看书 vs 刷视频 | 82 字
 - early_vs_sleep | 再玩一局还是睡 | 95 字
OK
```

> 契约要求 `text` 中文非空 >20 字，本文件 8 条最短 82 字，全部满足。
> 主案例是第 1 条 `workout_vs_hotpot_vs_movie`（健身房 / 火锅 / 看电影三选项）。

---

## 9. 演示入口

| 场景 | 地址 | 说明 |
|---|---|---|
| **现场演示（首选）** | **http://127.0.0.1:8888**（本机）/ **http://192.168.110.55:8888**（局域网） | 不依赖外网 |
| 离线演示（断网保险） | http://127.0.0.1:8888/?mock=1 | 内置假数据，零请求 |
| 公网映射 | http://106.13.186.155:8051 | ⚠️ **当前不可用**，见 7.4 |
| 演示台本 | `demo/script.md` | 逐句讲解词 + 应急预案 + 评委 Q&A |
| 演示案例库 | `demo/cases.json` | 8 条真实感纠结案例 |
| 演示前自检 | `bash tests/preflight.sh` | 2 秒出结论 |

---

## 10. 环境约定（给开发者）

- **Python 必须用 venv 解释器**：`/home/Developer/workspace/.venv/bin/python`（3.13.15）
  - ⚠️ 系统 `python3` 是 3.12，**不能** import mxagent 的 site-packages（pathlib/collections 报错）
- **不要 pip install 新东西**（网络受限）。venv 已有：`starlette` `uvicorn` `websockets`
  `markdown_it` `pydantic` `jsonschema` `aiohttp` `numpy` `av` `ctranslate2` `faster-whisper`
- **模型**：`qwen3.8-flash-next`（本机唯一可用模型，vision + thinking 均支持）
- **LLM 端点**：`http://127.0.0.1:8080/v1`（llama-server，token 在 `~/.my_tokens.yaml` 的 `endpoint` 段）
- **mxagent 可执行**：`/home/Developer/workspace/.venv/bin/mxagent`（0.1.123）
- **服务监听**：`0.0.0.0:8888`（`PICKONE_HOST` / `PICKONE_PORT` 可覆盖）
- **引擎降级开关**：环境变量 `MXAGENT_BIN`（默认 `mxagent`）。
  `MXAGENT_BIN=/nonexistent/mxagent` → 必须返回 `degraded:true` 的合法 Decision，绝不抛异常。

---

## 11. 验收标准对照（docs/ARCHITECTURE.md 第 7 节）

| # | 标准 | 状态 |
|---|---|---|
| 1 | `bash tests/run_all.sh` 全绿 | ✅ 67 过 0 败（开 `PICKONE_LLM=1 PICKONE_SLOW=1` 后 69 过 0 败） |
| 2 | `python engine/cli.py --demo` 产出合法 Decision JSON | ✅ 实测（含唯一 GO 牌、confidence、reasoning_tree） |
| 3 | 服务常驻 8888，浏览器可提交并看到结果 | ✅ 实测 |
| 4 | mxagent 不可用时仍返回结果且 `degraded:true`，不 500 | ✅ 实测（`MXAGENT_BIN=/nonexistent/mxagent`） |
| 5 | 打卡后同一问题重问，推荐或理由变化 | ✅ 实测（`weights_used` 与理由均变化） |

---

*README 基于 2026-09-27 实测状态编写。所有命令均已实际运行验证。*
