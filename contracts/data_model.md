# 数据模型契约 (engine/models.py)

纯 stdlib dataclass + `to_dict()` / `from_dict()`。**不引入 pydantic**，保证任何解释器可导入。
所有字段名即 JSON 字段名，前后端共用。

```python
class Option:
    id: str                 # "opt1"
    label: str              # "去健身房跑步"
    category: str           # workout | dance | theme_park | food | social | rest | study | other
    #                       # （workout=运动健身：健身/跑步/游泳/瑜伽等；dance 仍保留供舞蹈类）
    start_hint: str         # "19:00" 可为空
    duration_min: int       # 预计时长（分钟），未知填 -1
    place: str              # 地点名，可为空
    commute_min: int        # 单程通勤分钟，未知填 -1
    cost_cny: float         # 预估花费（人民币），未知填 -1
    social_value: int       # 0-10 主观社交价值（能见到想见的人=8~10）
    pleasure: int           # 0-10 期待感
    health_load: int        # 0-10 体力/健康负担（越累越高）
    workout: bool           # 是否算一次锻炼（跑步/健身/游泳等）
    depends_on: list[str]   # 与其他选项互斥/依赖的 option id
   纠结点: list[str]         # -> 字段名用 concerns: list[str]，用户口述的纠结点
    after: list[str]        # 结束后顺带可做（如"附近好吃的"）
    notes: str

class DecisionRequest:
    raw_text: str           # 用户原话
    when: str               # "今晚" / "周六下午" / ISO 时间，可为空
    options: list[Option]
    hard_constraints: list[str]   # "22:00 前必须到家"、"预算不超过 200"
    missing_info: list[str]       # 系统认为缺的关键信息
    extracted_ok: bool      # LLM 抽取是否成功（false = 走了兜底）

class Consequence:
    option_id: str
    horizon: str            # "48h"
    effects: list[dict]     # [{"time":"tonight","desc":"...","impact":-2..2}, ...]
    future_score: int       # 0-10，对未来安排的正面程度
    risk: str               # 主要风险描述，可为空

class ScoredOption:
    option: Option
    dims: dict              # {"rhythm":8.0,"social":9.0,"commute":3.0,...} 0-10
    total: float            # 加权总分 0-10
    feasible: bool
    blocked_reason: str     # censor 命中原因，空表示未拦
    consequence: Consequence | None
    reasons: list[str]      # 人话理由（引用档案证据）

class Card:
    kind: str               # 只可能是 "GO"（产品主张：只给一个选项）
    option_id: str | None   # 兜底建议（没抽出选项）时可为 None
    title: str              # 大标题，如"就它了：去健身房跑步"
    why: list[str]          # 理由（<=4 条，每条 <=40 字）
    detail: str             # 补充说明/行动建议

class Decision:
    decision_id: str
    created_at: float
    request: DecisionRequest
    scored: list[ScoredOption]      # 按 total 降序
    cards: list[Card]               # 恒为长度 1 的 [GO]；不产出 SWITCH / DROP
    confidence: float               # 0-1
    most_uncertain: str             # 最影响结论的未知项
    ask_user: str                    # 收尾一句（劝退纠结），不是提问
    reasoning_tree: dict            # {"root": "...", "children":[{"agent":"...","summary":"...","children":[...]}]}
    degraded: bool                  # true = LLM 不可用，走启发式
    blocked: list[dict]             # [{"option_id","reason"}]
    weights_used: dict              # 本次使用的维度权重（可解释）

class Profile:
    user_id: str
    nickname: str
    goals: list[dict]       # [{"key":"dance_per_week","target":3,"unit":"次/周"}]
    #                       # （key 名沿用 dance_per_week，语义是"每周运动次数"）
    history: list[dict]     # [{"date","activity","category","duration_min","mood":1-5}]
    weights: dict           # 维度权重（见 ARCHITECTURE 第 4 节）
    facts: list[str]        # 长期事实，如"楼下火锅店的老板娘很热情"、"不吃辣"
    updated_at: float
```

## 序列化规则
- 所有类提供 `to_dict()`（递归，None 保留为 null）与 `@classmethod from_dict(d)`（缺字段用默认值，不抛异常）。
- 未知字段忽略，不报错（前后端版本容忍）。
- `Decision.to_dict()` 必须 JSON 可序列化（无 set / 无自定义对象）。
