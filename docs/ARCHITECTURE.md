# PickOne — 选择困难辅助 Web App

> 本文件是所有实现者的**唯一事实来源 (single source of truth)**。
> 若实现与本文件冲突，以本文件为准；需要改契约必须先改本文件。

## 0. 一句话定位

用户用自然语言（文字或语音）说清"今晚/这周要面对的几个选项 + 各自的纠结点"，
系统综合**用户长期档案（运动频次、偏好、约束）**、**选项间的相互影响**、
**对未来安排的连锁后果**，给出可解释的建议，并在事后回收满意度让推荐越用越准。

## 1. 运行环境（务必照此实现，不要自行探测）

- 工作目录（项目根）：`/home/Developer/workspace/choice`
- mxagent 可执行：`/home/Developer/workspace/.venv/bin/mxagent` （已在 PATH 中，`mxagent` 可直接调用）
- **跑 mxagent 的 Python 必须用 venv 的解释器**：`/home/Developer/workspace/.venv/bin/python`（3.13）
  - ⚠️ 系统 `python3` 是 3.12，**不能** import mxagent 的 site-packages（会 pathlib/collections 报错）。
  - 任何需要 `MxAgentLib` / `common.*` 的代码，只能用 venv python 运行。
- 纯标准库的模块（打分器、数据模型）在两个解释器下都能跑。
- 可用第三方库（venv 内已存在）：`starlette`、`uvicorn`、`websockets`、`markdown_it`、`pydantic`、`jsonschema`、`aiohttp`。
  - **不要** pip install 新东西（网络受限且浪费时间）。
- 模型：`qwen3.8-flash-next`（本机唯一可用模型，vision + thinking 均支持）。
- LLM token 已配置好（`~/.my_tokens.yaml`），直接调用 mxagent 即可，无需额外参数。
- **服务监听地址（硬性要求）**：`0.0.0.0:8888`
- 已验证事实：`mxagent --task "..." --style clean --not_allow_ask` 可非交互跑通；
  `--result_check "..."` 成功返回 exit 0；`--custom_tools x.py` 里 `@add_censor` 能拦截工具调用；
  `--level 2` 会产生子 agent 层级并写入 `<snapshot>.d/agents/*.jsonl`。

## 2. 目录结构（严格遵守）

```
choice/
├── docs/ARCHITECTURE.md          # 本文件
├── contracts/                    # 接口契约（JSON Schema 与 API 说明）
│   ├── data_model.md
│   └── api.md
├── engine/                       # 【引擎组】mxagent 决策引擎
│   ├── __init__.py
│   ├── models.py                 # 数据类（纯 stdlib，无第三方依赖）
│   ├── schema.py                 # response_schema 定义 + 校验
│   ├── extractor.py              # 自然语言 -> DecisionRequest（调 mxagent）
│   ├── scorer.py                 # 确定性多维打分（纯 stdlib，不调 LLM）
│   ├── timeline.py               # 时间线推演（mxagent --level 委派）
│   ├── censor.py                 # 安全/健康/预算闸门（mxagent --censor 形式 + 纯函数版）
│   ├── explainer.py              # 生成唯一的 GO 牌 + 只讲优点 + 收尾一句
│   ├── advisor.py                # 编排入口：advise(request, profile) -> Decision
│   ├── profile.py                # 用户档案读写 + 满意度回灌
│   ├── cli.py                    # 命令行自测入口
│   └── agents/                   # mxagent config yaml（agent2tool 用）
├── server/                       # 【后端组】Web 服务
│   ├── app.py                    # starlette app，监听 0.0.0.0:8888
│   ├── store.py                  # 持久化（JSON 文件，线程安全）
│   └── run.sh                    # 启动脚本
├── web/                          # 【前端组】静态页面（由 server 挂载）
│   ├── index.html
│   ├── app.js
│   └── style.css
├── data/                         # 运行期数据（profile/decisions/feedback）
├── tests/                        # 端到端测试
│   ├── test_engine.py
│   ├── test_api.py
│   └── run_all.sh
└── README.md
```

## 3. 决策流程（5 段，引擎组实现）

```
自然语言输入
  ① Extractor   LLM 结构化抽取 -> DecisionRequest（含选项、纠结点、硬约束、缺失信息）
  ② Timeline    LLM(level 委派) 每个选项推演到未来 48h 的后果 -> OptionConsequence
  ③ Scorer      纯 Python 确定性打分（LLM 不做算术）-> 各维度分 + 总分
  ④ Censor      纯函数硬约束闸门（健康/预算/安全），违规选项降级并说明
  ⑤ Explainer   唯一结论（GO）+ 只讲优点 + 置信度 + 可展开推理树 + 劝退纠结的收尾句
```

### 3.1 关键设计约束（不可妥协）

1. **LLM 只做理解与推演，算术一律在 Python**：分数、排名、通勤成本换算全部 `scorer.py` 确定性计算，
   保证同样输入同样输出（可测试）。LLM 输出的数值只作为"主观权重估计"，且必须 clamp 到合法区间。
2. **只给一个选项**（产品核心，2026-09-27 修订，用户明确要求）：
   - 只输出一张 `GO` 牌：一个选项 + 它的优点（引用档案证据，如"你已经 4 天没运动了"）。
   - **不产出 SWITCH / DROP**。展示多个选项本身就是内耗，与产品目标矛盾。
   - 信息不全也无所谓：抽不出选项就从原话抓一个，再不行给低成本默认建议
     （"先出门走走"），并配一句"信息不多也能定：先动起来，比继续想更有价值"。
   - `epsilon`（伪选择阈值）仍用于**收尾话术**：top2 分差 < `epsilon` 时，
     收尾句说"这个选择不值得你想半小时"，而不是多给一张牌。
3. **置信度 + 最不确定项**：每次输出必须带 `confidence`(0-1) 与 `most_uncertain`（哪个未知信息最影响结论），
   并给出"你补充这条信息我立刻改答案"的具体提问。
4. **可解释**：`reasoning_tree` 字段完整保留层级推演过程，前端直接渲染成树。
5. **安全闸门优先**：`censor` 命中时，被拦选项不得成为 GO，且必须在 `blocked` 中如实说明。
   但**不向用户展示被拦选项的清单**（那会诱导"要不要放宽约束"的二次纠结）。
6. **降级可用**：若 mxagent 调用失败（超时/报错），引擎必须回退到"纯启发式模式"
   （只用 extractor 的正则兜底 + scorer），并设置 `degraded: true`。**服务绝不能 500。**

## 4. 打分维度（scorer.py，权重来自 profile，可被满意度反馈调整）

| 维度 key | 含义 | 默认权重 |
|---|---|---|
| `rhythm` | 生活节律缺口（如每周想运动 3 次、已 4 天没动 → 高分） | 0.22 |
| `social` | 社交价值（见朋友、陪伴） | 0.18 |
| `commute` | 通勤/时间成本（越低越好，取负） | 0.16 |
| `cost` | 花费（越低越好） | 0.10 |
| `pleasure` | 愉悦度/期待感 | 0.16 |
| `health` | 健康与体力（疲劳、天气、身体状况） | 0.12 |
| `future` | 对未来 48h 安排的连锁影响（由 Timeline 给出） | 0.06 |

- 每维 0~10 分，加权求和 → `total`。
- 硬约束不满足 → 该选项 `feasible=false`，不参选。
- `epsilon`（伪选择阈值）默认 `1.2`（总分的 ~6%）。
- **两条产品修正**（2026-09-28，修复"说 4 天没运动却推荐在家看电影"的矛盾）：
  - `commute` / `cost` 设"零成本区"：通勤 `<=15min`、花费 `<=30元` 一律视为同等便利
    （不再线性加分）。否则"什么都不做"（0 分钟通勤、0 花费）会永远压过
    "步行 5 分钟、人均 80"——躺平永远赢。
  - `rhythm` 缺口到阈值（`>=9`）时给运动类选项一个**硬性偏好加分**（+0.8）。
    一周该动 3 次却 4 天没动是长期目标，"零通勤零花费"是短期便利；
    线性加权下三项短期便利很容易压过单项长期目标，导致系统自相矛盾。
    阈值判断比加权更接近真实决策心理：欠账太多时优先补课。

## 5. 满意度回灌（后悔闭环）

- 每次决策落库 `decision_id`，24h 后前端提示打卡（demo 里可手动触发）。
- 打卡数据 `feedback {decision_id, chosen, went, satisfaction 1-5, note}`。
- `profile.apply_feedback()`：按"实际选择 vs 推荐"、"满意度"微调维度权重
  （学习率 0.05，权重和归一化，单维权重限制在 [0.05, 0.35]）。
- 档案变化必须可查询（前端展示"你的档案学到的偏好"）。

## 6. mxagent 调用规范（引擎组）

- 统一走 CLI 子进程（最稳，避免 event loop 冲突）：
  `/home/Developer/workspace/.venv/bin/mxagent --task <task> --model qwen3.8-flash-next --style clean --not_allow_ask --level <0|2> --snapshot <path>`
- 结构化输出：在 task 里明确要求"只输出 JSON"，并在 Python 侧用 `schema.py` 校验 + 容错解析
  （剥 ```json 围栏、正则抓第一个 `{...}`、失败重试 1 次、再失败走降级）。
- 需要层级推演时用 `--level 2` + 独立 snapshot 目录（`data/snapshots/<decision_id>/`），
  并把 `<snapshot>.d/agents/*.jsonl` 的层级信息整理进 `reasoning_tree`（这是"幕后可视化"的数据来源）。
- 每次 LLM 调用必须带超时（默认 120s）与失败降级。
- 并发：同一决策内多个选项的推演**串行**执行（框架哲学是串行更准），但不同决策之间可并发。

## 7. 验收标准（Definition of Done）

1. `bash tests/run_all.sh` 全绿。
2. `python engine/cli.py --demo` 用内置"健身房跑步 vs 楼下火锅 vs 在家看电影"样例产出合法 Decision JSON，
   含唯一 GO 牌、置信度、reasoning_tree。
3. 服务在 `0.0.0.0:8888` 常驻（监听所有网卡），浏览器打开 `http://127.0.0.1:8888` 可提交问题并看到结果。
4. mxagent 不可用时（人为把 mxagent 路径改错）服务仍返回结果且 `degraded:true`，不 500。
5. 反馈打卡后，同一问题重问，推荐或理由发生变化（闭环生效）。
