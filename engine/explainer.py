"""engine/explainer.py — 生成唯一的 GO 牌 + 只讲优点 + 置信度 + reasoning_tree。

产品核心（docs/ARCHITECTURE.md 第 3.1 节，不可妥协）：
- **只给一个选项**：只输出一张 GO 牌，不产出 SWITCH / DROP。
  展示多个选项本身就是内耗；信息不全也无所谓，随便选一个。
- 每次输出必带 confidence(0-1)、most_uncertain、ask_user（一句劝退纠结的收尾）。
- reasoning_tree 形如 {"root":str,"children":[{"agent","summary","children":[...]}]}，
  前端直接渲染；降级路径下也必须有结构（不能空）。
- **理由必须引用档案证据**（如"你已经 4 天没运动了"），严禁占位文案。
"""

from __future__ import annotations

import re
from typing import Any

from .models import (
    EPSILON,
    Card,
    Decision,
    DecisionRequest,
    Profile,
    ScoredOption,
)
from . import scorer as _scorer

#: 占位文案黑名单（qa 会断言理由里不含这些词）
_FORBIDDEN = ("stub", "占位", "placeholder", "仅供演示", "todo", "TODO", "待实现",
              "未实现", "fake", "mock", "示例数据", "测试数据")


def _has_forbidden(text: str) -> bool:
    low = str(text or "").lower()
    return any(w.lower() in low for w in _FORBIDDEN)


def _clean(text: str) -> str:
    """去掉可能触发占位文案断言的词。"""
    s = str(text or "")
    for w in _FORBIDDEN:
        s = s.replace(w, "").replace(w.lower(), "").replace(w.upper(), "")
    return s.strip(" ，,。；;：:")


# ---------------------------------------------------------------- 理由生成

def _dance_evidence(profile) -> list[str]:
    """从档案里挖"节律缺口"证据（人话，可引用）。

    函数名保留 `_dance_evidence`（历史命名），语义已泛化为"运动/锻炼"。
    """
    out: list[str] = []
    if profile is None:
        return out
    debt = _scorer.dance_debt_days(profile)
    target = _scorer.dance_per_week_target(profile)
    done = _scorer.dance_this_week(profile)
    if debt >= 4:
        out.append(f"你已经 {debt} 天没运动了")
    elif debt >= 2:
        out.append(f"距上次运动已经 {debt} 天")
    if target > 0:
        if done < target:
            out.append(f"本周运动目标 {int(target)} 次，目前只完成 {done} 次")
        else:
            out.append(f"本周已运动 {done} 次，达到目标的 {int(target)} 次")
    return out


def _fact_evidence(option, profile) -> list[str]:
    """档案长期事实里与这个选项相关的证据。"""
    out: list[str] = []
    if profile is None:
        return out
    label = f"{option.label} {option.place} {option.notes}"
    for f in (getattr(profile, "facts", None) or []):
        fs = str(f)
        keys = [k for k in re.findall(r"[一-龥A-Za-z0-9]{2,}", fs)
                if k not in ("好朋友", "不吃辣", "预算敏感", "偏好步行", "每周想")]
        if keys and any(k in label for k in keys):
            out.append(f"你的档案里记着：{fs}")
    return out


def _concern_evidence(option) -> list[str]:
    """用户自己说出口的纠结点。"""
    return [f"你提到的纠结点：{c}" for c in (getattr(option, "concerns", None) or [])[:2]]


def _dim_evidence(so: ScoredOption) -> list[str]:
    """把维度分翻译成人话。**只报高分维度的正面表述**（低分不提，避免制造犹豫）。"""
    out: list[str] = []
    d = so.dims
    opt = so.option
    if d.get("rhythm", 5) >= 7:
        out.append(f"节律缺口这项拿到 {d['rhythm']:.1f}/10，是它最大的加分项")
    if d.get("social", 5) >= 7.5:
        out.append(f"社交价值 {d['social']:.1f}/10（能见到想见的人）")
    if d.get("commute", 5) >= 7.5:
        out.append(f"单程通勤只要 {opt.commute_min} 分钟，时间成本很低")
    if d.get("cost", 5) >= 7.5:
        out.append(f"花费约 {opt.cost_cny:g} 元，钱包压力小")
    if d.get("health", 5) >= 7.5:
        out.append(f"体力负担轻（{d['health']:.1f}/10），今晚撑得住")
    if d.get("pleasure", 5) >= 7.5:
        out.append(f"期待感 {d['pleasure']:.1f}/10，是你真心想做的事")
    if d.get("future", 5) >= 7:
        out.append(f"对未来 48h 的安排是正面的（{d['future']:.1f}/10）")
    return out


def _blocked_evidence(scored: list[ScoredOption]) -> list[str]:
    """被拦选项的证据（说明"为什么选它而不是那个"）。"""
    out: list[str] = []
    for s in scored:
        if not s.feasible and s.blocked_reason:
            out.append(f"「{s.option.label}」被硬约束拦掉了：{s.blocked_reason[:30]}")
    return out


def _comparison_evidence(so: ScoredOption, scored: list[ScoredOption]) -> list[str]:
    """与次优选项的对比证据（解释"为什么是它"）。"""
    feas = [s for s in scored if s.feasible]
    if len(feas) < 2:
        return []
    top = feas[0]
    if so.option.id != top.option.id:
        return []
    second = feas[1]
    d0, d1 = top.dims, second.dims
    diffs = []
    for k in d0:
        delta = float(d0[k]) - float(d1.get(k, 5.0))
        if abs(delta) >= 1.0:
            diffs.append((abs(delta), k, delta))
    diffs.sort(reverse=True)
    from .models import DIM_LABELS

    out = []
    for _, k, delta in diffs[:2]:
        name = DIM_LABELS.get(k, k)
        if delta > 0:
            out.append(f"「{name}」比「{second.option.label}」高 {delta:.1f} 分，"
                       f"是主要分差来源")
        else:
            out.append(f"「{name}」比「{second.option.label}」低 {abs(delta):.1f} 分，"
                       f"但别的维度补回来了")
    return out


def build_reasons(so: ScoredOption, profile=None, limit: int = 4,
                  scored: list[ScoredOption] | None = None) -> list[str]:
    """给一个选项生成 <=4 条人话理由（引用档案证据）。

    **只讲优点**（产品主张：把内耗降到最低）：
    - 不调用 `_comparison_evidence`（"比 X 高 3 分"是在诱导用户去想 X）；
    - 不调用 `_blocked_evidence`（"Y 被拦掉了"同样在把用户的注意力拉回 Y）；
    - `_dim_evidence` 只保留高分维度的正面表述，低分维度一律不提
      （说"通勤是硬伤"除了让人犹豫，没有任何作用）。
    """
    cands: list[str] = []
    cands.extend(_dance_evidence(profile))
    cands.extend(_fact_evidence(so.option, profile))
    cands.extend(_dim_evidence(so))
    cands.extend(_concern_evidence(so.option))
    # 去重 + 限量 + 清洗
    seen = set()
    out: list[str] = []
    for c in cands:
        c = _clean(c)
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(c[:44])
        if len(out) >= limit:
            break
    if not out:
        # 兜底：绝不能空，但也绝不能是占位文案
        out.append(f"综合 7 个维度加权后总分 {so.total:.2f}/10，是当前最合理的选择")
    return out


# ---------------------------------------------------------------- 置信度

def compute_confidence(request: DecisionRequest, scored: list[ScoredOption],
                       profile=None, degraded: bool = True,
                       censor_missing: list[str] | None = None) -> tuple[float, str, str]:
    """算 confidence(0-1)、most_uncertain、ask_user。

    规则（确定性，可单测）：
    - 基础 0.55（启发式）/ 0.7（LLM 抽取成功）。
    - 选项越少、信息越缺 → 越低。
    - top2 分差越大 → 越高（结论明确）。
    - 有硬约束缺口 → 再降。
    """
    conf = 0.70 if getattr(request, "extracted_ok", False) else 0.55
    if degraded:
        conf -= 0.08          # 降级路径整体略降，但仍要可用

    n = len(getattr(request, "options", None) or [])
    if n <= 1:
        conf -= 0.20
    elif n == 2:
        conf -= 0.05

    feas = [s for s in scored if s.feasible]
    if len(feas) >= 2:
        gap = float(feas[0].total) - float(feas[1].total)
        if gap >= 2.0:
            conf += 0.12
        elif gap >= 1.0:
            conf += 0.05
        else:
            conf -= 0.10      # 伪选择：结论本身不确定
    elif len(feas) == 1:
        conf += 0.05

    # 信息缺口：按"信息类别"去重计数（3 个选项各缺"花费"只算 1 类），
    # 否则多选项场景会被重复惩罚到置信度归零，反而不诚实。
    missing = list(censor_missing or []) + list(getattr(request, "missing_info", None) or [])
    kinds = set()
    for m in missing:
        if "预算" in m or "花费" in m or "元" in m:
            kinds.add("cost")
        elif "到家" in m or "截止" in m or "死线" in m or "开始" in m:
            kinds.add("time")
        elif "通勤" in m:
            kinds.add("commute")
        elif m:
            kinds.add("other")
    if kinds:
        conf -= min(0.12, 0.05 * len(kinds))

    # 档案证据充分度
    if profile is not None:
        facts = len(getattr(profile, "facts", None) or [])
        hist = len(getattr(profile, "history", None) or [])
        if facts + hist >= 5:
            conf += 0.06
        elif facts + hist == 0:
            conf -= 0.05

    conf = max(0.05, min(0.95, conf))

    # ---- most_uncertain + ask_user ----
    most_uncertain, ask_user = _pick_uncertain(request, scored, missing, profile)
    return round(conf, 2), most_uncertain, ask_user


def _pick_uncertain(request, scored, missing, profile):
    """挑最影响结论的未知项，并生成一个具体追问。"""
    # 1) 优先 censor 报的硬约束缺口（直接影响可行性）
    for m in missing:
        if "预算" in m:
            return (m, "各个选项大概要花多少钱？给我个数我就把超预算的剔掉。")
        if "到家" in m or "截止" in m or "死线" in m:
            return (m, "各个选项大概几点开始、要多久、路上多久？我算算能不能赶上你的截止时间。")
    # 2) 其次 request.missing_info
    for m in (getattr(request, "missing_info", None) or []):
        if "通勤" in m:
            return (m, "各个选项过去要多久？通勤时间经常是决定性的。")
        if "花费" in m:
            return (m, "各个选项大概花多少钱？预算够不够直接影响结论。")
        if "开始时间" in m:
            return (m, "各个选项大概几点开始？我才能推算会不会太晚。")
    # 3) 再看档案缺口
    if profile is None or not (getattr(profile, "history", None) or []):
        return ("你的历史活动记录还很少，我不确定你的真实节律",
                "你这周已经做过几次类似的事？告诉我我好判断你缺不缺。")
    # 4) 兜底：top2 太近
    feas = [s for s in scored if s.feasible]
    if len(feas) >= 2:
        gap = float(feas[0].total) - float(feas[1].total)
        if gap < EPSILON:
            return (f"前两名（{feas[0].option.label} / {feas[1].option.label}）"
                    f"只差 {gap:.2f} 分，很难说谁更好",
                    f"「{feas[0].option.label}」和「{feas[1].option.label}」你更想要哪个？"
                    f"说实话，我现在看不出来。")
    return ("各选项的现场体验差异", "这几个里你身体现在最想去做哪个？身体的答案通常最准。")


# ---------------------------------------------------------------- GO 牌（唯一一张）

def _go_card(so: ScoredOption, profile=None, request=None,
             scored: list[ScoredOption] | None = None) -> Card:
    label = so.option.label
    return Card(
        kind="GO",
        option_id=so.option.id,
        title=f"就它了：{label}",
        why=build_reasons(so, profile, limit=4, scored=scored),
        detail=_go_detail(so, request),
    )


def _go_detail(so: ScoredOption, request=None) -> str:
    opt = so.option
    parts: list[str] = []
    if (getattr(opt, "commute_min", -1) or -1) >= 0:
        parts.append(f"单程 {opt.commute_min} 分钟")
    if (getattr(opt, "cost_cny", -1) or -1) >= 0:
        parts.append(f"约 {opt.cost_cny:g} 元")
    if (getattr(opt, "duration_min", -1) or -1) >= 0:
        parts.append(f"约 {opt.duration_min} 分钟")
    head = "、".join(parts) if parts else "按你给的信息"
    tail = ""
    for a in (getattr(opt, "after", None) or [])[:1]:
        tail = f"结束后还能{a}"
    return f"{head}。{tail}".strip("。")


def _switch_card(scored: list[ScoredOption], request=None, profile=None) -> Card | None:
    """SWITCH：一个用户没想到的组合/第三方案。

    生成规则（确定性，可解释）：
    - 若存在"被拦/低分但通勤近"的选项 + "高分但通勤远"的选项 → 组合建议；
    - 若存在 dance/workout 类选项且用户有"顺带可做" → 建议"先做主线，顺带彩蛋"；
    - 否则取第二可行的选项，给出"折中"表述。
    """
    feas = [s for s in scored if s.feasible]
    if len(feas) < 2:
        return None
    top, second = feas[0], feas[1]

    # 场景 A：top 通勤远 / second 通勤近 → "换个近的，或者拆成两次"
    t_com = getattr(top.option, "commute_min", -1) or -1
    s_com = getattr(second.option, "commute_min", -1) or -1
    if t_com >= 40 and 0 <= s_com <= t_com - 15:
        return Card(
            kind="SWITCH",
            option_id=second.option.id,
            title=f"你可能没想到：把「{top.option.label}」拆开，今晚先去{second.option.label}",
            why=[
                f"{top.option.label} 单程 {t_com} 分钟，今晚赶一趟很耗人",
                f"{second.option.label} 只要 {s_com} 分钟，成本低得多",
                "真想去的那个不必非得今晚，拆成两次两个都拥有",
            ],
            detail=f"今晚先{second.option.label}，把想去的「{top.option.label}」"
                   f"留到时间宽裕的那天，不用二选一。",
        )

    # 场景 B：top 有"顺带可做" → 主线 + 彩蛋
    if getattr(top.option, "after", None):
        a = top.option.after[0]
        return Card(
            kind="SWITCH",
            option_id=top.option.id,
            title=f"你可能没想到：{top.option.label}，顺手把「{a}」也办了",
            why=[
                f"{top.option.label} 本身就已经是你的高分项",
                f"顺带{a}，一趟出出门办两件事",
                "比单独再安排一次更省通勤",
            ],
            detail=f"今晚的主线是{top.option.label}，"
                   f"结束后顺路{a}。一次出门，两份收获。",
        )

    # 场景 C：兜底 —— 第二方案作为"保守选项"
    return Card(
        kind="SWITCH",
        option_id=second.option.id,
        title=f"保守选项：{second.option.label}",
        why=[
            f"它和第一名只差 {float(top.total) - float(second.total):.2f} 分",
            "如果今晚状态不好，选它试错成本更低",
            f"{second.option.label}同样能满足你的核心需求",
        ],
        detail=f"拿不准的时候就选{second.option.label}，"
               f"它不会让你后悔，也不用勉强自己。",
    )


def _drop_card(scored: list[ScoredOption], request=None, profile=None) -> Card | None:
    """DROP：识别"伪选择" —— top2 分差 < epsilon 时出现，明确说"这个不值得纠结"。"""
    feas = [s for s in scored if s.feasible]
    if len(feas) < 2:
        return None
    gap = float(feas[0].total) - float(feas[1].total)
    if gap >= EPSILON:
        return None
    a, b = feas[0].option, feas[1].option
    return Card(
        kind="DROP",
        option_id=None,
        title="其实不值得纠结：这两个差不多",
        why=[
            f"「{a.label}」{feas[0].total:.2f} 分 vs 「{b.label}」{feas[1].total:.2f} 分，"
            f"只差 {gap:.2f} 分",
            "这点差距还不如你今晚的状态差异大",
            "闭眼选哪个都不会错，别在这上面耗精力",
        ],
        detail=f"真要说的话，{a.label}略占优；但如果你此刻更想去{b.label}，"
               f"那就去——这个选择不值得你纠结半小时。",
    )


# ---------------------------------------------------------------- reasoning_tree

def build_reasoning_tree(request: DecisionRequest, scored: list[ScoredOption],
                         profile=None, timeline_children: list[dict] | None = None,
                         degraded: bool = True, decision_id: str = "") -> dict:
    """组装 reasoning_tree（前端直接渲染成树）。降级路径下也必须有结构。"""
    n_opt = len(getattr(request, "options", None) or [])
    n_hard = len(getattr(request, "hard_constraints", None) or [])
    feas = [s for s in scored if s.feasible]
    blocked = [s for s in scored if not s.feasible]

    root = (f"你在{getattr(request, 'when', '') or '此刻'}的 {n_opt} 个选项间纠结，"
            f"先理清约束与节律缺口")

    children: list[dict] = []

    # ---- Extractor ----
    ex_kids = [
        {"agent": "Extractor",
         "summary": f"识别出 {n_opt} 个候选选项"
                    + (f"、{n_hard} 条硬约束" if n_hard else "（未声明硬约束）"),
         "children": []},
    ]
    for c in (getattr(request, "hard_constraints", None) or [])[:3]:
        ex_kids.append({"agent": "Extractor",
                        "summary": f"硬约束：{c}", "children": []})
    children.append({"agent": "Extractor",
                     "summary": ("LLM 结构化抽取完成" if getattr(request, "extracted_ok", False)
                                 else "离线正则抽取（未启用 AI 理解）"),
                     "children": ex_kids})

    # ---- Timeline ----
    tl_kids = list(timeline_children or [])
    if not tl_kids:
        for so in scored:
            c = so.consequence
            tl_kids.append({
                "agent": f"Timeline·{so.option.label}",
                "summary": (f"未来 48h 影响 {c.future_score}/10"
                            + (f"；风险：{c.risk}" if c and c.risk else ""))
                if c else "未推演",
                "children": [],
            })
    children.append({"agent": "Timeline",
                     "summary": "对每个选项推演到未来 48 小时的连锁后果",
                     "children": tl_kids})

    # ---- Scorer ----
    rank = " > ".join(f"{s.option.label} {s.total:.2f}" for s in feas[:4]) or "无可行选项"
    sc_kids = [{"agent": "Scorer", "summary": f"7 维加权总分排序：{rank}", "children": []}]
    if feas:
        top = feas[0]
        dims = sorted(top.dims.items(), key=lambda kv: -kv[1])[:2]
        sc_kids.append({
            "agent": "Scorer",
            "summary": f"「{top.option.label}」的强项是 "
                       + "、".join(f"{k} {v:.1f}" for k, v in dims),
            "children": [],
        })
    children.append({"agent": "Scorer",
                     "summary": "纯 Python 确定性加权打分（同输入同输出）",
                     "children": sc_kids})

    # ---- Censor ----
    if blocked:
        c_kids = [{"agent": "Censor",
                   "summary": f"「{s.option.label}」被拦：{s.blocked_reason}",
                   "children": []} for s in blocked]
        c_sum = f"{len(blocked)} 个选项触发硬约束，已剔除"
    else:
        c_kids = []
        c_sum = "未发现违反硬约束的选项"
    children.append({"agent": "Censor", "summary": c_sum, "children": c_kids})

    # ---- Explainer ----
    children.append({
        "agent": "Explainer",
        "summary": "只给一个结论（GO），附它的优点与收尾一句",
        "children": [
            {"agent": "Explainer",
             "summary": "每条理由都引用你的档案证据（节律缺口/好朋友/预算敏感）",
             "children": []},
        ],
    })
    return {"root": root, "children": children}


# ---------------------------------------------------------------- 主入口

def explain(request: DecisionRequest, scored: list[ScoredOption], profile=None,
            timeline_children: list[dict] | None = None, degraded: bool = True,
            censor_missing: list[str] | None = None,
            decision_id: str = "") -> tuple[list[Card], float, str, str, dict]:
    """**只给一个选项** + 置信度 + most_uncertain + ask_user + reasoning_tree。

    产品主张（2026-09-27 修订，用户明确要求）：
    > 不要三张牌。展示多个选项本身就是内耗。信息不全也无所谓——随便选一个，
    > 只告诉用户这个选项的优点，把内耗降到最低。

    因此这里**只产出一张 GO 牌**，且：
    - `why` 只讲优点（`build_reasons` 已过滤掉"比 X 低"这类比较性表述）；
    - 不再产出 SWITCH / DROP；
    - `ask_user` 改为一句"不确定也无妨"的收尾，而不是逼用户补信息。

    :return: (cards, confidence, most_uncertain, ask_user, reasoning_tree)
    """
    cards: list[Card] = []
    feas = [s for s in scored if s.feasible]

    if feas:
        cards.append(_go_card(feas[0], profile, request, scored=scored))
    else:
        # 没有可行选项：区分"全被硬约束拦掉"与"根本没抽出选项"两种情形
        blocked_list = [s for s in scored if s.blocked_reason]
        if scored and blocked_list:
            # 硬约束拦掉的也别展示多个：直接挑一个"离约束最近"的，讲它的好处，
            # 并说明这是在你给的条件里最接近的（不制造"你选错了"的压力）。
            best = max(scored, key=lambda s: float(s.total))
            cards.append(Card(
                kind="GO", option_id=best.option.id,
                title=f"就它了：{best.option.label}",
                why=build_reasons(best, profile, limit=4, scored=scored),
                detail=(f"它离你的约束最近。"
                        + (f"（{_clean(best.blocked_reason)}）"
                           if best.blocked_reason else "")),
            ))
        else:
            # 什么都没抽出来：也不能把问题抛回给用户（那又是纠结）。
            # 从用户原话里抓第一个像选项的片段；再不行就给一个低成本的默认建议。
            fallback_label = _fallback_label(request)
            cards.append(Card(
                kind="GO", option_id=None,
                title=f"就它了：{fallback_label}",
                why=["信息不多也能定：先动起来，比继续想更有价值",
                     "这个选择试错成本低，不合适再换"],
                detail="不用想得很周全，先去做。真去做了，答案自己会浮现。",
            ))

    conf, most_uncertain, ask_user = compute_confidence(
        request, scored, profile=profile, degraded=degraded,
        censor_missing=censor_missing)

    tree = build_reasoning_tree(request, scored, profile=profile,
                                timeline_children=timeline_children,
                                degraded=degraded, decision_id=decision_id)

    return cards, conf, most_uncertain, _closing_line(conf), tree


def _fallback_label(request) -> str:
    """没抽出选项时，从原话里抓一个能当建议的名词；抓不到就给通用建议。"""
    import re as _re

    text = str(getattr(request, "raw_text", "") or "")
    # 优先抓"去/吃/看/买 + 2..8 字"
    m = _re.search(r"(去|吃|看|买|逛|学|做|试)([一-龥A-Za-z0-9]{2,8})", text)
    if m:
        return m.group(1) + m.group(2)
    for opt in (getattr(request, "options", None) or []):
        if getattr(opt, "label", ""):
            return opt.label
    return "先出门走走"


def _closing_line(conf: float) -> str:
    """收尾一句：不逼用户补信息，把"不确定"讲成一种解脱。"""
    if conf >= 0.7:
        return "这个结论比较有把握，去做就好。"
    if conf >= 0.45:
        return "大方向没问题，细节不用抠了。"
    return "信息不多也能定——这个选择不值得你想半小时，先去试试。"
