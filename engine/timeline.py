"""engine/timeline.py — 时间线推演：每个选项推演到未来 48h 的后果。

两条路径：
1. **LLM 路径**（use_llm=True 且 mxagent 可用）：
   `mxagent --level 2` 让子 agent 分层推演，snapshot 落在
   `data/snapshots/<decision_id>/`，并把 `<snapshot>.d/agents/*.jsonl`
   的层级信息整理进 reasoning_tree（ARCHITECTURE 第 6 节）。
2. **降级路径**：纯确定性启发式推演（不调 LLM），保证 Consequence 结构非空。

LLM 只做"理解与推演"，算术（future_score 归一化、impact clamp）仍在 Python。
"""

from __future__ import annotations

from typing import Any

from .models import Consequence, DecisionRequest, Option, clamp

# ---------------------------------------------------------------- 确定性推演（降级路径）

#: 类别 → 未来 48h 的基础连锁影响（确定性）
_CAT_FUTURE = {
    "dance": 7, "workout": 7, "theme_park": 5, "food": 6, "social": 7,
    "rest": 7, "study": 7, "other": 5,
}

#: 类别 → 典型风险描述
_CAT_RISK = {
    "dance": "运动后肌肉酸痛、可能晚归影响第二天状态",
    "workout": "运动后肌肉酸痛、可能晚归影响第二天状态",
    "theme_park": "花费高、排队久、晚归导致第二天疲劳",
    "food": "吃太多影响消化、预算容易超",
    "social": "社交时长不可控、容易熬过头",
    "rest": "补觉太久反而更昏、当天一事无成",
    "study": "新爱好可能三分钟热度、占用休息时间",
    "other": "结果不确定，取决于现场状态",
}


def _heuristic_consequence(option: Option, profile=None) -> Consequence:
    """确定性推演一个选项的未来 48h 后果（不调 LLM）。"""
    cat = (option.category or "other").lower()
    effects: list[dict] = []

    # ---- 今晚 ----
    if cat in ("dance", "workout"):
        effects.append({"time": "今晚", "desc": "出一身汗，本周锻炼额度 +1，情绪释放",
                        "impact": 2})
        if (getattr(option, "commute_min", -1) or -1) >= 40:
            effects.append({"time": "今晚",
                            "desc": f"单程通勤 {option.commute_min} 分钟，路上耗时明显",
                            "impact": -1})
    elif cat == "theme_park":
        effects.append({"time": "今晚", "desc": "体验值高，但园区大、排队久，体力消耗大",
                        "impact": 1})
        if (getattr(option, "cost_cny", -1) or -1) >= 250:
            effects.append({"time": "今晚",
                            "desc": f"花费约 {option.cost_cny:g} 元，本月预算压力增加",
                            "impact": -2})
    elif cat == "food":
        effects.append({"time": "今晚", "desc": "吃到想吃的东西，心情会变好", "impact": 1})
        if (getattr(option, "cost_cny", -1) or -1) >= 100:
            effects.append({"time": "今晚",
                            "desc": f"人均约 {option.cost_cny:g} 元，预算敏感时要掂量",
                            "impact": -1})
    elif cat == "rest":
        effects.append({"time": "今晚", "desc": "身体得到恢复，疲劳感下降", "impact": 2})
        effects.append({"time": "今晚", "desc": "但今晚没有任何社交与新鲜输入",
                        "impact": -1})
    elif cat == "study":
        effects.append({"time": "今晚", "desc": "学到新东西，有成长感", "impact": 1})
        effects.append({"time": "今晚", "desc": "新爱好需要持续投入，否则容易搁置",
                        "impact": -1})
    else:
        effects.append({"time": "今晚", "desc": "按计划完成，结果中性", "impact": 0})

    # ---- 用户提到的"顺带可做" ----
    for a in (getattr(option, "after", None) or [])[:2]:
        effects.append({"time": "今晚", "desc": f"顺带：{a}", "impact": 1})

    # ---- 明天 ----
    if cat in ("dance", "workout"):
        effects.append({"time": "明天", "desc": "可能肌肉酸痛，但睡眠质量通常更好",
                        "impact": 1})
    elif cat == "theme_park":
        effects.append({"time": "明天", "desc": "大概率疲劳，上午状态受影响", "impact": -2})
    elif cat == "rest":
        effects.append({"time": "明天", "desc": "精力恢复，明天办事效率更高", "impact": 1})
    else:
        effects.append({"time": "明天", "desc": "对明天安排影响有限", "impact": 0})

    # ---- 通勤/晚归的连锁 ----
    if (getattr(option, "commute_min", -1) or -1) >= 45:
        effects.append({"time": "明天",
                        "desc": "通勤远导致今晚睡得晚，第二天起床更痛苦", "impact": -1})

    # ---- future_score：确定性计算 ----
    future = float(_CAT_FUTURE.get(cat, 5))
    # 档案事实：偏好步行 15 分钟内 → 通勤远的选项未来分下降
    facts = [str(f) for f in (getattr(profile, "facts", None) or [])]
    for f in facts:
        if ("步行" in f and "15" in f) and (getattr(option, "commute_min", -1) or -1) > 20:
            future -= 1.5
        if ("预算敏感" in f or "不吃辣" in f) and cat == "food" \
                and (getattr(option, "cost_cny", -1) or -1) >= 100:
            future -= 1.0
    # 纠结点里提到"累/晚" → 未来分下降
    for c in (getattr(option, "concerns", None) or []):
        if any(w in c for w in ("累", "晚", "远", "熬")):
            future -= 0.5

    return Consequence(
        option_id=option.id,
        horizon="48h",
        effects=effects[:6],
        future_score=int(clamp(round(future), 0, 10)),
        risk=_CAT_RISK.get(cat, ""),
    )


# ---------------------------------------------------------------- LLM 路径

_TIMELINE_TEMPLATE = """你是"PickOne"App 的后果推演器。请对下面每个选项推演**未来 48 小时**的连锁后果。

{task}

用户原话：
---
{raw}
---

待推演的选项：
{options}

要求：
1. 只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字。
2. options 数组与输入选项一一对应，label 必须与输入完全一致（用于匹配）。
3. effects 每条包含 time（"今晚"/"明天"/"后天上午"）、desc（一句中文后果，<=40字）、
   impact（-2~2 的整数，负面的写负数）。
4. future_score 是 0~10 整数：该选项对未来 48h 安排的正面程度。
5. risk 是主要风险（<=30字），没有则空字符串。

JSON 结构：
{{"options":[{{"label":"","effects":[{{"time":"","desc":"","impact":0}}],"future_score":5,"risk":""}}]}}"""


def _match_effects(obj: dict, options: list[Option]) -> dict[str, Consequence]:
    """把 LLM 输出的 options 数组按 label 匹配回 option_id。"""
    out: dict[str, Consequence] = {}
    llm_opts = obj.get("options") if isinstance(obj, dict) else None
    if not isinstance(llm_opts, list):
        return out
    # 建 label → option 索引（容忍轻微差异：去空格后包含匹配）
    by_label: dict[str, Option] = {}
    for o in options:
        by_label[str(o.label).strip()] = o

    for item in llm_opts:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        opt = by_label.get(label)
        if opt is None:
            # 模糊匹配：互相包含
            for o in options:
                if label and (label in o.label or o.label in label):
                    opt = o
                    break
        if opt is None:
            continue
        effects = []
        for e in (item.get("effects") or []):
            if isinstance(e, dict):
                desc = str(e.get("desc") or e.get("description") or "").strip()
                if not desc:
                    continue
                effects.append({
                    "time": str(e.get("time") or "之后")[:12],
                    "desc": desc[:60],
                    "impact": int(clamp(e.get("impact", 0), -2, 2)),
                })
            elif isinstance(e, str) and e.strip():
                effects.append({"time": "之后", "desc": e.strip()[:60], "impact": 0})
        fs = item.get("future_score", 5)
        try:
            fs = int(clamp(fs, 0, 10))
        except (TypeError, ValueError):
            fs = 5
        out[opt.id] = Consequence(
            option_id=opt.id, horizon="48h", effects=effects[:6],
            future_score=fs, risk=str(item.get("risk") or "")[:60])
    return out


def infer(request: DecisionRequest, profile=None, use_llm: bool = True,
          decision_id: str = "", timeout: float = 120.0
          ) -> tuple[dict[str, Consequence], dict, bool]:
    """推演所有选项的未来 48h 后果。

    :return: (consequences, tree_info, degraded)
        - consequences: {option_id: Consequence}
        - tree_info: {"snapshot": str, "children": [...]}（reasoning_tree 的 Timeline 部分）
        - degraded: True = 走了降级路径
    """
    options = list(getattr(request, "options", None) or [])
    if not options:
        return {}, {"snapshot": "", "children": []}, (not use_llm)

    if use_llm:
        try:
            from . import mxcli

            if mxcli.available():
                from . import schema as _schema

                opt_lines = "\n".join(
                    f"- {o.label}（类别 {o.category}，通勤 {o.commute_min} 分钟，"
                    f"花费 {o.cost_cny} 元，时长 {o.duration_min} 分钟）"
                    for o in options)
                task = _TIMELINE_TEMPLATE.format(
                    task=_schema.schema_prompt("timeline"),
                    raw=str(getattr(request, "raw_text", ""))[:2000],
                    options=opt_lines)
                snap = mxcli.snapshot_dir_for(decision_id or "timeline") \
                    if decision_id else ""
                obj, res = mxcli.run_json(task, "timeline", level=2,
                                          snapshot=snap, timeout=timeout)
                if obj is not None:
                    cons = _match_effects(obj, options)
                    if cons:
                        # LLM 可能自己直接答完而不委派子 agent（level 2 只是"可以"委派），
                        # 此时 <snapshot>.d/agents/ 只有一个 Agent.jsonl。
                        # 兜底：没有层级文件时，用 LLM 真实返回的后果合成推理树节点，
                        # 保证 reasoning_tree 永远有真实内容（前端要渲染）。
                        tree = mxcli.snapshot_tree(snap) if snap else {"children": []}
                        children = list(tree.get("children") or [])
                        if not children:
                            children = [{
                                "agent": "Timeline·" + (options[0].label if options else "推演"),
                                "summary": _summary_of(cons[options[0].id])
                                           if options and options[0].id in cons
                                           else "LLM 已推演全部选项的未来 48h 后果",
                                "children": [],
                            }]
                            for o in options[1:]:
                                c = cons.get(o.id)
                                children.append({
                                    "agent": "Timeline·" + o.label,
                                    "summary": _summary_of(c) if c else "已推演",
                                    "children": [],
                                })
                        return cons, {"snapshot": snap, "children": children}, False
        except Exception:
            pass

    # ---- 降级：确定性推演 ----
    cons = {o.id: _heuristic_consequence(o, profile) for o in options}
    children = [{
        "agent": f"Timeline·{o.label}",
        "summary": _summary_of(cons[o.id]),
        "children": [],
    } for o in options]
    return cons, {"snapshot": "", "children": children}, True


def _summary_of(c: Consequence) -> str:
    """把 Consequence 压成一句话（给 reasoning_tree 用）。"""
    if not c.effects:
        return f"未来 48h 影响评分 {c.future_score}/10"
    first = c.effects[0]
    txt = f"{first.get('time', '')}：{first.get('desc', '')}"
    if c.risk:
        txt += f"；风险：{c.risk}"
    return txt[:120]


# 兼容别名
def timeline(request, profile=None, use_llm=True, decision_id=""):
    return infer(request, profile=profile, use_llm=use_llm,
                 decision_id=decision_id)


def run(request, profile=None, use_llm=True, decision_id=""):
    return infer(request, profile=profile, use_llm=use_llm,
                 decision_id=decision_id)


__all__ = ["infer", "timeline", "run", "heuristic_consequence"]
