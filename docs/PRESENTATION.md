# PickOne 比赛陈述文档

> ## Attention is what we save.
> **把注意力，花在值得的事情上。**

| 项 | 内容 |
|---|---|
| 项目 | PickOne — 可解释决策引擎 |
| 底座框架 | mxagent v0.1.123（NVIDIA MMPLEX team in-house 分层多智能体框架） |
| 运行环境 | 赞奇 DGX Spark（NVIDIA GB10），hostname `spark-4cf2` |
| LLM 来源 | 本地 llama-server `Qwen3.8-Flash-Next-UD-Q4_K_XL` + stepfun API |
| 交付规模 | 53 个文件 / 9.39MB / 67 项测试全绿 / 67.7s 演示视频 |
| 代码仓库 | `github.com/mathisonwang/pickone` |

本文分两部分：**第一部分**介绍我们脚下的地基——mxagent 框架；**第二部分**讲清楚这个项目是怎么被做出来的。产品设计、五段决策流程与七维打分表的细节见 `docs/REPORT.md`，参赛心路见 `docs/ESSAY.md`，本文不重复。

---

# 第一部分　mxagent 框架介绍

## 1.1 定位：一个纯 CLI 的智能体框架

mxagent 是 **NVIDIA MMPLEX team 的 in-house agent 框架**，**自 2025 年 3 月起开发**，形态是**纯 CLI agent**——一个命令行程序就是全部，没有服务端、没有控制台、没有消息队列。它的设计目标很直接：**做领先市面上所有的 CLI agent**。

这个定位不是营销话术，而是可以被逐条验证的工程事实。当前版本 **0.1.123**，**2.4MB 纯 Python，无重依赖**。一个 CLI 程序里塞进了层级多智能体、会话快照、工具闸门、技能系统、MCP 集成和一整套 Python API。

## 1.2 基础能力

| 能力 | 用法 | 说明 |
|---|---|---|
| 多模型选择 | `--model` / `--list models` | 本机可用 `qwen3.8-flash-next`；`--temperature` / `--max_tokens` 可调 |
| 交互 / 非交互 | `--task` 缺省即进 REPL | `--task "..."` 一次一答，适合脚本化；不带则进入交互模式 |
| 可定制 system prompt | `--rules` / `--common_rules` | 前者挂到每个（子）agent，后者共享给**所有** agent 含子 agent |
| 智能上下文管理 | 超阈值自动压缩 | 压缩策略由 `prompts/summarize.md` 定义，对用户透明 |
| 配置 YAML 化 | `--config` + `--parameter` + `includes` | 变量替换、多文件组合，`--dump_config` 可导出当前生效配置 |
| 结果自检 | `--result_check` | 布尔判定，成功 exit 0、失败 exit 1，可直接接 CI |

## 1.3 工具集

| 类别 | 工具 / 参数 |
|---|---|
| 文件系统 | read / write / grep / find，`--workspace` 沙箱 + 只读环境变量 |
| 系统命令 | `--system` 白名单模式（不给参数则全放开，框架明确标注 dangerous） |
| Python 执行 | `--execute file.py` 跑脚本；`python_run` 跑代码片段 |
| 托管终端 | `tmux`：命名终端、后台命令、交互输入、原始 scrollback |
| 图片读取 | `image_reader`（多模态） |
| 邮件 | `send_mail`，默认需人工确认 |
| 等待 / 定时 | `wait` / `wait_until` |
| 任务规划 | `task_planner` / `task_planner_workflow`（显式任务图 + 状态转移） |
| 终止 | `terminate`（让 LLM 自己决定收工） |

## 1.4 独特能力：领先体现在哪

### ① 层级多智能体（Hierarchical Multi-Agent）

这是 mxagent 的核心。每个 agent 都能实例化能力相同的子 agent，形成一棵智能体树。

- **`--level 0~3` 控制递归分解深度**：level 0 是纯执行器（不生成子 agent），level 越高越倾向把任务拆开。每一层用不同的 system prompt 引导分解策略，同层行为一致。
- **严格的 root-to-leaf 通信协议**：子 agent 遇到疑问只能向父提问（`request_task_info`），父**必须**应答；答不上就向自己的上级传，最终到 Root Agent 转给用户——**人机闭环**。子 agent 永远不许直接打扰用户。
- **静态团队模式**：`--level team.yaml` 用 YAML 定义固定组织结构（`sub_agents` / skills / rules / tools 白名单 / model / temperature），manager 只能把活派给预定义成员，不能临时造人。
- **委派模型策略**：`--allowed_models` / `--cost_limit` 限制子 agent 能用什么模型，把成本控制做进框架而不是靠人盯。

### ② Snapshot 会话保存 / 恢复

- `--snapshot x.json`：文件不存在则创建，存在则**恢复并续写**。
- **递归保存子 agent 状态**——委派出去的工作也能检查和恢复，这是大多数框架做不到的。
- 支持 checkpoint 分叉：官方用法是 `cp current.json good_checkpoint.json` 手动复制快照文件，探索跑偏后覆盖回来即可回到已知良好状态。
- 配套 `<snapshot>.d/agents/*.jsonl` **append-only 全量轨迹**，上下文被压缩过也不丢历史。
- 离线工具三件套：`--show_agents_hierarchy`（看智能体树）、`--dump_subagent`（导出某个 agent 的完整轨迹）、`--visualize`（快照可视化）。

### ③ Agent-as-Tool：把 agent 变成函数

`agent2tool(config.yaml)` 把一个 mxagent 配置变成一个可调用的 Python 函数，入参出参由 YAML 里的 JSON Schema 约束。三种用法：executor 里手动调、注册成工具（`tools.add`）、`--tools xxx.yaml` 直接注册。

更进一步，`--as_mcp config.yaml` 把 agent 暴露成 **MCP server**，任何支持 MCP 的客户端（Cursor 等）都能直接调用。配合能力分层组合（build tree → find source file → debug tree build failure），agent 可以像乐高一样堆。

### ④ Censor：工具调用前闸门

```python
def censor(name, argv, kwargs) -> Optional[str]:  # 返回字符串即拒绝并说明原因
```

在工具**执行前**拦截，返回非空字符串就是拒绝并附上理由。还能做预处理（例如先 `p4 open` 再允许编辑）。这实现了**策略执行而不改工具输入输出**——与"生成完再过滤"是两回事，后者是补救，前者才是安全。

### ⑤ Skill 系统

`--skills dir` 加载技能目录，`learn_skill` 工具让 LLM **按需逐层学习**——先看目录，再读单个技能，不一次性灌满上下文。支持 Python 单文件或包目录，**docstring 即文档**；当学到可调用函数时自动注册为工具。另有 `--anthropic_skills` 兼容 Anthropic 标准的 `SKILL.md`。

### ⑥ MCP 集成

`--stdio_mcp "cmd"` / `--http_mcp url`，多服务器并发，工具自动发现并注入。

### ⑦ Executor Python API

`mxagent()` / `mxagent_async()` / `async_run()` / `return_answer()` / `get_snapshot()` / `restore_snapshot()` / `cancel_current_task()` / `interrupt_current_task()` / `change_model()` / `get_token_usage()` / `tools` / `ToolsGroup` / `add_censor` / `add_rule` / `Image` / `Audio` / `DoNotSplit`。**这把 agent 变成了可编程的工作流单元**——能在 Python 里创建、取消、打断、换模型、查用量。

## 1.5 安全设计

| 层面 | 机制 |
|---|---|
| 无服务端 | 纯 CLI，token 存本地 `~/.my_tokens.yaml`（chmod 600），key 不进日志 |
| key 解析顺序 | `$MXAGENT_API_KEY` → token file 中匹配 endpoint name 的条目 → endpoint 自带 api_key |
| workspace 沙箱 | 默认只写 workspace、只读全盘；`MXAGENT_READ_ONLY` / `MXAGENT_WRITE_PATH` 细粒度控制 |
| 工具闸门 | censor 前置于每次工具调用 |
| 命令白名单 | `--system` 显式授权 |

## 1.6 工程细节

版本 0.1.123；`--dump_config` 导出配置；`--test_tools` 工具自测；`--test-models` 模型可用性测试；`--visualize` 快照可视化；自带 `validation/` 回归测试目录。

除此之外还有几处见功夫的地方：`--confirm` 可以指定哪些工具需要人工确认；`--exclude_tools` 反向裁剪工具面；`--save_answer` 把最终答案单独落盘（`agent2tool` 就靠它拿结果）；`--response_schema` 用 JSON Schema 约束输出结构；argcomplete 全量补全，长参数也能 Tab 出来。这些单独看都不起眼，合在一起意味着**这个 CLI 是可以被脚本、被 CI、被别的程序严肃接管的**。

## 1.7 为什么好用

| 优点 | 含义 |
|---|---|
| 零配置启动 | 一个 CLI，token 放好就能用 |
| 递归分解是自动的 | 不用手写 DAG，agent 自己判断复杂度 |
| 状态可保存可回放 | 长任务跨天续跑，出问题能回到已知良好状态 |
| 能力可复用 | agent 变函数、变 MCP server，像乐高一样堆 |
| 安全可控 | 沙箱 + censor + token 本地化 |
| 可编程 | Python API 让 agent 进工作流 |

把这六条连起来读，会发现它们指向同一件事：**mxagent 把"智能体"从一次性脚本变成了可运维的工程对象。** 能启动（零配置）、能拆解（自动递归）、能续跑（快照）、能复用（agent-as-tool）、能兜住（沙箱与闸门）、能编排（Python API）。市面上多数框架解决了第一点和第二点，后四点往往要使用者自己补——而补的部分才是长任务真正的成本所在。

---

# 第二部分　项目完成过程

## 2.1 环境事实

| 项 | 实测值 |
|---|---|
| 机器 | 赞奇 DGX Spark，GPU NVIDIA GB10，hostname `spark-4cf2` |
| mxagent | v0.1.123，装在 `/home/Developer/workspace/.venv` |
| LLM 端点 | `~/.my_tokens.yaml` active endpoint 为 `stepfun`（`https://api.stepfun.com/step_plan/v1`） |
| 本地模型 | llama-server 跑 `Qwen3.8-Flash-Next-UD-Q4_K_XL`，`--parallel 1`，262144 context |

## 2.2 开发历程

**阶段 0 · 环境与框架。** 通读 mxagent 文档（epub）与本机安装的框架源码，实测验证 7 项关键机制：非交互调用、`result_check` 退出码、censor 拦截、level 2 层级与 per-agent JSONL 轨迹、异步工具挂起等待、团队 YAML 模式、venv 解释器陷阱（系统 `python3` 是 3.12，import 不了 mxagent 的 site-packages）。

**阶段 1 · 契约先行。** 动手前先写三份文档作为唯一事实来源：`docs/ARCHITECTURE.md`（5 段决策流程、7 维打分表、6 条不可妥协约束）、`contracts/api.md`（12 个端点 + 异步任务模式 + "永不 500"）、`contracts/data_model.md`（Option / DecisionRequest / Consequence / ScoredOption / Card / Decision / Profile）。

**阶段 2 · 并行开发（5 个子 agent）。**

| 子 agent | 职责 |
|---|---|
| `engine_dev` | mxagent 决策引擎：抽取 → 时间线推演 → 确定性打分 → censor 闸门 → 三张牌解释器 |
| `backend_dev` | starlette 服务 + REAL/stub 自动切换 bridge |
| `frontend_dev` | 手机优先深色 UI、语音输入、AI 推演动画、三张牌、可折叠推理树 |
| `qa_demo` | unittest 套件 + 健壮性测试 + 5 分钟现场演示台本 |
| `asr_dev` | 本地 whisper 离线语音识别 |

**阶段 3 · 集成与 debug（主 agent 亲自做）。** 前端资源 404（相对路径 vs `/static/*`）；404/405 返回非 JSON；重启后任务永久卡 running（加启动清扫）；LLM 慢无兜底（150s 硬超时 + 前端快速模式按钮）；`--model qwen3.8-flash-next` 让 mxagent 直接退出 1（alias 能被 `--list` 列出却不能传给 `--model`，且失败时不写 snapshot）；LLM JSON 解析 100% 失败（终端折行在 JSON 字符串中间插真实换行，还把 `"start_hint"` 污染成 `"sta rt_hint"`）；veteran 档案"最近跳舞在 1 天前"与核心演示矛盾（改成 4 天前）。

**阶段 4 · 视频制作（4 个子 agent）。** 技术验证：Chrome headless + CDP 逐帧录屏 + edge-tts 配音 + ffmpeg 合成；脚本分镜：8 镜结构 + 金句 + 禁用词表；录屏：fetch 拦截注入预跑数据（结果页 1.5s 出 vs 真实 43s）；配音合成：23 句 YunjianNeural + BGM + 字幕烧录。

**阶段 5 · 反复打磨（用户反馈驱动）。** 三张牌 → 只给一个结论（产品最重要的判断）；案例从"跳舞"换成"健身/火锅/看电影"（代码 120 处 + 文档 92 处）；标题定为 Attention is what we save.；视频解说词从技术视角改成用户视角（删去 python / mxagent / LLM 等词）；节奏：edge-tts padding 裁切（1.19s → 0.213s，提速 5.6 倍）；静音加速：分段线性映射表，17 段全部 ≤3.5s；字幕逗号 bug：ASS Format 行漏声明 `MarginV`，被 libass 吞掉分隔逗号；产品文案收敛为"答案有了，去就行。"

**阶段 6 · 交付。** GitHub 仓库清理（578M → 9.39MB，移除视频中间产物），产出报告书、征文与演示视频。

## 2.3 交付数据

| 指标 | 数值 |
|---|---|
| 仓库文件数 | 53 个（git 跟踪） |
| 仓库体积 | 9.39MB（清理前 578MB） |
| 测试 | 67 项通过 / 0 失败；开启真实 LLM 慢用例后 69 项通过 |
| 演示视频 | 67.7s，1440×900 @ 30fps，H.264 + AAC |
| 代码仓库 | `github.com/mathisonwang/pickone` |

## 2.4 框架能力落在产品上的位置

陈述框架不能只罗列参数，要说清它到底替我们省了什么。下表是本次开发中**真实用到**的 mxagent 能力与它们解决的工程问题：

| mxagent 能力 | 在项目中的实际用法 | 省掉的工作 |
|---|---|---|
| `--level 2` 层级委派 | 每个选项一条独立时间线，推演到未来 48h | 不用手写子 agent 生命周期与调度 |
| `--snapshot` + `<snapshot>.d/agents/*.jsonl` | 每次决策落盘，前端推理树直接渲染 | 不用自己设计 trace schema 与回放格式 |
| `--censor` 工具调用前闸门 | 预算 / 截止时间硬约束在生成结论前拦截 | 不用在生成后打补丁过滤 |
| `agent2tool` / `--as_mcp` | 抽取器 / 推演器 / 解释器各自沉淀为 YAML 配置 | 换模型不动推演器，能力可分层组合 |
| `--result_check` | 自动验收，失败 exit 1 可直接接脚本 | 不用写判空与人工抽检 |
| `--config` + `--parameter` | 三个 agent 配置（`extractor` / `timeline` / `explainer`）各自沉淀为 YAML | 同一能力既能 CLI 跑，也能被当工具调 |
| `MXAGENT_BIN` 环境变量 | 指向不存在的可执行文件即整体降级 | 韧性演示不需要改代码 |
| workspace 沙箱 + 只读环境变量 | 5 个并行子 agent 各写各的目录，互不越界 | 不需要额外的权限隔离层 |

最后一行值得多说一句：**并行开发的安全性不是靠"叮嘱 agent 别改别人文件"保证的，是靠框架的沙箱保证的。** 这是 in-house 框架相对"自己拼 LangChain"最实际的差别——约束在基础设施里，不在提示词里。

## 2.5 几点复盘

**契约先行的收益是滞后的。** 阶段 5 把案例从"跳舞"整体换成"健身/火锅/看电影"，grep 命中代码 120 处、文档 92 处，但因为字段名早在 `contracts/data_model.md` 里定死，动的只有数据，没有一处接口签名。如果当初边写边定，这次替换会变成一次重构。

**框架的边界要实测，不能读文档。** `--model qwen3.8-flash-next` 让 mxagent 直接退出 1——这个 alias 能被 `--list models` 正常列出，却不能作为 `--model` 的实参，且失败时不写 snapshot。这类细节文档不会写，只有真跑一次才知道。阶段 0 那 7 项机制实测，是后面所有架构决策的地基。

**工具的错误要复核。** 视频阶段有两次典型的"工具犯错"：编辑工具把几行代码重复插入，反而制造了新的语法错误；`build_retime_video.py` 读的是中间产物 mp4，而新帧在帧目录里，根本没进成片，导致成片从 67.7s 退回 90s。共同点是**"你以为的输入"和"实际的输入"不是同一个东西**——所以每一轮自动化产出都必须有人（或另一个 agent）核对，不能只看退出码是 0。

---

## 致谢

**感谢 https://www.stepfun.com/ 提供的 token** —— 让整个项目的 LLM 推理成为可能。

**感谢赞奇提供的 DGX Spark** —— 让 agent 有了一台可以自己动手的家。

整个项目是在 Spark 上运行 mxagent、接入 stepfun 提供的 API，让 agent 自己在 Spark 上完成开发：从本地部署 model，到项目实现、测试、提交、录制视频，全部由 agent 自主完成。
