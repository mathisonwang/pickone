"""engine/advisor.py — 编排入口：advise(request, profile, use_llm=True) -> Decision。

五段流程（docs/ARCHITECTURE.md 第 3 节）：
    ① Extractor  自然语言 → DecisionRequest
    ② Timeline   每个选项推演到未来 48h → OptionConsequence
    ③ Scorer     纯 Python 确定性加权打分
    ④ Censor     纯函数硬约束闸门
    ⑤ Explainer  唯一 GO 牌 + 只讲优点 + 置信度 + reasoning_tree + 收尾一句

**不可妥协的降级保证**：
- MXAGENT_BIN 指向不存在的可执行文件 → 返回合法 Decision、degraded=true、**不抛异常**。
- advise() 在任何情况下都返回合法 Decision（绝不 raise）。
"""

from __future__ import annotations

import time
import uuid

from .models import (
    DEFAULT_WEIGHTS,
    Decision,
    DecisionRequest,
    Profile,
)


def _new_decision_id() -> str:
    return "dec-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _weights_of(profile) -> dict:
    """取档案权重；非法则回退默认权重。"""
    if profile is None:
        return dict(DEFAULT_WEIGHTS)
    w = getattr(profile, "weights", None)
    if not isinstance(w, dict) or not w:
        return dict(DEFAULT_WEIGHTS)
    out = dict(DEFAULT_WEIGHTS)
    for k, v in w.items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv == fv and fv > 0:      # 排除 NaN
            out[k] = fv
    return out


def advise(request, profile=None, use_llm: bool = True) -> Decision:
    """完整决策编排。**任何输入、任何环境都返回合法 Decision，绝不抛异常。**"""
    t0 = time.time()
    decision_id = _new_decision_id()

    # ---- 0) 入参容错（可能是 dict / None / 半成品对象） ----
    try:
        if request is None:
            request = DecisionRequest(raw_text="", when="", options=[],
                                      hard_constraints=[], missing_info=[
                                          "输入为空：请告诉我在纠结什么"],
                                      extracted_ok=False)
        elif isinstance(request, dict):
            request = DecisionRequest.from_dict(request)
        elif not hasattr(request, "options"):
            request = DecisionRequest.from_dict({})
    except Exception:
        request = DecisionRequest(raw_text="", when="", options=[],
                                  hard_constraints=[], missing_info=["输入解析失败"],
                                  extracted_ok=False)

    if profile is not None and not hasattr(profile, "weights"):
        try:
            profile = Profile.from_dict(profile if isinstance(profile, dict) else {})
        except Exception:
            profile = None

    degraded = not bool(use_llm)
    weights = _weights_of(profile)

    # ---- ② Timeline（LLM 层级推演 / 降级确定性推演） ----
    consequences: dict = {}
    timeline_children: list[dict] = []
    try:
        from . import timeline as _timeline

        consequences, tree_info, tl_degraded = _timeline.infer(
            request, profile=profile, use_llm=use_llm,
            decision_id=decision_id, timeout=120.0)
        timeline_children = tree_info.get("children") or []
        degraded = degraded or tl_degraded
    except Exception:
        degraded = True

    # ---- ③ Scorer（纯 Python 确定性算术） ----
    scored = []
    try:
        from . import scorer as _scorer

        scored = _scorer.score(request, weights=weights, profile=profile,
                               consequences=consequences)
    except Exception:
        # 极端兜底：构造一个全 5 分的空壳，保证结构合法
        from .models import ScoredOption

        scored = [ScoredOption(option=o, dims={k: 5.0 for k in weights},
                               total=5.0, feasible=True)
                  for o in (getattr(request, "options", None) or [])]
        degraded = True

    # ---- ④ Censor（纯函数硬约束闸门） ----
    blocked: list[dict] = []
    censor_missing: list[str] = []
    try:
        from . import censor as _censor

        scored, blocked, censor_missing = _censor.censor(request, scored, profile=profile)
        # censor 之后重排：可行的按 total 降序，不可行的沉底
        from . import scorer as _scorer

        scored = _scorer.resort(scored)
    except Exception:
        degraded = True

    # ---- ⑤ Explainer（唯一 GO 牌 + 置信度 + reasoning_tree） ----
    try:
        from . import explainer as _explainer

        cards, conf, most_uncertain, ask_user, tree = _explainer.explain(
            request, scored, profile=profile,
            timeline_children=timeline_children, degraded=degraded,
            censor_missing=censor_missing, decision_id=decision_id)
    except Exception:
        # 极端兜底：至少给一张 GO
        from .models import Card

        feas = [s for s in scored if s.feasible]
        top = feas[0] if feas else (scored[0] if scored else None)
        cards = [Card(
            kind="GO", option_id=(top.option.id if top else None),
            title=(f"就它了：{top.option.label}" if top else "没看出你在纠结什么"),
            why=[f"综合 7 个维度加权后总分 {top.total:.2f}/10"] if top
            else ["请把几个选项分别列出来"],
            detail="按你给的信息，这是当前最合理的选择。")]
        conf, most_uncertain, ask_user = 0.4, "各选项的时间、花费、通勤距离", \
            "各个选项大概几点开始、花多少钱、离你多远？"
        tree = {"root": "决策", "children": []}
        degraded = True

    # ---- 补齐 ScoredOption.reasons（引用档案证据） ----
    try:
        from . import explainer as _explainer

        for so in scored:
            if not so.reasons:
                so.reasons = _explainer.build_reasons(so, profile, limit=3,
                                                      scored=scored)
    except Exception:
        pass

    # ---- 组装 Decision ----
    try:
        dec = Decision(
            decision_id=decision_id,
            created_at=time.time(),
            request=request,
            scored=scored,
            cards=cards,
            confidence=conf,
            most_uncertain=most_uncertain,
            ask_user=ask_user,
            reasoning_tree=tree,
            degraded=bool(degraded),
            blocked=blocked,
            weights_used=weights,
        )
    except Exception:
        # 最后一道防线：绝不抛异常
        dec = Decision(decision_id=decision_id, created_at=time.time(),
                       request=DecisionRequest(raw_text=""), scored=[], cards=[],
                       confidence=0.1, most_uncertain="引擎内部错误",
                       ask_user="请把几个选项分别列出来，我再试一次。",
                       reasoning_tree={"root": "决策", "children": []},
                       degraded=True, blocked=[], weights_used=dict(DEFAULT_WEIGHTS))

    return dec


# 兼容别名
def run(request, profile=None, use_llm=True):
    return advise(request, profile=profile, use_llm=use_llm)


def decide(request, profile=None, use_llm=True):
    return advise(request, profile=profile, use_llm=use_llm)


__all__ = ["advise", "run", "decide"]
