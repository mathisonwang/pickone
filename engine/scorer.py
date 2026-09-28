"""engine/scorer.py — 确定性多维打分（纯 stdlib，**不调 LLM**）。

设计约束（不可妥协，docs/ARCHITECTURE.md 第 3.1 / 4 节）：
1. 算术全部在这里完成：同输入 → 同输出，可单测。
2. 每维 0~10 分，加权求和 → total（0~10）。
3. 硬约束不满足 → feasible=false，不参选（由 censor.py 判定后回写）。
4. 维度语义与权重方向必须与 tests/test_engine.py 的断言一致：
   - social 权重↑ → 高社交选项(opt3, social_value=9)相对 opt1 的分差**增大**
   - commute 权重↓ → 通勤 60min 的 opt1 相对通勤 25min 的 opt2 **变得更有利**
5. 两条产品修正（2026-09-28，修复"说 4 天没运动却推荐在家看电影"的矛盾）：
   - 通勤/花费设"零成本区"：<=15min / <=30元 不再线性加分，
     否则"什么都不做"永远占优（躺平永远赢）。
   - 节律缺口到阈值（rhythm>=9）时给运动类选项硬性偏好加分，
     体现"欠账太多优先补课"——长期目标不该被短期便利线性压过。
"""

from __future__ import annotations

import re
from typing import Any

from .models import (
    DIMENSIONS,
    DIM_MAX,
    DIM_MIN,
    EPSILON,
    Consequence,
    DecisionRequest,
    Option,
    ScoredOption,
    clamp,
)

# ---------------------------------------------------------------- 常量

#: 通勤分钟 → 维度分的换算锚点（确定性，不用 LLM）
COMMUTE_FULL_MIN = 90.0        # 90 分钟通勤 → 0 分
COMMUTE_ZERO_MIN = 0.0         # 0 分钟（锚点，实际给 COMMUTE_FREE_SCORE）
#: 通勤"零成本区"上限：<=15 分钟（步行/就在楼下）一律视为同等便利，不再线性加分。
#: 否则"什么都不做"（0 分钟通勤）会永远压过"步行 5 分钟"——躺平永远赢。
COMMUTE_FREE_MIN = 15.0
#: 零成本区的通勤维度分（不给满分 10，把 1 分留给"真的动了"的选项）
COMMUTE_FREE_SCORE = 9.0
#: 花费（元）→ 维度分的换算锚点
COST_FULL_CNY = 400.0          # 400 元 → 0 分
COST_ZERO_CNY = 0.0            # 0 元（锚点，实际给 COST_FREE_SCORE）
#: 花费"零压力区"上限：<=30 元（一杯奶茶钱）一律视为同等省钱。
COST_FREE_CNY = 30.0
#: 零压力区的花费维度分（同样不给满分，避免"免费"成为决定性优势）
COST_FREE_SCORE = 9.0

#: 未知值的中性分（信息不全时不奖励也不惩罚）
UNKNOWN_DIM = 5.0

#: 节律缺口达到"硬性偏好"阈值时的加分（rhythm >= 该值 且 选项是运动类）。
#: 产品逻辑：欠账太多时"补课"优先——一周该动 3 次却 4 天没动，
#: 短期便利（零通勤/零花费）不该赢。这是阈值判断，不是线性加权能表达的。
RHYTHM_HARD_MIN = 9.0
#: 硬性偏好加分幅度（0~10 分制下的绝对加分）。
#: 定 1.6 而不是更小，依据是 12 个真实 LLM 样本的实测噪声分布：
#: "零通勤零花费在家休息"相对运动选项的最大优势是 1.00 分——那两次 LLM
#: 同时给了电影 social=5（其余 10 次是 1）、pleasure 6~7、health 7~10，
#: 是多个维度同向波动的"坏心情样本"。0.8 的 bonus 盖不住，出现 4/5 抖动。
#: 1.6 在实测极值上留 0.6 余量，可容忍 social 再涨到 7 左右的未见波动。
RHYTHM_HARD_BONUS = 1.6

#: 纠结点关键词 → 维度惩罚（确定性启发式）
_CONCERN_PENALTY = {
    "远": ("commute", 1.2), "通勤": ("commute", 1.0), "地铁": ("commute", 0.4),
    "累": ("health", 1.2), "疲": ("health", 1.0), "困": ("health", 0.8),
    "贵": ("cost", 1.2), "预算": ("cost", 0.8), "省钱": ("cost", -0.6),
    "尬": ("social", 0.8), "一个人": ("social", 0.6), "孤单": ("social", 0.6),
    "无聊": ("pleasure", 1.0), "没意思": ("pleasure", 1.0),
    "担心": ("pleasure", 0.4), "怕": ("pleasure", 0.4),
    "三分钟热度": ("pleasure", 0.8), "热度": ("pleasure", 0.5),
    "洗碗": ("health", 0.5),
}

#: 类别默认值（抽取信息不全时的兜底，全部确定性）
#: 注意：`workout`（运动健身）与 `dance` 同属锻炼类，默认值保持一致，
#: 这样"跑步/健身"与"跳舞"在信息缺失时不会被人为拉开差距。
_CATEGORY_DEFAULTS = {
    "dance": dict(pleasure=7, health_load=5, social_value=6, workout=True),
    "workout": dict(pleasure=7, health_load=5, social_value=6, workout=True),
    "theme_park": dict(pleasure=8, health_load=7, social_value=5, workout=False),
    "food": dict(pleasure=7, health_load=4, social_value=6, workout=False),
    "social": dict(pleasure=6, health_load=4, social_value=8, workout=False),
    "rest": dict(pleasure=6, health_load=-3, social_value=3, workout=False),
    "study": dict(pleasure=5, health_load=3, social_value=4, workout=False),
    "other": dict(pleasure=5, health_load=5, social_value=5, workout=False),
}

#: 类别默认时长/通勤/花费（仅当抽取值为 -1 时用于推演，不写入 Option）
_CATEGORY_HINTS = {
    "dance": dict(duration=90, commute=30, cost=120.0),
    "workout": dict(duration=60, commute=30, cost=120.0),
    "theme_park": dict(duration=240, commute=60, cost=300.0),
    "food": dict(duration=90, commute=20, cost=120.0),
    "social": dict(duration=120, commute=30, cost=80.0),
    "rest": dict(duration=180, commute=5, cost=0.0),
    "study": dict(duration=120, commute=25, cost=100.0),
    "other": dict(duration=90, commute=25, cost=60.0),
}

#: 运动类活动关键词（跑步/健身/游泳…都算"锻炼"，与 dance 同等对待）
_WORKOUT_WORDS = ("健身", "跑步", "游泳", "瑜伽", "爬山", "撸铁", "运动",
                  "锻炼", "跳舞", "舞蹈", "舞")


# ---------------------------------------------------------------- 档案节律

def _is_workout_activity(cat: str, act: str) -> bool:
    """一条历史记录算不算"运动/锻炼"（跳舞、跑步、健身都算）。"""
    c = str(cat or "").lower()
    if c in ("dance", "workout"):
        return True
    a = str(act or "")
    return any(w in a for w in _WORKOUT_WORDS)


def dance_debt_days(profile) -> int:
    """距上次运动类活动过去几天（无记录返回 -1）。

    函数名保留 `dance_debt_days`（测试与 explainer 引用），
    但语义已泛化为"运动/锻炼"：dance 与 workout 类别、以及
    "跑步/健身/游泳/撸铁/运动"等关键词都算。

    天数按**自然日差**算（不是 86400s 整除），否则凌晨跑会少算一天，
    "4 天前"这个演示锚点就不稳。
    """
    if profile is None:
        return -1
    history = getattr(profile, "history", None) or []
    import time as _time
    today = _time.localtime()
    today_key = (today.tm_year, today.tm_mon, today.tm_mday)
    best = -1
    for h in history:
        cat = str(h.get("category", "") if isinstance(h, dict) else "")
        act = str(h.get("activity", "") if isinstance(h, dict) else "")
        date = str(h.get("date", "") if isinstance(h, dict) else "")
        if not _is_workout_activity(cat, act):
            continue
        ts = _parse_date_ts(date)
        if ts is None:
            continue
        d = _time.localtime(ts)
        days = _day_number(today_key) - _day_number((d.tm_year, d.tm_mon, d.tm_mday))
        if best < 0 or days < best:
            best = days
    return best


def _day_number(key) -> int:
    """(y, m, d) → 可比较的日序号（time.mktime 归一化，免手算闰年）。"""
    import time as _time
    try:
        return int(_time.mktime((key[0], key[1], key[2], 12, 0, 0, 0, 0, -1)) // 86400)
    except Exception:
        return 0


def dance_per_week_target(profile) -> float:
    """档案里的每周运动目标（无则 0）。

    兼容 `dance_per_week`（旧 key）与 `workout_per_week`（新 key）。
    """
    if profile is None:
        return 0.0
    for g in (getattr(profile, "goals", None) or []):
        key = str(g.get("key", "") if isinstance(g, dict) else "")
        if key in ("dance_per_week", "workout_per_week", "dance_weekly",
                   "workout_weekly", "跳舞频次", "运动频次"):
            try:
                return float(g.get("target", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def dance_this_week(profile) -> int:
    """本周已运动几次（按 ISO 周一起算）。"""
    if profile is None:
        return 0
    import time as _time
    now = _time.localtime()
    monday = _time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0,
                           0, 0, -1)) - (now.tm_wday * 86400)
    n = 0
    for h in (getattr(profile, "history", None) or []):
        cat = str(h.get("category", "") if isinstance(h, dict) else "")
        act = str(h.get("activity", "") if isinstance(h, dict) else "")
        date = str(h.get("date", "") if isinstance(h, dict) else "")
        if not _is_workout_activity(cat, act):
            continue
        ts = _parse_date_ts(date)
        if ts is not None and ts >= monday:
            n += 1
    return n


def _parse_date_ts(date: str):
    """'2026-09-20' → 本地时间戳；失败返回 None。"""
    if not date:
        return None
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(date).strip())
    if not m:
        return None
    import time as _time
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _time.mktime((y, mo, d, 12, 0, 0, 0, 0, -1))
    except Exception:
        return None


# ---------------------------------------------------------------- 维度分

def _commute_dim(commute_min: int) -> float:
    """通勤越短分越高（0min→9，15min→9，90min→0）。

    `<=15min` 是"零成本区"：步行 5 分钟和 0 分钟通勤在体感上没差别，
    一律给 COMMUTE_FREE_SCORE(9.0)。超过 15 分钟后每多 1 分钟才真的扣分。
    这样"在家躺着"不会因为"零通勤"而永远压过"下楼走两步"。
    """
    if commute_min is None or commute_min < 0:
        return UNKNOWN_DIM
    c = float(commute_min)
    if c <= COMMUTE_FREE_MIN:
        return COMMUTE_FREE_SCORE
    # 从 FREE_SCORE 单调降到 0（必须单调，否则 16min 会比 15min 分还高）
    v = COMMUTE_FREE_SCORE - (c - COMMUTE_FREE_MIN) * COMMUTE_FREE_SCORE / \
        (COMMUTE_FULL_MIN - COMMUTE_FREE_MIN)
    return clamp(v, DIM_MIN, DIM_MAX)


def _cost_dim(cost_cny: float) -> float:
    """花费越低分越高（0元→9，30元→9，400元→0）。

    `<=30元` 是"零压力区"：免费和一杯奶茶钱在钱包体感上没差别，
    一律给 COST_FREE_SCORE(9.0)。超过 30 元后每多 1 元才真的扣分。
    """
    if cost_cny is None or cost_cny < 0:
        return UNKNOWN_DIM
    v = float(cost_cny)
    if v <= COST_FREE_CNY:
        return COST_FREE_SCORE
    # 从 FREE_SCORE 单调降到 0（必须单调，否则 31 元会比 30 元分还高）
    v = COST_FREE_SCORE - (v - COST_FREE_CNY) * COST_FREE_SCORE / \
        (COST_FULL_CNY - COST_FREE_CNY)
    return clamp(v, DIM_MIN, DIM_MAX)


def _rhythm_dim(option: Option, profile) -> float:
    """生活节律缺口：本周该运动 3 次只动了 1 次、且已 4 天没动 → 高分。"""
    cat = (option.category or "").lower()
    is_dance = cat in ("dance", "workout") or bool(getattr(option, "workout", False))
    if not is_dance:
        # 非锻炼类：按"是否需要出门活动"给中性偏上分
        return 5.0
    target = dance_per_week_target(profile)
    done = dance_this_week(profile)
    debt = dance_debt_days(profile)
    score = 5.0
    if target > 0:
        gap = target - done
        score += gap * 2.2            # 差 1 次 +2.2，差 2 次 +4.4
    if debt >= 0:
        if debt >= 4:
            score += 2.4              # "已经 4 天没运动"
        elif debt >= 2:
            score += 1.2
        elif debt <= 1:
            score -= 1.6              # 刚运动过，节律缺口小
    # 用户自己说"这周已经运动过一次"这类纠结点 → 缺口下降
    for c in (getattr(option, "concerns", None) or []):
        if "已经跳" in c or "跳过" in c or "这周跳" in c \
                or "已经运动" in c or "这周运动" in c or "跑过" in c:
            score -= 1.5
    return clamp(score, DIM_MIN, DIM_MAX)


def is_workout_option(option: Option) -> bool:
    """这个选项算不算"运动/锻炼"（与 `_rhythm_dim` 的判定保持一致）。"""
    cat = (option.category or "").lower()
    return cat in ("dance", "workout") or bool(getattr(option, "workout", False))


def rhythm_hard_bonus(dims: dict, option: Option) -> float:
    """节律缺口足够大时，给运动类选项的"硬性偏好"加分。

    产品逻辑（ARCHITECTURE 第 4 节 `rhythm` 维度的本意）：
    一周该运动 3 次却 4 天没动，这是**长期目标**；而"零通勤、零花费、
    在家休息"是**短期便利**。线性加权下三项短期便利加起来很容易
    压过单项长期目标，于是系统一边说"你 4 天没运动了"、
    一边推荐"在家看电影"——自相矛盾，演示时说不通。

    所以这里不用加权，改用阈值：缺口到顶（>= RHYTHM_HARD_MIN）时
    直接给运动类选项加分，体现"欠账太多时优先补课"的真实决策心理。

    非运动类选项、或节律缺口没到阈值的，一律 0 分（不影响其他场景）。
    """
    if not is_workout_option(option):
        return 0.0
    try:
        rhythm = float(dims.get("rhythm", 0.0))
    except (TypeError, ValueError):
        return 0.0
    if rhythm < RHYTHM_HARD_MIN:
        return 0.0
    return RHYTHM_HARD_BONUS


def _social_dim(option: Option, profile) -> float:
    """社交价值：social_value 为主，档案里的"好朋友"事实加分。"""
    base = float(getattr(option, "social_value", 5) or 5)
    label = f"{option.label or ''} {option.place or ''} {option.notes or ''}"
    facts = [str(f) for f in (getattr(profile, "facts", None) or [])]
    for f in facts:
        # "楼下火锅店的老板娘很热情" → 提到该地点的选项加分
        keys = [k for k in re.findall(r"[一-龥A-Za-z0-9]{2,}", f)
                if k not in ("好朋友", "不吃辣", "预算敏感", "偏好步行")]
        if not keys:
            continue
        if any(k in label for k in keys):
            base += 1.5
    for c in (getattr(option, "concerns", None) or []):
        if "一个人" in c or "尬" in c or "孤单" in c:
            base -= 1.2
    return clamp(base, DIM_MIN, DIM_MAX)


def _pleasure_dim(option: Option, profile) -> float:
    """愉悦度：pleasure 为主，纠结点惩罚，档案历史 mood 微调。"""
    base = float(getattr(option, "pleasure", 5) or 5)
    for c in (getattr(option, "concerns", None) or []):
        for kw, (dim, pen) in _CONCERN_PENALTY.items():
            if kw in c and dim == "pleasure":
                base -= pen
    # 历史里同类活动的平均 mood（有则微调）
    moods = []
    for h in (getattr(profile, "history", None) or []):
        cat = str(h.get("category", "") if isinstance(h, dict) else "")
        if cat == (option.category or "").lower() and cat:
            try:
                moods.append(float(h.get("mood", 3)))
            except (TypeError, ValueError):
                pass
    if moods:
        avg = sum(moods) / len(moods)
        base += (avg - 3.0) * 0.8     # mood 5 → +1.6；mood 1 → -1.6
    return clamp(base, DIM_MIN, DIM_MAX)


def _health_dim(option: Option, profile) -> float:
    """健康体力：health_load 越低分越高；rest 类是恢复。"""
    base = 10.0 - float(getattr(option, "health_load", 5) or 5)
    cat = (option.category or "").lower()
    if cat == "rest":
        base += 2.0
    if getattr(option, "workout", False):
        base += 0.8
    for c in (getattr(option, "concerns", None) or []):
        for kw, (dim, pen) in _CONCERN_PENALTY.items():
            if kw in c and dim == "health":
                base -= pen
    # 档案事实："不吃辣" → 吃辣类餐饮扣分
    label = f"{option.label or ''} {option.place or ''} {option.notes or ''}"
    for f in (getattr(profile, "facts", None) or []):
        fs = str(f)
        if ("不吃辣" in fs or "不能吃辣" in fs) and ("辣" in label or "火锅" in label):
            base -= 2.5
    return clamp(base, DIM_MIN, DIM_MAX)


def _future_dim(option: Option, consequence) -> float:
    """对未来 48h 的连锁影响：由 Timeline 给出；没有则按类别中性估计。"""
    if consequence is not None:
        try:
            return clamp(float(consequence.future_score), DIM_MIN, DIM_MAX)
        except (TypeError, ValueError):
            pass
    cat = (option.category or "").lower()
    fallback = {"dance": 7.0, "workout": 7.0, "theme_park": 5.0, "food": 6.0,
                "social": 7.0, "rest": 7.0, "study": 7.0, "other": 5.0}
    return fallback.get(cat, 5.0)


def dims_for(option: Option, profile=None, consequence=None) -> dict:
    """算一个选项的 7 个维度分（确定性，纯函数）。"""
    return {
        "rhythm": round(_rhythm_dim(option, profile), 2),
        "social": round(_social_dim(option, profile), 2),
        "commute": round(_commute_dim(getattr(option, "commute_min", -1)), 2),
        "cost": round(_cost_dim(getattr(option, "cost_cny", -1.0)), 2),
        "pleasure": round(_pleasure_dim(option, profile), 2),
        "health": round(_health_dim(option, profile), 2),
        "future": round(_future_dim(option, consequence), 2),
    }


def total_for(dims: dict, weights: dict) -> float:
    """加权总分（0~10）。权重和不为 1 时先归一化，保证可比。"""
    wsum = 0.0
    acc = 0.0
    for k in DIMENSIONS:
        w = float(weights.get(k, 0.0) or 0.0)
        if w <= 0:
            continue
        wsum += w
        acc += float(dims.get(k, UNKNOWN_DIM)) * w
    if wsum <= 0:
        return 0.0
    return clamp(acc / wsum, 0.0, 10.0)


# ---------------------------------------------------------------- 主入口

def score(request: DecisionRequest, weights: dict | None = None,
          profile=None, consequences: dict | None = None,
          apply_constraints: bool = True) -> list[ScoredOption]:
    """给一个 DecisionRequest 的所有选项打分。

    :param request: DecisionRequest
    :param weights: 维度权重（缺省用 DEFAULT_WEIGHTS）
    :param profile: Profile（用于 rhythm/social/pleasure 维度；可为 None）
    :param consequences: {option_id: Consequence}（Timeline 产物；可为 None）
    :param apply_constraints: 是否在打分时同步跑硬约束闸门（默认 True，
        保证 `scorer.score()` 单独被调用时也满足契约：预算超限 / 到家晚于死线
        → feasible=false 且 blocked_reason 非空）
    :return: list[ScoredOption]，**按 total 降序**（feasible=False 的沉底）
    """
    from .models import DEFAULT_WEIGHTS

    w = dict(DEFAULT_WEIGHTS)
    if isinstance(weights, dict):
        for k in DIMENSIONS:
            if k in weights:
                try:
                    w[k] = float(weights[k])
                except (TypeError, ValueError):
                    pass
    consequences = consequences or {}

    out: list[ScoredOption] = []
    for opt in (getattr(request, "options", None) or []):
        cons = consequences.get(getattr(opt, "id", ""))
        dims = dims_for(opt, profile, cons)
        total = total_for(dims, w)
        # 节律缺口到阈值 → 运动类选项的硬性偏好加分（见 rhythm_hard_bonus 文档）
        total += rhythm_hard_bonus(dims, opt)
        out.append(ScoredOption(option=opt, dims=dims, total=round(total, 2),
                                feasible=True, blocked_reason="",
                                consequence=cons, reasons=[]))

    # ---- 硬约束闸门（censor 纯函数版；scorer 单独被调用时也要满足契约） ----
    if apply_constraints:
        try:
            from . import censor as _censor

            _censor.censor(request, out, profile=profile)
        except Exception:
            pass

    # 稳定排序：可行的按 total 降序，feasible=False 沉底
    return resort(out)


# 兼容 tests/test_engine.py 尝试的多个入口名
def score_request(request, weights=None, profile=None, consequences=None):
    return score(request, weights=weights, profile=profile,
                 consequences=consequences)


def score_all(request, weights=None, profile=None, consequences=None):
    return score(request, weights=weights, profile=profile,
                 consequences=consequences)


def score_options(request, weights=None, profile=None, consequences=None):
    return score(request, weights=weights, profile=profile,
                 consequences=consequences)


def resort(scored: list[ScoredOption]) -> list[ScoredOption]:
    """censor 之后重排：可行的按 total 降序，不可行的沉底。"""
    feas = [s for s in scored if s.feasible]
    infeas = [s for s in scored if not s.feasible]
    feas.sort(key=lambda s: (-float(s.total), str(s.option.id)))
    infeas.sort(key=lambda s: (-float(s.total), str(s.option.id)))
    return feas + infeas


def top_gap(scored: list[ScoredOption]) -> float:
    """可行选项里 top1 与 top2 的总分差（不足 2 个返回 inf 意义上的 999）。"""
    feas = [s for s in scored if s.feasible]
    if len(feas) < 2:
        return 999.0
    return float(feas[0].total) - float(feas[1].total)


def is_pseudo_choice(scored: list[ScoredOption], epsilon: float = EPSILON) -> bool:
    """top2 分差 < epsilon → "伪选择"，该出 DROP 牌。"""
    return top_gap(scored) < float(epsilon)


__all__ = [
    "score", "score_request", "score_all", "score_options", "dims_for",
    "total_for", "resort", "top_gap", "is_pseudo_choice",
    "dance_debt_days", "dance_per_week_target", "dance_this_week",
    "is_workout_option", "rhythm_hard_bonus",
    "COMMUTE_FULL_MIN", "COST_FULL_CNY",
    "COMMUTE_FREE_MIN", "COMMUTE_FREE_SCORE",
    "COST_FREE_CNY", "COST_FREE_SCORE",
    "RHYTHM_HARD_MIN", "RHYTHM_HARD_BONUS",
]
