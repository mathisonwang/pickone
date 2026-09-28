"""server/engine_bridge.py — 引擎适配层

契约接口（engine/ 由另一位同事实现，可能尚未就绪或中途变动）：
    from engine.advisor  import advise      # advise(request, profile, use_llm=True) -> Decision
    from engine.extractor import extract    # extract(text, use_llm=True) -> DecisionRequest
    from engine.profile  import load_profile, save_profile, reset_profile, log_activity, apply_feedback
    from engine.models   import DecisionRequest, Decision, Profile

本模块职责：
1. 尝试 import 真引擎；成功则透传调用（对象自动 to_dict() 序列化）。
2. import 失败 / 调用抛异常 → 用本文件的 stub 返回**结构合法**的假数据（degraded=true），
   保证服务永远能起、永远不 500。
3. 每次调用打日志标明 real engine 还是 stub。

对 app.py 暴露的统一函数（全部接收/返回 **dict**，不抛异常）：
    bridge.advise(request_dict, profile_dict, use_llm=True) -> Decision dict
    bridge.extract(text, use_llm=True)                      -> DecisionRequest dict
    bridge.profile_load(user_id)    -> Profile dict（可能为默认档案）
    bridge.profile_save(profile_dict) -> Profile dict
    bridge.profile_reset(user_id, preset) -> Profile dict
    bridge.profile_log_activity(user_id, activity) -> bool
    bridge.apply_feedback(profile_dict, feedback_dict) -> Profile dict
    bridge.engine_status() -> {"advisor":..,"extractor":..,"profile":..,"models":..}
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

log = logging.getLogger("pickone.bridge")

# ---------------------------------------------------------------- 可用性探测

ENGINE_AVAILABLE = {
    "advisor": False,
    "extractor": False,
    "profile": False,
    "models": False,
}

_advise = None
_extract = None
_p_load = _p_save = _p_reset = _p_log = _p_apply = None
_models = None

try:
    from engine import models as _models  # type: ignore
    ENGINE_AVAILABLE["models"] = True
except Exception as e:  # noqa: BLE001
    log.warning("[bridge] engine.models 不可用（将使用 stub 数据模型）: %r", e)

try:
    from engine.advisor import advise as _advise  # type: ignore
    ENGINE_AVAILABLE["advisor"] = True
except Exception as e:  # noqa: BLE001
    log.warning("[bridge] engine.advisor.advise 不可用 → stub: %r", e)

try:
    from engine.extractor import extract as _extract  # type: ignore
    ENGINE_AVAILABLE["extractor"] = True
except Exception as e:  # noqa: BLE001
    log.warning("[bridge] engine.extractor.extract 不可用 → stub: %r", e)

try:
    from engine.profile import (  # type: ignore
        load_profile as _p_load,
        save_profile as _p_save,
        reset_profile as _p_reset,
        log_activity as _p_log,
        apply_feedback as _p_apply,
    )
    ENGINE_AVAILABLE["profile"] = True
except Exception as e:  # noqa: BLE001
    log.warning("[bridge] engine.profile 不可用 → stub: %r", e)

log.info("[bridge] 引擎可用性: %s", ENGINE_AVAILABLE)


def engine_status() -> dict:
    """各引擎模块 real / stub 状态。"""
    return {k: ("real" if v else "stub") for k, v in ENGINE_AVAILABLE.items()}


# ---------------------------------------------------------------- 序列化辅助

def to_dict(obj):
    """引擎对象（dataclass with to_dict）→ dict；已是 dict 原样返回。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, (list, tuple)):
        return [to_dict(x) for x in obj]
    d = getattr(obj, "to_dict", None)
    if callable(d):
        try:
            return d()
        except Exception as e:  # noqa: BLE001
            log.warning("[bridge] %s.to_dict() 失败: %r", type(obj).__name__, e)
    if hasattr(obj, "__dataclass_fields__"):
        try:
            return {k: to_dict(getattr(obj, k)) for k in obj.__dataclass_fields__}
        except Exception:  # noqa: BLE001
            pass
    try:
        json.dumps(obj)
        return obj
    except Exception:  # noqa: BLE001
        return str(obj)


def _from_models(cls_name: str, d: dict):
    """优先用 engine.models 的 from_dict 构造对象；不可用返回原 dict。"""
    if _models is not None:
        cls = getattr(_models, cls_name, None)
        if cls is not None:
            try:
                return cls.from_dict(d or {})
            except Exception as e:  # noqa: BLE001
                log.warning("[bridge] engine.models.%s.from_dict 失败: %r", cls_name, e)
    return d or {}


# ---------------------------------------------------------------- stub 数据模型

DEFAULT_WEIGHTS = {
    "rhythm": 0.22, "social": 0.18, "commute": 0.16,
    "cost": 0.10, "pleasure": 0.16, "health": 0.12, "future": 0.06,
}

VALID_CATEGORIES = {"dance", "workout", "theme_park", "food", "social", "rest",
                    "study", "other"}


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def stub_option(option_id: str, label: str, **kw) -> dict:
    return {
        "id": option_id,
        "label": label,
        "category": kw.get("category", "other"),
        "start_hint": kw.get("start_hint", ""),
        "duration_min": int(_num(kw.get("duration_min", -1), -1)),
        "place": kw.get("place", ""),
        "commute_min": int(_num(kw.get("commute_min", -1), -1)),
        "cost_cny": _num(kw.get("cost_cny", -1), -1),
        "social_value": int(_num(kw.get("social_value", 5), 5)),
        "pleasure": int(_num(kw.get("pleasure", 5), 5)),
        "health_load": int(_num(kw.get("health_load", 5), 5)),
        "workout": bool(kw.get("workout", False)),
        "depends_on": list(kw.get("depends_on") or []),
        "concerns": list(kw.get("concerns") or []),
        "after": list(kw.get("after") or []),
        "notes": kw.get("notes", ""),
    }


def stub_request(raw_text: str = "", **kw) -> dict:
    return {
        "raw_text": raw_text,
        "when": kw.get("when", ""),
        "options": [to_dict(o) if not isinstance(o, dict) else o
                    for o in (kw.get("options") or [])],
        "hard_constraints": list(kw.get("hard_constraints") or []),
        "missing_info": list(kw.get("missing_info") or []),
        "extracted_ok": bool(kw.get("extracted_ok", False)),
    }


# ---------------------------------------------------------------- stub 抽取器

_SPLIT_RE = re.compile(r"[，,。；;\n！!？?]+|\s{2,}|(?:或者|还是|要么|vs\.?|VS)")
_TIME_RE = re.compile(r"(今晚|今晚上|明天|后天|周六|周日|星期天|周末|本周|这周|下周|中午|下午|晚上|上午|早上|\d{1,2}[:：]\d{2})")
_WHEN_WORDS = ("今晚", "今晚上", "明天", "后天", "周六", "周日", "星期天", "周末",
               "本周", "这周", "下周", "中午", "下午", "晚上", "上午", "早上")


def stub_extract(text: str) -> dict:
    """正则兜底抽取：把输入按分隔符切成候选选项。extracted_ok=False。"""
    text = (text or "").strip()
    when = ""
    m = _TIME_RE.search(text)
    if m:
        when = m.group(1)
    parts = [p.strip(" 、的了吧呢啊哦呀~～") for p in _SPLIT_RE.split(text) if p and p.strip()]
    # 过滤掉纯时间/疑问碎片
    options = []
    for i, p in enumerate(parts[:8], start=1):
        if len(p) < 2 or p in _WHEN_WORDS:
            continue
        label = p[:40]
        cat = "other"
        low = label.lower()
        if any(k in label for k in ("跳舞", "舞蹈")) or "dance" in low:
            cat = "dance"
        elif any(k in label for k in ("健身", "跑步", "游泳", "瑜伽", "爬山", "撸铁",
                                      "运动", "锻炼")):
            cat = "workout"
        elif any(k in label for k in ("迪士尼", "乐园", "游乐")):
            cat = "theme_park"
        elif any(k in label for k in ("吃", "餐", "饭", "火锅", "烧烤")):
            cat = "food"
        elif any(k in label for k in ("朋友", "聚", "社交")):
            cat = "social"
        elif any(k in label for k in ("睡", "休息", "躺")):
            cat = "rest"
        elif any(k in label for k in ("学习", "看书", "课", "作业")):
            cat = "study"
        options.append(stub_option(f"opt{i}", label, category=cat))
    missing = []
    if not options:
        missing.append("没能识别出任何候选选项，请分别列出你在纠结的几个选项")
    missing.append("各选项的时间、花费、通勤距离（补充后建议会更准）")
    return stub_request(raw_text=text, when=when, options=options,
                        missing_info=missing, extracted_ok=False)


# ---------------------------------------------------------------- stub 决策器

def _score_stub_option(opt: dict) -> tuple[dict, float]:
    """粗糙启发式打分（0-10），仅用于 stub 模式占位。"""
    dims = {
        "rhythm": 5.0,
        "social": _num(opt.get("social_value", 5), 5),
        "commute": max(0.0, 10.0 - _num(opt.get("commute_min", 20), 20) / 6.0)
                   if _num(opt.get("commute_min", -1), -1) >= 0 else 5.0,
        "cost": max(0.0, 10.0 - _num(opt.get("cost_cny", 50), 50) / 20.0)
                if _num(opt.get("cost_cny", -1), -1) >= 0 else 5.0,
        "pleasure": _num(opt.get("pleasure", 5), 5),
        "health": max(0.0, 10.0 - _num(opt.get("health_load", 5), 5)),
        "future": 5.0,
    }
    total = sum(dims[k] * DEFAULT_WEIGHTS[k] for k in DEFAULT_WEIGHTS) / sum(DEFAULT_WEIGHTS.values())
    return dims, round(total, 2)


def stub_advise(request: dict, profile: dict | None = None) -> dict:
    """构造结构完全合法的假 Decision（degraded=true）。"""
    request = to_dict(request) or {}
    profile = to_dict(profile) or {}
    decision_id = "dec-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    options = request.get("options") or []

    scored = []
    for opt in options:
        opt = to_dict(opt) or {}
        dims, total = _score_stub_option(opt)
        scored.append({
            "option": opt,
            "dims": dims,
            "total": total,
            "feasible": True,
            "blocked_reason": "",
            "consequence": {
                "option_id": opt.get("id", ""),
                "horizon": "48h",
                "effects": [
                    {"time": "tonight", "desc": "（stub 模式）未做真实推演", "impact": 0},
                ],
                "future_score": 5,
                "risk": "",
            },
            "reasons": [
                "stub 模式：引擎未加载，此分数为占位启发式结果",
                f"通勤约 {opt.get('commute_min', -1)} 分钟，花费约 {opt.get('cost_cny', -1)} 元",
            ],
        })
    scored.sort(key=lambda s: s["total"], reverse=True)

    cards = []
    if scored:
        top = scored[0]
        cards.append({
            "kind": "GO",
            "option_id": top["option"].get("id"),
            "title": f"就它了：{top['option'].get('label', '第一项')}",
            "why": ["stub 模式：真实引擎不可用", "该选项占位总分最高，仅供演示界面"],
            "detail": "引擎接入后此处会给出引用你档案的真实理由。",
        })
        if len(scored) >= 2 and (scored[0]["total"] - scored[1]["total"]) < 1.2:
            cards.append({
                "kind": "DROP",
                "option_id": None,
                "title": "不值得纠结",
                "why": ["前两名分差很小", "闭眼选哪个都不会错"],
                "detail": "stub 占位建议。",
            })
    else:
        cards.append({
            "kind": "GO",
            "option_id": None,
            "title": "没看出你在纠结什么",
            "why": ["没能识别出候选选项"],
            "detail": "请把几个选项分别列出来，例如：A…还是 B…。",
        })

    return {
        "decision_id": decision_id,
        "created_at": time.time(),
        "request": request,
        "scored": scored,
        "cards": cards,
        "confidence": 0.1,
        "most_uncertain": "引擎状态（当前为 stub 占位结果）",
        "ask_user": "各选项大概几点开始、花多少钱、离你多远？",
        "reasoning_tree": {
            "root": "stub 决策（engine 不可用）",
            "children": [{"agent": "server_stub", "summary": "未调用 LLM，纯占位打分", "children": []}],
        },
        "degraded": True,
        "blocked": [],
        "weights_used": dict(DEFAULT_WEIGHTS),
    }


# ---------------------------------------------------------------- stub 档案

def stub_profile(user_id: str, preset: str = "new") -> dict:
    p = {
        "user_id": user_id,
        "nickname": user_id,
        "goals": [],
        "history": [],
        "weights": dict(DEFAULT_WEIGHTS),
        "facts": [],
        "updated_at": time.time(),
    }
    if preset == "veteran":
        p["goals"] = [{"key": "workout_per_week", "target": 3, "unit": "次/周"}]
        p["facts"] = ["楼下火锅店的老板娘很热情", "不吃辣", "每周想运动 3 次"]
        now = time.time()
        p["history"] = [
            {"date": time.strftime("%Y-%m-%d", time.localtime(now - 86400 * d)),
             "activity": a, "category": c, "duration_min": 90, "mood": m}
            for d, a, c, m in [(2, "在家休息", "rest", 3),
                               (4, "去健身房跑步", "workout", 5),
                               (7, "去楼下那家火锅店", "food", 5),
                               (9, "在家看电影", "rest", 4)]
        ]
    return p


def _clamp_weights(w: dict) -> dict:
    out = {}
    for k, v in w.items():
        out[k] = min(0.35, max(0.05, _num(v, 0.1)))
    s = sum(out.values()) or 1.0
    return {k: round(v / s, 4) for k, v in out.items()}


def stub_apply_feedback(profile: dict, feedback: dict) -> dict:
    """学习率 0.05 微调权重（ARCHITECTURE 第 5 节）。"""
    profile = dict(profile or stub_profile(feedback.get("user_id", "demo")))
    w = dict(profile.get("weights") or DEFAULT_WEIGHTS)
    try:
        sat = min(5.0, max(1.0, _num(feedback.get("satisfaction", 3), 3)))
    except Exception:  # noqa: BLE001
        sat = 3.0
    delta = (sat - 3.0) * 0.05  # 满意↑ 愉悦/社交权重↑，不满意反之
    went = bool(feedback.get("went", True))
    chosen = feedback.get("chosen", "")
    boost = {"pleasure": 1.0, "social": 0.6}
    if not went:
        boost = {"commute": 0.8, "cost": 0.5}  # 放鸽子 → 更看重成本
    for k, scale in boost.items():
        if k in w:
            w[k] = w[k] + delta * scale
    profile["weights"] = _clamp_weights(w)
    if chosen:
        note = f"曾选择「{chosen}」，满意度 {int(sat)}/5"
        facts = list(profile.get("facts") or [])
        if note not in facts:
            facts.append(note)
        profile["facts"] = facts[-50:]
    profile["updated_at"] = time.time()
    return profile


# ---------------------------------------------------------------- 统一对外接口

def extract(text: str, use_llm: bool = True) -> dict:
    """自然语言 → DecisionRequest dict。绝不抛异常。"""
    text = text if isinstance(text, str) else str(text or "")
    if ENGINE_AVAILABLE["extractor"] and _extract is not None:
        try:
            req = _extract(text, use_llm=use_llm)
            d = to_dict(req)
            if isinstance(d, dict) and "options" in d:
                log.info("[bridge] extract 使用 REAL engine（options=%d）", len(d.get("options") or []))
                return d
            log.warning("[bridge] real extract 返回结构异常，回落 stub: %r", type(d))
        except Exception as e:  # noqa: BLE001
            log.warning("[bridge] real extract 抛异常，回落 stub: %r", e)
    log.info("[bridge] extract 使用 STUB")
    return stub_extract(text)


def advise(request, profile=None, use_llm: bool = True) -> dict:
    """DecisionRequest dict + Profile dict → Decision dict。绝不抛异常。"""
    req_dict = to_dict(request) or {}
    prof_dict = to_dict(profile)
    if ENGINE_AVAILABLE["advisor"] and _advise is not None:
        try:
            req_obj = _from_models("DecisionRequest", req_dict)
            prof_obj = _from_models("Profile", prof_dict) if prof_dict is not None else None
            dec = _advise(req_obj, prof_obj, use_llm=use_llm)
            d = to_dict(dec)
            if isinstance(d, dict) and ("cards" in d or "scored" in d):
                d.setdefault("decision_id", req_dict.get("decision_id") or
                             "dec-" + uuid.uuid4().hex[:8])
                d.setdefault("created_at", time.time())
                log.info("[bridge] advise 使用 REAL engine")
                return d
            log.warning("[bridge] real advise 返回结构异常，回落 stub: %r", type(d))
        except Exception as e:  # noqa: BLE001
            log.warning("[bridge] real advise 抛异常，回落 stub: %r", e, exc_info=True)
    log.info("[bridge] advise 使用 STUB（degraded=true）")
    return stub_advise(req_dict, prof_dict)


def profile_load(user_id: str) -> dict:
    """返回 Profile dict（不存在则给默认档案，不落盘）。"""
    if ENGINE_AVAILABLE["profile"] and _p_load is not None:
        try:
            p = _p_load(user_id)
            d = to_dict(p)
            if isinstance(d, dict) and d:
                log.info("[bridge] profile_load REAL (%s)", user_id)
                return d
        except Exception as e:  # noqa: BLE001
            log.warning("[bridge] real load_profile 异常，回落 stub: %r", e)
    return stub_profile(user_id, "new")


def profile_save(profile_dict: dict) -> dict:
    if ENGINE_AVAILABLE["profile"] and _p_save is not None:
        try:
            obj = _from_models("Profile", profile_dict)
            r = _p_save(obj)
            if r is not None:
                return to_dict(r)
            return profile_dict
        except Exception as e:  # noqa: BLE001
            log.warning("[bridge] real save_profile 异常: %r", e)
    return profile_dict


def profile_reset(user_id: str, preset: str = "new") -> dict:
    preset = preset if preset in ("new", "veteran") else "new"
    if ENGINE_AVAILABLE["profile"] and _p_reset is not None:
        try:
            r = _p_reset(user_id, preset)
            d = to_dict(r)
            if isinstance(d, dict) and d:
                log.info("[bridge] profile_reset REAL (%s, %s)", user_id, preset)
                return d
        except Exception as e:  # noqa: BLE001
            log.warning("[bridge] real reset_profile 异常，回落 stub: %r", e)
    return stub_profile(user_id, preset)


def profile_log_activity(user_id: str, activity) -> bool:
    """activity: {"activity","category","duration_min","mood"}。返回是否成功。"""
    if ENGINE_AVAILABLE["profile"] and _p_log is not None:
        try:
            _p_log(user_id, to_dict(activity) if not isinstance(activity, dict) else activity)
            log.info("[bridge] log_activity REAL (%s)", user_id)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("[bridge] real log_activity 异常，回落 stub 存储: %r", e)
    return False


def apply_feedback(profile_dict: dict, feedback_dict: dict) -> dict:
    if ENGINE_AVAILABLE["profile"] and _p_apply is not None:
        try:
            obj = _from_models("Profile", profile_dict)
            fb = to_dict(feedback_dict)
            r = _p_apply(obj, fb)
            d = to_dict(r)
            if isinstance(d, dict) and d.get("weights"):
                log.info("[bridge] apply_feedback REAL")
                return d
        except Exception as e:  # noqa: BLE001
            log.warning("[bridge] real apply_feedback 异常，回落 stub: %r", e)
    return stub_apply_feedback(profile_dict, feedback_dict)
