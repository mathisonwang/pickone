"""数据模型（纯 stdlib，任何解释器可导入）。

字段名即 JSON 字段名，与 contracts/data_model.md 一致。
所有类提供 to_dict() / from_dict()，缺字段用默认值，未知字段忽略，不抛异常。
"""

import json
import time

# ---------------------------------------------------------------- 维度常量

DIMENSIONS = [
    "rhythm",    # 生活节律缺口
    "social",    # 社交价值
    "commute",   # 通勤/时间成本（越低越差的原始分，内部已反向）
    "cost",      # 花费
    "pleasure",  # 愉悦度/期待感
    "health",    # 健康与体力
    "future",    # 对未来 48h 的连锁影响
]

DIM_LABELS = {
    "rhythm": "生活节律",
    "social": "社交",
    "commute": "通勤成本",
    "cost": "花费",
    "pleasure": "愉悦度",
    "health": "健康体力",
    "future": "后续影响",
}

DEFAULT_WEIGHTS = {
    "rhythm": 0.22,
    "social": 0.18,
    "commute": 0.16,
    "cost": 0.10,
    "pleasure": 0.16,
    "health": 0.12,
    "future": 0.06,
}

WEIGHT_MIN = 0.05
WEIGHT_MAX = 0.35
LEARNING_RATE = 0.05
EPSILON = 1.2          # 伪选择阈值（总分差）
DIM_MIN = 0.0
DIM_MAX = 10.0

CATEGORIES = [
    "dance", "workout", "theme_park", "food", "social", "rest", "study", "other",
]


# ---------------------------------------------------------------- 工具函数

def _as_str(v, default=""):
    if v is None:
        return default
    if isinstance(v, str):
        return v
    return str(v)


def _as_int(v, default=0):
    if v is None:
        return default
    try:
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip()
        if not s:
            return default
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _as_float(v, default=0.0):
    if v is None:
        return default
    try:
        if isinstance(v, bool):
            return float(v)
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if not s:
            return default
        return float(s)
    except (TypeError, ValueError):
        return default


def _as_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1", "是"):
        return True
    if s in ("false", "no", "n", "0", "否", ""):
        return False
    return default


def _as_list(v):
    """任意值 -> list[str]，容忍 None / 单字符串 / 非列表。"""
    if v is None:
        return []
    if isinstance(v, list):
        out = []
        for x in v:
            if x is None:
                continue
            if isinstance(x, str):
                if x.strip():
                    out.append(x.strip())
            elif isinstance(x, (int, float, bool)):
                out.append(str(x))
            else:
                s = _safe_json(x)
                if s:
                    out.append(s)
        return out
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    return [str(v)]


def _safe_json(obj):
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


def clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo
    if v != v:  # NaN
        return lo
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- 数据类


class Option:
    def __init__(self, id="", label="", category="other", start_hint="",
                 duration_min=-1, place="", commute_min=-1, cost_cny=-1.0,
                 social_value=5, pleasure=5, health_load=5, workout=False,
                 depends_on=None, concerns=None, after=None, notes=""):
        self.id = _as_str(id)
        self.label = _as_str(label)
        self.category = _as_str(category, "other")
        self.start_hint = _as_str(start_hint)
        self.duration_min = _as_int(duration_min, -1)
        self.place = _as_str(place)
        self.commute_min = _as_int(commute_min, -1)
        self.cost_cny = _as_float(cost_cny, -1.0)
        self.social_value = int(clamp(social_value, 0, 10))
        self.pleasure = int(clamp(pleasure, 0, 10))
        self.health_load = int(clamp(health_load, 0, 10))
        self.workout = _as_bool(workout, False)
        self.depends_on = _as_list(depends_on)
        self.concerns = _as_list(concerns)
        self.after = _as_list(after)
        self.notes = _as_str(notes)

    def to_dict(self):
        return {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "start_hint": self.start_hint,
            "duration_min": self.duration_min,
            "place": self.place,
            "commute_min": self.commute_min,
            "cost_cny": self.cost_cny,
            "social_value": self.social_value,
            "pleasure": self.pleasure,
            "health_load": self.health_load,
            "workout": self.workout,
            "depends_on": list(self.depends_on),
            "concerns": list(self.concerns),
            "after": list(self.after),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        return cls(
            id=d.get("id", ""), label=d.get("label", ""),
            category=d.get("category", "other"),
            start_hint=d.get("start_hint", ""),
            duration_min=d.get("duration_min", -1),
            place=d.get("place", ""),
            commute_min=d.get("commute_min", -1),
            cost_cny=d.get("cost_cny", -1.0),
            social_value=d.get("social_value", 5),
            pleasure=d.get("pleasure", 5),
            health_load=d.get("health_load", 5),
            workout=d.get("workout", False),
            depends_on=d.get("depends_on"),
            concerns=d.get("concerns"),
            after=d.get("after"),
            notes=d.get("notes", ""),
        )

    def __repr__(self):
        return f"Option({self.id}:{self.label})"


class DecisionRequest:
    def __init__(self, raw_text="", when="", options=None, hard_constraints=None,
                 missing_info=None, extracted_ok=False):
        self.raw_text = _as_str(raw_text)
        self.when = _as_str(when)
        self.options = [o if isinstance(o, Option) else Option.from_dict(o)
                        for o in (options or [])]
        self.hard_constraints = _as_list(hard_constraints)
        self.missing_info = _as_list(missing_info)
        self.extracted_ok = _as_bool(extracted_ok, False)

    def to_dict(self):
        return {
            "raw_text": self.raw_text,
            "when": self.when,
            "options": [o.to_dict() for o in self.options],
            "hard_constraints": list(self.hard_constraints),
            "missing_info": list(self.missing_info),
            "extracted_ok": self.extracted_ok,
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        return cls(
            raw_text=d.get("raw_text", ""), when=d.get("when", ""),
            options=d.get("options") or [],
            hard_constraints=d.get("hard_constraints"),
            missing_info=d.get("missing_info"),
            extracted_ok=d.get("extracted_ok", False),
        )

    def __repr__(self):
        return f"DecisionRequest(n_options={len(self.options)}, ok={self.extracted_ok})"


class Consequence:
    def __init__(self, option_id="", horizon="48h", effects=None,
                 future_score=5, risk=""):
        self.option_id = _as_str(option_id)
        self.horizon = _as_str(horizon, "48h")
        self.effects = _norm_effects(effects)
        self.future_score = int(clamp(future_score, 0, 10))
        self.risk = _as_str(risk)

    def to_dict(self):
        return {
            "option_id": self.option_id,
            "horizon": self.horizon,
            "effects": [dict(e) for e in self.effects],
            "future_score": self.future_score,
            "risk": self.risk,
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        return cls(
            option_id=d.get("option_id", ""), horizon=d.get("horizon", "48h"),
            effects=d.get("effects"), future_score=d.get("future_score", 5),
            risk=d.get("risk", ""),
        )

    def __repr__(self):
        return f"Consequence({self.option_id}, future={self.future_score})"


def _norm_effects(v):
    """effects -> [{"time":str,"desc":str,"impact":int(-2..2)}]，容忍脏数据。"""
    out = []
    if not isinstance(v, list):
        return out
    for item in v:
        if isinstance(item, dict):
            desc = _as_str(item.get("desc") or item.get("description")
                           or item.get("effect") or item.get("text"))
            if not desc:
                continue
            out.append({
                "time": _as_str(item.get("time") or item.get("when"), "之后"),
                "desc": desc,
                "impact": int(clamp(item.get("impact", 0), -2, 2)),
            })
        elif isinstance(item, str) and item.strip():
            out.append({"time": "之后", "desc": item.strip(), "impact": 0})
    return out


class ScoredOption:
    def __init__(self, option=None, dims=None, total=0.0, feasible=True,
                 blocked_reason="", consequence=None, reasons=None):
        self.option = option if isinstance(option, Option) else Option.from_dict(option)
        self.dims = _norm_dims(dims)
        self.total = round(clamp(total, 0, 10), 2)
        self.feasible = _as_bool(feasible, True)
        self.blocked_reason = _as_str(blocked_reason)
        self.consequence = (consequence if isinstance(consequence, Consequence)
                            else Consequence.from_dict(consequence)
                            if consequence else None)
        self.reasons = _as_list(reasons)

    def to_dict(self):
        return {
            "option": self.option.to_dict(),
            "dims": dict(self.dims),
            "total": self.total,
            "feasible": self.feasible,
            "blocked_reason": self.blocked_reason,
            "consequence": self.consequence.to_dict() if self.consequence else None,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        return cls(
            option=d.get("option"), dims=d.get("dims"), total=d.get("total", 0.0),
            feasible=d.get("feasible", True),
            blocked_reason=d.get("blocked_reason", ""),
            consequence=d.get("consequence"), reasons=d.get("reasons"),
        )

    def __repr__(self):
        return f"ScoredOption({self.option.id}, total={self.total}, ok={self.feasible})"


def _norm_dims(v):
    out = {}
    src = v if isinstance(v, dict) else {}
    for k in DIMENSIONS:
        out[k] = round(clamp(src.get(k, 5.0), DIM_MIN, DIM_MAX), 2)
    return out


class Card:
    def __init__(self, kind="GO", option_id=None, title="", why=None, detail=""):
        self.kind = _as_str(kind, "GO")
        self.option_id = _as_str(option_id) if option_id is not None else None
        self.title = _as_str(title)
        self.why = _as_list(why)
        self.detail = _as_str(detail)

    def to_dict(self):
        return {
            "kind": self.kind,
            "option_id": self.option_id,
            "title": self.title,
            "why": list(self.why),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        return cls(
            kind=d.get("kind", "GO"), option_id=d.get("option_id"),
            title=d.get("title", ""), why=d.get("why"), detail=d.get("detail", ""),
        )

    def __repr__(self):
        return f"Card({self.kind}:{self.option_id})"


class Decision:
    def __init__(self, decision_id="", created_at=None, request=None, scored=None,
                 cards=None, confidence=0.5, most_uncertain="", ask_user="",
                 reasoning_tree=None, degraded=False, blocked=None,
                 weights_used=None):
        self.decision_id = _as_str(decision_id)
        self.created_at = _as_float(created_at, time.time())
        self.request = (request if isinstance(request, DecisionRequest)
                        else DecisionRequest.from_dict(request))
        self.scored = [s if isinstance(s, ScoredOption) else ScoredOption.from_dict(s)
                       for s in (scored or [])]
        self.cards = [c if isinstance(c, Card) else Card.from_dict(c)
                      for c in (cards or [])]
        self.confidence = round(clamp(confidence, 0.0, 1.0), 2)
        self.most_uncertain = _as_str(most_uncertain)
        self.ask_user = _as_str(ask_user)
        self.reasoning_tree = _norm_tree(reasoning_tree)
        self.degraded = _as_bool(degraded, False)
        self.blocked = _norm_blocked(blocked)
        self.weights_used = _norm_weights(weights_used)

    def to_dict(self):
        return {
            "decision_id": self.decision_id,
            "created_at": self.created_at,
            "request": self.request.to_dict(),
            "scored": [s.to_dict() for s in self.scored],
            "cards": [c.to_dict() for c in self.cards],
            "confidence": self.confidence,
            "most_uncertain": self.most_uncertain,
            "ask_user": self.ask_user,
            "reasoning_tree": self.reasoning_tree,
            "degraded": self.degraded,
            "blocked": [dict(b) for b in self.blocked],
            "weights_used": dict(self.weights_used),
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        return cls(
            decision_id=d.get("decision_id", ""), created_at=d.get("created_at"),
            request=d.get("request"), scored=d.get("scored") or [],
            cards=d.get("cards") or [], confidence=d.get("confidence", 0.5),
            most_uncertain=d.get("most_uncertain", ""),
            ask_user=d.get("ask_user", ""),
            reasoning_tree=d.get("reasoning_tree"), degraded=d.get("degraded", False),
            blocked=d.get("blocked") or [], weights_used=d.get("weights_used"),
        )

    def __repr__(self):
        return (f"Decision({self.decision_id}, cards={[c.kind for c in self.cards]}, "
                f"conf={self.confidence}, degraded={self.degraded})")


def _norm_tree(v):
    """保证 reasoning_tree 形如 {"root":str,"children":[{agent,summary,children}]}。"""
    if not isinstance(v, dict):
        return {"root": "", "children": []}
    out = {"root": _as_str(v.get("root")), "children": []}
    for ch in (v.get("children") or []):
        out["children"].append(_norm_node(ch))
    return out


def _norm_node(v):
    if not isinstance(v, dict):
        return {"agent": _as_str(v), "summary": "", "children": []}
    return {
        "agent": _as_str(v.get("agent")),
        "summary": _as_str(v.get("summary")),
        "children": [_norm_node(c) for c in (v.get("children") or [])],
    }


def _norm_blocked(v):
    out = []
    if not isinstance(v, list):
        return out
    for item in v:
        if isinstance(item, dict):
            out.append({"option_id": _as_str(item.get("option_id")),
                        "reason": _as_str(item.get("reason"))})
        elif isinstance(item, str) and item.strip():
            out.append({"option_id": "", "reason": item.strip()})
    return out


def _norm_weights(v):
    out = {}
    src = v if isinstance(v, dict) else {}
    for k in DIMENSIONS:
        out[k] = round(float(src.get(k, DEFAULT_WEIGHTS[k])), 4)
    return out


class Profile:
    def __init__(self, user_id="demo", nickname="", goals=None, history=None,
                 weights=None, facts=None, updated_at=None, category_prefs=None):
        self.user_id = _as_str(user_id, "demo")
        self.nickname = _as_str(nickname)
        self.goals = _norm_goals(goals)
        self.history = _norm_history(history)
        self.weights = _norm_weights(weights)
        self.facts = _as_list(facts)
        self.updated_at = _as_float(updated_at, time.time())
        #: 类别偏好累积（后悔闭环的中间产物）：[{"category","mood","ts"}]
        self.category_prefs = _as_list(category_prefs)

    def to_dict(self):
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "goals": [dict(g) for g in self.goals],
            "history": [dict(h) for h in self.history],
            "weights": dict(self.weights),
            "facts": list(self.facts),
            "updated_at": self.updated_at,
            "category_prefs": [dict(p) for p in self.category_prefs
                               if isinstance(p, dict)],
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        return cls(
            user_id=d.get("user_id", "demo"), nickname=d.get("nickname", ""),
            goals=d.get("goals"), history=d.get("history"),
            weights=d.get("weights"), facts=d.get("facts"),
            updated_at=d.get("updated_at"),
            category_prefs=d.get("category_prefs"),
        )

    def __repr__(self):
        return f"Profile({self.user_id}, facts={len(self.facts)}, hist={len(self.history)})"


def _norm_goals(v):
    out = []
    if not isinstance(v, list):
        return out
    for item in v:
        if isinstance(item, dict):
            out.append({
                "key": _as_str(item.get("key")),
                "target": _as_float(item.get("target", 0), 0),
                "unit": _as_str(item.get("unit", "")),
            })
    return out


def _norm_history(v):
    out = []
    if not isinstance(v, list):
        return out
    for item in v:
        if not isinstance(item, dict):
            continue
        out.append({
            "date": _as_str(item.get("date")),
            "activity": _as_str(item.get("activity")),
            "category": _as_str(item.get("category", "other")),
            "duration_min": _as_int(item.get("duration_min", -1), -1),
            "mood": int(clamp(item.get("mood", 3), 1, 5)),
        })
    return out
