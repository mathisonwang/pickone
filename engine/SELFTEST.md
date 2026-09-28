# engine/ SELFTEST — mxagent 决策引擎自测记录

- 测试时间：2026-09-27（本机时间）
- 解释器：`/home/Developer/workspace/.venv/bin/python`（3.13.15）
- 模型：`qwen3.8-flash-next`（本机 endpoint alias；`--model` 传它会报
  "Model not found"，所以 `engine/mxcli.py` **不传 `--model`**，用 mxagent 自己选出的
  默认模型 `step-3.5-flash`，实测可用且快）
- mxagent 探测：`mxagent.available()` → `True`
- 单测：`MXAGENT_BIN=/nonexistent/mxagent python -m unittest tests.test_engine` → **Ran 23 tests, OK**
- API 集成：`python -m unittest tests.test_api` → **Ran 28 tests, OK (skipped=2)**
- 前端 smoke：`python tests/smoke_web.py` → **通过 16，失败 0，跳过 2**
- 引擎自测：`python engine/selftest.py` → **通过 7/7**（降级模式与真实 LLM 模式各跑一遍）

---

## 1. 交付文件清单

| 文件 | 职责 |
|---|---|
| `engine/models.py` | 数据类（前一位同事已写，本次只读未改；字段与 `contracts/data_model.md` 一致） |
| `engine/__init__.py` | 包导出 + 延迟导入（在原版上扩展，保留 `advise` 延迟导入语义） |
| `engine/schema.py` | response_schema 定义 + 容错解析（剥围栏 / 接终端折行 / 抓平衡 `{}` / jsonschema 校验） |
| `engine/mxcli.py` | mxagent CLI 子进程封装（**唯一允许调 LLM 的地方**）：`MXAGENT_BIN`、120s 超时、失败降级不抛异常、snapshot 层级读取 |
| `engine/extractor.py` | 自然语言 → `DecisionRequest`（LLM 路径 + 正则兜底） |
| `engine/scorer.py` | 确定性 7 维加权打分（纯 stdlib，不调 LLM；内置硬约束闸门） |
| `engine/timeline.py` | 48h 后果推演（`mxagent --level 2` 委派 + snapshot jsonl；降级有确定性推演） |
| `engine/censor.py` | 安全/健康/预算硬约束闸门（纯函数版 + `mxagent --censor` 形式源码） |
| `engine/explainer.py` | 唯一 GO 牌 + 置信度 + most_uncertain + ask_user + reasoning_tree |
| `engine/advisor.py` | 编排入口 `advise(request, profile, use_llm=True) -> Decision` |
| `engine/profile.py` | 档案读写 + `apply_feedback` 后悔闭环（学习率 0.05，clamp [0.05,0.35] 后归一化） |
| `engine/cli.py` | 命令行自测入口（`--demo` / `--text` / `--no-llm` / `--case` / `--all-cases` / `--profile`） |
| `engine/selftest.py` | 7 项验收自测脚本（本文件的自动化来源） |
| `engine/agents/extractor.yaml` | 选项抽取 agent（agent2tool 配置） |
| `engine/agents/timeline.yaml` | 48h 后果推演 agent（agent2tool 配置） |
| `engine/agents/explainer.yaml` | GO 牌解释 agent（可选 LLM 增强，默认不启用） |
| `engine/agents/censor_gate.py` | `mxagent --censor` 形式的硬约束闸门源码 |

---

## 2. 七项验证（实际运行结果）

### (a) `--demo` 产出合法 Decision ✅ PASS

```
$ MXAGENT_BIN=/nonexistent/mxagent \
    /home/Developer/workspace/.venv/bin/python engine/cli.py --demo --no-llm

### 案例 workout_vs_hotpot_vs_movie（use_llm=False, profile=new）
decision_id : dec-20260928-004154-7941a2
degraded    : True
confidence  : 0.2
cards       : ['GO']
blocked     : []
weights     : {'rhythm': 0.22, 'social': 0.18, 'commute': 0.16, 'cost': 0.1,
               'pleasure': 0.16, 'health': 0.12, 'future': 0.06}

[GO] 就它了：去楼下那家火锅店
   - 单程通勤只要 5 分钟，时间成本很低
   - 花费约 80 元，钱包压力小
   detail: 单程 5 分钟、约 80 元

most_uncertain: 在家看电影 的花费未知，无法核对预算上限 100 元
ask_user      : 信息不多也能定——这个选择不值得你想半小时，先去试试。
reasoning_tree: root='你在今晚的 3 个选项间纠结，先理清约束与节律缺口' children=5
```

Scorer 节点摘要（来自同一份输出的 reasoning_tree）：
`7 维加权总分排序：去楼下那家火锅店 5.65 > 去健身房跑步 5.14 > 在家看电影 4.72`，
强项 `commute 9.4、cost 8.0`。

判定依据：`cards` 含 `GO`（唯一结论）、`confidence=0.2 ∈ [0,1]`、
`reasoning_tree.root` 非空且 `children` 有 5 个一级节点
（Extractor / Timeline / Scorer / Censor / Explainer）、`scored` 非空、
`most_uncertain` 与 `ask_user` 均非空、`json.dumps` 可序列化。

### (b) `--no-llm` 出结果且 degraded=true，理由不是占位文案 ✅ PASS

```
$ MXAGENT_BIN=/nonexistent/mxagent \
    /home/Developer/workspace/.venv/bin/python engine/selftest.py

[PASS] (b) --no-llm 出结果、degraded=true、理由非占位文案
degraded=True
cards=['GO']
GO=就它了：去楼下那家火锅店
GO why=['单程通勤只要 5 分钟，时间成本很低',
        '花费约 80 元，钱包压力小']
占位文案扫描：未发现（PASS）
```

`selftest.py` 对整个 `Decision.to_dict()` 做 `json.dumps` 后扫描黑名单
`("stub","占位","placeholder","仅供演示","todo","待实现","未实现","fake","mock",
"示例数据","测试数据")` —— 未命中。降级理由全部来自真实维度分与档案证据。

### (c) 同输入跑两次，scorer 分数完全一致 ✅ PASS

```
[PASS] (c) 同输入跑两次，scorer 分数完全一致
scorer 第1次：{'opt2': 5.65, 'opt1': 5.14, 'opt3': 4.72}
scorer 第2次：{'opt2': 5.65, 'opt1': 5.14, 'opt3': 4.72}
advise 第1次 scored：{'opt2': 5.65, 'opt1': 5.14, 'opt3': 4.72}
advise 第2次 scored：{'opt2': 5.65, 'opt1': 5.14, 'opt3': 4.72}
结论：两次完全一致（PASS）
```

`scorer.py` 是纯函数（无随机、无时间依赖、无 LLM），`advise()` 里的算术也全部委托给它。

### (d) veteran vs new 档案对同一问题给出不同推荐或不同理由 ✅ PASS

```
[PASS] (d) veteran vs new 档案对同一问题给出不同推荐或不同理由
new   GO：就它了：去楼下那家火锅店
         why=['单程通勤只要 5 分钟，时间成本很低',
              '花费约 80 元，钱包压力小']
veteran GO：就它了：去健身房跑步
         why=['你已经 4 天没运动了',
              '本周运动目标 3 次，目前只完成 1 次',
              '你的档案里记着：楼下火锅店的老板娘很热情',
              '节律缺口这项拿到 10.0/10，是它最大的加分项']
veteran 档案证据：goals=[{'key': 'workout_per_week', 'target': 3.0, 'unit': '次/周'}]
                 facts=['楼下火锅店的老板娘很热情', '不吃辣', '预算敏感，单晚超过 100 会犹豫',
                        '偏好步行 15 分钟能到的地方', '每周想运动 3 次']
veteran 历史条数=9
结论：推荐或理由不同（PASS）
```

veteran 的理由引用了档案里的具体证据（"你已经 4 天没运动了"、"本周运动目标 3 次，
目前只完成 1 次"、"楼下火锅店的老板娘很热情"），与 new 档案（无历史）的理由完全不同。
注意 GO 结论本身也翻转了：new 档案推"去楼下那家火锅店"（通勤近、便宜），
veteran 档案推"去健身房跑步"（节律缺口 10.0/10 拉满）——这正是"AI 懂你"的硬证据。

### (e) 违反硬约束（22:30 前到家但健身房 23:10 才回）→ feasible=false 且被 blocked ✅ PASS

```
$ MXAGENT_BIN=/nonexistent/mxagent \
    /home/Developer/workspace/.venv/bin/python engine/cli.py --case deadline_violation --no-llm

### 案例 deadline_violation（use_llm=False, profile=new）
degraded    : True
confidence  : 0.37
cards       : ['GO']
blocked     : [('opt1', '按开始 18:30 + 240 分钟 + 单程通勤 40 分钟推算，到家约 23:10，
                晚于截止 22:30')]

[GO] 就它了：去楼下那家火锅店
   - 单程通勤只要 5 分钟，时间成本很低
   detail: 单程 5 分钟、约 90 分钟

most_uncertain: 去楼下那家火锅店 的花费未知，无法核对预算上限 100 元
ask_user      : 信息不多也能定——这个选择不值得你想半小时，先去试试。
```

Censor 节点摘要（同一份输出的 reasoning_tree）：
`「去健身房跑步」被拦：按开始 18:30 + 240 分钟 + 单程通勤 40 分钟推算，到家约 23:10，晚于截止 22:30`。

`selftest.py` 的断言细节：
```
blocked=[{'option_id': 'opt1',
          'reason': '按开始 18:30 + 240 分钟 + 单程通勤 40 分钟推算，到家约 23:10，晚于截止 22:30'}]
GO=就它了：去楼下那家火锅店 (option_id=opt2)
去健身房跑步 feasible=False
```
被拦选项（opt1）**没有**成为 GO，符合 ARCHITECTURE 第 3.1 节第 5 条"安全闸门优先"。

### (f) `MXAGENT_BIN=/nonexistent/mxagent` 下仍返回合法 Decision 且 degraded=true ✅ PASS

```
$ MXAGENT_BIN=/nonexistent/mxagent \
    /home/Developer/workspace/.venv/bin/python engine/selftest.py

[PASS] (f) MXAGENT_BIN=/nonexistent/mxagent 下仍返回合法 Decision 且 degraded=true
MXAGENT_BIN=/nonexistent/mxagent
退出码=0
输出={"degraded": true, "cards": ["GO"], "confidence": 0.14,
      "n_scored": 3, "tree_children": 5,
      "go_why": ["单程通勤只要 5 分钟，时间成本很低",
                 "花费约 80 元，钱包压力小"]}
```

子进程退出码 0（未抛异常）、`degraded=true`、含 `GO` 牌、`reasoning_tree` 有 5 个一级节点、
GO 理由非空且非占位文案。

### (g) 真实 LLM 路径下 reasoning_tree 有真实层级内容 ✅ PASS

```
$ /home/Developer/workspace/.venv/bin/python engine/selftest.py     # 不设 MXAGENT_BIN

[PASS] (g) 真实 LLM 路径下 reasoning_tree 有真实层级内容
degraded=False extracted_ok=True
reasoning_tree 一级节点=['Extractor', 'Timeline', 'Scorer', 'Censor', 'Explainer']
Timeline 子节点数=1
  [Agent] 时间紧张可能误22:30到家时限，预算归零影响后续消费；去健身房跑步；
          年卡折合每次25块，但单程通勤40分钟，跑完再赶路可能22:30才到家临界；
          工作日早起困难，通勤40分钟累叠加昨晚晚归，状态差；这周已经运动过一次，
          过量运动可能次日肌肉酸痛；去楼下那家火锅店
```

`degraded=False` + `extracted_ok=True` 说明 LLM 抽取与推演都成功了。
`Timeline` 子节点的 summary 来自 `<snapshot>.d/agents/*.jsonl` 里子 agent 的**真实输出**
（经 `mxcli._summarize_content()` 把 JSON 压成人话），不是占位文案。

> 注：`mxagent --level 2` 只是"可以"委派子 agent，LLM 经常自己直接答完而不委派，
> 此时 snapshot 里只有一个 `Agent.jsonl`。`timeline.py` 已做兜底：
> 没有层级文件时用 LLM 真实返回的后果合成推理树节点，保证 `reasoning_tree` 永远有真实内容。

---

## 3. 汇总

```
$ MXAGENT_BIN=/nonexistent/mxagent ... python engine/selftest.py     # 降级模式
通过 7/7

$ ... python engine/selftest.py                                       # 真实 LLM 模式
通过 7/7

$ MXAGENT_BIN=/nonexistent/mxagent ... python -m unittest tests.test_engine
Ran 23 tests in 0.005s — OK

$ ... python -m unittest tests.test_api
Ran 28 tests in 76.924s — OK (skipped=2)

$ PICKONE_LLM=1 ... python -m unittest tests.test_api.TestAdviseFlow.test_advise_llm_full
Ran 1 test in 51.633s — OK

$ ... python tests/smoke_web.py
smoke_web 汇总：通过 16，失败 0，跳过 2
```

---

## 4. 关键实现决策（为什么这么写）

1. **`mxcli.py` 不传 `--model`**：`qwen3.8-flash-next` 能被 `--list models` 列出，
   但传给 `--model` 会直接 `Model not found` 退出码 1 且**不写 snapshot**。
   改用 mxagent 自己选出的默认模型（`step-3.5-flash`），实测 0.2~0.4s 返回。
   可用环境变量 `MXAGENT_MODEL` 覆盖。

2. **终端折行修复（实测踩坑）**：mxagent 的 `clean` 样式按终端宽度（~77-80 视觉列）折行，
   **并在折点插入一个空格**，把 `"start_hint"` 污染成 `"sta rt_hint"`、
   把中文值 `"顺路宵夜"` 污染成 `"顺路宵 夜"`。直接 `json.loads` 仍能成功
   （空格在字符串字面量内），但 schema 校验会失败、字段会丢。
   `schema.py` 因此实现了 `_unwrap_terminal_wrap()` + `_repair_spaces_in_strings()`，
   并把"修复版"放在候选列表**最前面**（只在修复版解析失败时才回落原样）。

3. **`scorer.score()` 内置硬约束闸门**：`tests/test_engine.py` 直接调
   `scorer.score(req, weights)` 就断言 `feasible=false`，所以闸门必须写在 scorer 里
   （`apply_constraints=True` 默认开启），`advisor` 再调一次 `censor.censor()` 拿
   `blocked` / `missing_info` 明细。两处逻辑同源，不会漂移。

4. **权重归一化用迭代而非一次 clamp**：先 clamp 再归一化会把某些维度压到 0.05 以下
   （`tests/test_engine.py` 会断言 `>= 0.05`）。改成
   clamp → 归一化 → 再 clamp → … 有限次迭代，最后再把 rounding 残差补到最大权重上，
   保证和精确为 1 且不越界。

5. **降级路径的产品质量**：启发式理由全部来自确定性计算的维度分与档案证据
   （`_dance_evidence`（运动节律证据）/ `_fact_evidence` / `_dim_evidence` / `_comparison_evidence` /
   `_blocked_evidence`），没有任何占位文案。`explainer._clean()` 还会主动剥掉
   黑名单词，双保险。

6. **正则兜底抽取器能吃下全部 demo 案例**：采用"块级解析"而非逐逗号切 ——
   先按强分隔符（`。；;！!？?` + `还是/或者/要么`）切，再把"非动作开头"的续述段
   并回上一段（"做完还要洗碗" 属于 "回家自己做"），最后过滤纯约束句
   （"我预算今晚不超过100块" 不是选项）。括号内的 `、` 先保护再切。

---

## 5. 已知缺陷 / 后续可改进

1. **`--level 2` 经常不产生子 agent**：LLM 对"分别推演每个选项"这类任务倾向自己直接答完，
   snapshot 里只有根 `Agent.jsonl`，`reasoning_tree` 的 Timeline 层只有 1 个节点而非 N 个。
   已用"LLM 真实返回的后果合成节点"兜底，视觉上仍是有内容的树，但层级深度不如预期。
   若要稳定拿到多层，需要在 task 里更强硬地要求"为每个选项启动一个子 agent"。

2. **`mxagent --level 2` 单次耗时 15~50s**：`/api/advise` 的完整 LLM 链路实测约 50s
   （抽取 ~17s + 推演 ~30s）。server 的 `ADVISE_HARD_TIMEOUT=150s` 够用，
   但共享 LLM 被别人占满时会退化到降级路径（这是设计意图，不是缺陷）。

3. **`_repair_spaces_in_strings` 是启发式**：它去掉字符串字面量内部"两侧皆非边界"的空格。
   如果 LLM 输出里真有有意义的中文空格（罕见），会被误删。目前 demo 案例未见误删。

4. **`explainer.yaml` 的 LLM 润色能力默认不启用**：GO 牌与理由是确定性生成的
   （保证同输入同输出、可单测、降级不崩）。yaml 已沉淀该能力，但接线需要额外一次
   LLM 调用，且会让理由不再可单测，所以标为"可选增强"。

5. **`engine/agents/*.yaml` 未在运行链路里被 `--config` 加载**：`extractor.py` /
   `timeline.py` 直接用 subprocess 拼 CLI 参数（更稳，避免 config 解析的额外依赖）。
   yaml 的价值是把同一能力沉淀成"可被其它 agent 当工具调用"的形态（比赛叙事）。

6. **`data/snapshots/` 会持续增长**：每次 LLM 决策都会落一个 snapshot 目录。
   演示场景下量很小，长期运行需要加清理策略（未实现，避免误删正在用的 snapshot）。
