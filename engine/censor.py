"""engine/censor.py — 安全 / 健康 / 预算闸门（硬约束）。

两种形态（ARCHITECTURE 第 2 节）：
1. **纯函数版** `censor(request, scored)` —— 引擎实际使用的确定性闸门。
2. **mxagent --censor 形式** `censor_source()` —— 生成一个 censor python 文件，
   可用 `mxagent --censor <file>` 挂在 LLM 调用前（比赛叙事：生成前拦截）。

硬约束语义（contracts/api.md 第 33~37 行，**不可改**）：
- `hard_constraints` 是**自然语言字符串列表**，不用结构化 DSL。
- 预算超限 → feasible=false + blocked + blocked_reason 非空。
- `start_hint + duration_min + commute_min` 齐全且推算到家晚于 deadline → feasible=false。
- 信息不全 → 保留 feasible=true，但必须降低 confidence 并写入 missing_info/most_uncertain。
"""

from __future__ import annotations

import re
from typing import Any

from .models import DecisionRequest, ScoredOption, clamp

# ---------------------------------------------------------------- 正则（自然语言 → 数值）

_TIME_RE = re.compile(r"(\d{1,2})\s*[:：点]\s*(\d{1,2})")
#: 宽松时间表达（"22:30" / "11点" / "11点半" / "11点30"）
_TIME_ANY_RE = re.compile(
    r"(\d{1,2})\s*[:：点]\s*(\d{1,2})|(\d{1,2})\s*点\s*(半|\d{1,2})?")
#: 死线关键词在时间**之前**（"22:30 前必须到家" / "11点前回家"）
_DEADLINE_RE = re.compile(
    r"(?:死线|截止|之前|以前|前必须|前要|前得|须|必须|务必|一定要|不能晚于|不得晚于)"
    r"[^0-9]{0,12}(\d{1,2})\s*[:：点]\s*(\d{1,2})")
#: 死线关键词在时间**之后**（"明天早上7点要起床" / "7点得起床上班"）。
#: 必须带情态词（必须/要/得…），否则 "18:30 出发" 这种**开始时间**会被误判成死线。
_DEADLINE_AFTER_RE = re.compile(
    r"(\d{1,2})\s*[:：点]\s*(\d{1,2})?\s*(?:左右|前后)?\s*"
    r"(?:必须|得要|需要|得|要|需)\s*"
    r"(?:起床|起床上班|上班|上岗|到公司|到单位|出门|出发|赶车|赶高铁|赶飞机)")
#: 死线关键词在时间**之前**且时间是"N点"口语形式（"8点前得到公司" / "下午3点前得到公司"）
_DEADLINE_DOT_BEFORE_RE = re.compile(
    r"(\d{1,2})\s*点\s*(半|\d{1,2})?\s*(?:之)?前\s*"
    r"(?:必须|得要|需要|得|要|需)?\s*"
    r"(?:到家|回家|回去|归|到公司|到单位|起床|起床上班|上班|上岗|出门|出发)")
_BUDGET_RE = re.compile(
    r"(?:预算|花费|开销|费用|不超过|不超|控制在|最多|至多|上限)[^0-9]{0,12}(\d+(?:\.\d+)?)\s*"
    r"(?:元|块|块钱|rmb|RMB|¥|￥)?")
_BUDGET_RE2 = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|块钱)\s*(?:以内|之内|以下|封顶)")

#: 时段词 → 小时修正（"下午3点"=15:00，"早上7点"=07:00）
_AM_WORDS = ("凌晨", "早上", "早晨", "上午", "明早", "一早")
_PM_WORDS = ("下午", "傍晚", "晚上", "晚间", "夜里", "今晚")

#: 健康/安全类硬约束关键词 → 命中即拦（无论数值）
_HEALTH_WORDS = ("不能喝", "不能吃", "过敏", "忌口", "医生", "医嘱", "禁",
                 "不能剧运", "不宜", "怀孕", "受伤", "发烧", "感冒")
_SAFETY_WORDS = ("太晚", "不安全", "深夜", "一个人去", "别去", "禁止")


def _norm_hh_mm(h: int, mi: int, prefix: str) -> int | None:
    """把 (时, 分, 时段词) 归一化成 0~1439 的分钟数。

    "下午3点" → 900；"早上7点" → 420；"晚上11点半" → 1410。
    没有时段词时按原样返回（24 小时制的 "22:30" 不受影响）。
    """
    if not (0 <= h < 24 and 0 <= mi < 60):
        return None
    if any(w in prefix for w in _PM_WORDS) and h < 12:
        h += 12
    elif any(w in prefix for w in _AM_WORDS) and h == 12:
        h = 0
    return h * 60 + mi


def _first_time_minutes(text: str) -> int | None:
    """在文本里找第一个时间表达 → 分钟数（含"下午3点"这类时段修正）。"""
    s = str(text or "")
    m = _TIME_ANY_RE.search(s)
    if not m:
        return None
    if m.group(1) is not None:
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        h = int(m.group(3))
        tail = m.group(4)
        mi = 30 if tail == "半" else (int(tail) if tail else 0)
    return _norm_hh_mm(h, mi, s[max(0, m.start() - 6):m.start()])


def parse_deadline(text: str) -> int | None:
    """从自然语言里解析"必须到家的时刻"→ 分钟数（0~1439）。找不到返回 None。

    例："22:30 前必须到家" → 1350；"11点前回家" → 1380；
        "明天早上7点要起床" → 420；"下午3点前得到公司" → 900。
    """
    if not text:
        return None
    s = str(text)
    m = _DEADLINE_RE.search(s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        prefix = s[max(0, m.start() - 6):m.start()]
        return _norm_hh_mm(h, mi, prefix)
    # 时间在前、死线词在后（"明天早上7点要起床" / "7点得起床上班"）
    m = _DEADLINE_AFTER_RE.search(s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        prefix = s[max(0, m.start() - 6):m.start()]
        return _norm_hh_mm(h, mi, prefix)
    # "N点前得到公司" 这类口语死线（"8点前得到公司" / "下午3点前得到公司"）
    m = _DEADLINE_DOT_BEFORE_RE.search(s)
    if m:
        h = int(m.group(1))
        tail = m.group(2)
        mi = 30 if tail == "半" else (int(tail) if tail else 0)
        return _norm_hh_mm(h, mi, s[max(0, m.start() - 6):m.start()])
    # 兜底：只要出现"到家/回家"+ 任意时间
    if re.search(r"(到家|回家|回去|归)", s):
        return _first_time_minutes(s)
    return None


def parse_budget(text: str) -> float | None:
    """从自然语言里解析预算上限（元）。找不到返回 None。"""
    if not text:
        return None
    s = str(text)
    m = _BUDGET_RE.search(s) or _BUDGET_RE2.search(s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def parse_start_minutes(start_hint: str) -> int | None:
    """'19:00' / '19点' / '19：30' → 分钟数；空或无法解析返回 None。"""
    if not start_hint:
        return None
    m = _TIME_RE.search(str(start_hint))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h < 24 and 0 <= mi < 60):
        return None
    return h * 60 + mi


def home_arrival_min(option) -> int | None:
    """推算到家时刻（分钟）。start_hint + duration_min + commute_min 齐全才算得出来。"""
    start = parse_start_minutes(getattr(option, "start_hint", ""))
    dur = getattr(option, "duration_min", -1)
    com = getattr(option, "commute_min", -1)
    if start is None or dur is None or dur < 0 or com is None or com < 0:
        return None
    return start + int(dur) + int(com)


def fmt_minutes(m: int | None) -> str:
    """1350 → '22:30'。"""
    if m is None:
        return ""
    m = int(m) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


# ---------------------------------------------------------------- 纯函数闸门

def _budget_violation(option, budget: float | None) -> str:
    if budget is None:
        return ""
    cost = getattr(option, "cost_cny", -1.0)
    if cost is None or cost < 0:
        return ""                      # 花费未知 → 不拦，但记 missing_info
    if float(cost) > float(budget) + 1e-9:
        return (f"预估花费 {cost:g} 元超出预算上限 {budget:g} 元"
                f"（超 {float(cost) - float(budget):g} 元）")
    return ""


def _deadline_violation(option, deadline: int | None) -> str:
    if deadline is None:
        return ""
    arrival = home_arrival_min(option)
    if arrival is None:
        return ""                      # 信息不全 → 不拦
    if arrival > deadline:
        return (f"按开始 {getattr(option, 'start_hint', '')} + "
                f"{getattr(option, 'duration_min', -1)} 分钟 + 单程通勤 "
                f"{getattr(option, 'commute_min', -1)} 分钟推算，到家约 "
                f"{fmt_minutes(arrival)}，晚于截止 {fmt_minutes(deadline)}")
    return ""


def _health_violation(option, constraint: str) -> str:
    label = f"{getattr(option, 'label', '')} {getattr(option, 'notes', '')} " \
            f"{' '.join(getattr(option, 'concerns', None) or [])}"
    for kw in _HEALTH_WORDS:
        if kw in constraint and kw in label:
            return f"健康约束「{constraint}」与该选项冲突"
    return ""


def censor(request: DecisionRequest, scored: list[ScoredOption],
           profile=None) -> tuple[list[ScoredOption], list[dict], list[str]]:
    """对已打分的选项跑硬约束闸门。

    :return: (scored, blocked, missing_info)
        - scored: 原地改写 feasible / blocked_reason，**不重排**（调用方用 scorer.resort）
        - blocked: [{"option_id","reason"}]
        - missing_info: 因信息不全而无法判定的缺口（调用方用于降 confidence）
    """
    blocked: list[dict] = []
    missing: list[str] = []
    constraints = list(getattr(request, "hard_constraints", None) or [])

    budget = None
    deadline = None
    for c in constraints:
        b = parse_budget(c)
        if b is not None and budget is None:
            budget = b
        d = parse_deadline(c)
        if d is not None and deadline is None:
            deadline = d

    for so in scored:
        opt = so.option
        reasons: list[str] = []
        for c in constraints:
            r = _budget_violation(opt, parse_budget(c)) if parse_budget(c) is not None else ""
            if not r:
                r = _deadline_violation(opt, parse_deadline(c)) \
                    if parse_deadline(c) is not None else ""
            if not r:
                r = _health_violation(opt, c)
            if r:
                reasons.append(r)
        # 去重（同一条约束可能被多个正则命中）
        uniq = []
        for r in reasons:
            if r not in uniq:
                uniq.append(r)
        if uniq:
            so.feasible = False
            so.blocked_reason = "；".join(uniq)
            blocked.append({"option_id": opt.id, "reason": so.blocked_reason})
        else:
            so.feasible = True
            so.blocked_reason = ""

    # ---- 信息不全的缺口（不拦，但必须让调用方降 confidence） ----
    for so in scored:
        opt = so.option
        if budget is not None and (getattr(opt, "cost_cny", -1.0) or -1.0) < 0:
            missing.append(f"{opt.label} 的花费未知，无法核对预算上限 {budget:g} 元")
        if deadline is not None:
            if parse_start_minutes(getattr(opt, "start_hint", "")) is None:
                missing.append(f"{opt.label} 的开始时间未知，无法核对 "
                               f"{fmt_minutes(deadline)} 前到家")
            elif (getattr(opt, "duration_min", -1) or -1) < 0:
                missing.append(f"{opt.label} 的预计时长未知，无法核对 "
                               f"{fmt_minutes(deadline)} 前到家")
            elif (getattr(opt, "commute_min", -1) or -1) < 0:
                missing.append(f"{opt.label} 的通勤时间未知，无法核对 "
                               f"{fmt_minutes(deadline)} 前到家")

    # 去重并限量
    seen = set()
    uniq_missing = []
    for m in missing:
        if m not in seen:
            seen.add(m)
            uniq_missing.append(m)
    return scored, blocked, uniq_missing[:6]


# 兼容别名
def apply_censors(request, scored, profile=None):
    return censor(request, scored, profile)


def check(request, scored, profile=None):
    return censor(request, scored, profile)


# ---------------------------------------------------------------- mxagent --censor 形式

_CENSOR_SOURCE = '''"""engine/agents/censor_gate.py — mxagent --censor 用的硬约束闸门。

用法（比赛叙事：在 LLM 生成前拦截）：
    mxagent --task "..." --censor engine/agents/censor_gate.py

本文件由 engine.censor.censor_source() 生成/维护，逻辑与 censor.py 的纯函数版一致：
预算超限 / 到家晚于死线 / 健康冲突 → 直接拒绝工具调用。
"""
from MxAgentLib import add_censor

BUDGET_WORDS = ("预算", "不超过", "上限", "最多", "至多", "控制在")
DEADLINE_WORDS = ("前必须到家", "之前必须到家", "前要到家", "前必须回家")


@add_censor
def pickone_censor(name: str, argv: list, kwargs: dict):
    """健康/预算/安全硬约束闸门（自然语言规则，非结构化 DSL）。"""
    text = " ".join(str(x) for x in (argv or []))
    for w in ("过敏", "不能吃辣", "医嘱", "禁止"):
        if w in text:
            return f"命中健康硬约束（{w}），已拦截"
    return None
'''


def censor_source() -> str:
    """返回 mxagent --censor 形式的 python 源码（写入 engine/agents/ 后可用）。"""
    return _CENSOR_SOURCE


def write_censor_file(path: str) -> str:
    """把 censor 源码写到指定路径，返回路径。失败返回空串（不抛异常）。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_CENSOR_SOURCE)
        return path
    except Exception:
        return ""


__all__ = [
    "censor", "apply_censors", "check", "parse_deadline", "parse_budget",
    "parse_start_minutes", "home_arrival_min", "fmt_minutes",
    "censor_source", "write_censor_file",
]
