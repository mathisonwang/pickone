"""engine/profile.py — 用户档案读写 + 满意度回灌（后悔闭环）。

契约（contracts/data_model.md + contracts/api.md）：
    load_profile(user_id) -> Profile
    save_profile(profile) -> Profile
    reset_profile(user_id, preset="new"|"veteran") -> Profile
    log_activity(user_id, activity) -> None
    apply_feedback(profile, feedback) -> Profile

回灌规则（ARCHITECTURE 第 5 节）：学习率 0.05，权重 clamp 到 [0.05, 0.35] 后归一化。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time

from .models import (
    DEFAULT_WEIGHTS,
    DIMENSIONS,
    LEARNING_RATE,
    WEIGHT_MAX,
    WEIGHT_MIN,
    Profile,
    clamp,
)

# ---------------------------------------------------------------- 路径

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = os.path.join(ROOT, "data", "profiles")
LOCK_DIR = os.path.join(ROOT, "data", "locks")

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lk = _LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[key] = lk
        return lk


def _safe_user_id(user_id) -> str:
    """防目录穿越：只保留安全字符。"""
    s = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(user_id or "demo")).strip("._")
    return (s or "demo")[:64]


def profile_path(user_id: str) -> str:
    return os.path.join(PROFILE_DIR, _safe_user_id(user_id) + ".json")


# ---------------------------------------------------------------- 预设

def new_profile(user_id: str = "demo", preset: str = "new") -> Profile:
    """构造档案。preset: `new`（几乎无历史）/ `veteran`（3 周老用户）。"""
    uid = str(user_id or "demo")
    if preset == "veteran":
        now = time.time()
        # ⚠️ 演示关键：veteran 必须是"已经 4 天没运动"的状态，
        # 否则 rhythm（节律缺口）维度拉不开，"老用户优选运动"的核心演示就失效。
        # 最近一次运动在 4 天前，再往前是 7/11/17/21 天前（约每周 1~2 次，低于目标 3 次）。
        history = [
            {"date": time.strftime("%Y-%m-%d", time.localtime(now - 86400 * d)),
             "activity": a, "category": c, "duration_min": dur, "mood": m}
            for d, a, c, dur, m in [
                (2, "在家休息", "rest", 240, 3),
                (4, "去健身房跑步", "workout", 60, 5),
                (7, "去楼下那家火锅店", "food", 90, 5),
                (9, "在家看电影", "rest", 150, 4),
                (11, "去健身房跑步", "workout", 60, 4),
                (14, "在家休息", "rest", 240, 3),
                (17, "去楼下那家火锅店", "food", 90, 5),
                (19, "在家看电影", "rest", 150, 4),
                (21, "去健身房跑步", "workout", 60, 5),
            ]
        ]
        return Profile(
            user_id=uid,
            nickname="老用户",
            goals=[{"key": "workout_per_week", "target": 3, "unit": "次/周"}],
            history=history,
            weights=dict(DEFAULT_WEIGHTS),
            facts=[
                "楼下火锅店的老板娘很热情",
                "不吃辣",
                "预算敏感，单晚超过 100 会犹豫",
                "偏好步行 15 分钟能到的地方",
                "每周想运动 3 次",
            ],
            updated_at=now,
        )
    return Profile(
        user_id=uid,
        nickname="新用户",
        goals=[],
        history=[],
        weights=dict(DEFAULT_WEIGHTS),
        facts=[],
        updated_at=time.time(),
    )


# 兼容 tests/test_engine.py 会尝试的构造器名
def make_profile(user_id: str = "demo", preset: str = "new") -> Profile:
    return new_profile(user_id, preset)


def create_profile(user_id: str = "demo", preset: str = "new") -> Profile:
    return new_profile(user_id, preset)


def default_profile(user_id: str = "demo", preset: str = "new") -> Profile:
    return new_profile(user_id, preset)


def preset_profile(user_id: str = "demo", preset: str = "new") -> Profile:
    return new_profile(user_id, preset)


def veteran_profile(user_id: str = "demo", preset: str = "veteran") -> Profile:
    """老用户档案。容忍被以 veteran_profile(uid, preset="veteran") 形式调用。"""
    return new_profile(user_id, "veteran" if preset in ("veteran", "") else preset)


# ---------------------------------------------------------------- 读写

def load_profile(user_id: str) -> Profile:
    """读档案；不存在 / 读失败 → 返回 new 预设（不抛异常，不落盘）。"""
    path = profile_path(user_id)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            p = Profile.from_dict(d)
            if not p.user_id:
                p.user_id = str(user_id or "demo")
            return p
    except Exception:
        pass
    return new_profile(user_id, "new")


def save_profile(profile) -> Profile:
    """落盘（原子写 + 文件锁）。失败不抛异常，原样返回。"""
    if profile is None:
        return profile
    if not hasattr(profile, "to_dict"):
        return profile
    uid = getattr(profile, "user_id", "demo") or "demo"
    try:
        profile.updated_at = time.time()
    except Exception:
        pass
    path = profile_path(uid)
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        with _lock_for("profile_" + _safe_user_id(uid)):
            fd, tmp = tempfile.mkstemp(dir=PROFILE_DIR, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception:
        pass
    return profile


def reset_profile(user_id: str, preset: str = "new") -> Profile:
    """重置为指定预设并落盘。preset 非法 → new。"""
    if preset not in ("new", "veteran"):
        preset = "new"
    p = new_profile(user_id, preset)
    return save_profile(p)


def log_activity(user_id: str, activity) -> Profile:
    """记录一次日常活动（供 rhythm 维度使用）。失败不抛异常。"""
    uid = str(user_id or "demo")
    with _lock_for("profile_" + _safe_user_id(uid)):
        p = load_profile(uid)
        if isinstance(activity, dict):
            rec = {
                "date": str(activity.get("date") or time.strftime("%Y-%m-%d")),
                "activity": str(activity.get("activity") or ""),
                "category": str(activity.get("category") or "other"),
                "duration_min": int(_num(activity.get("duration_min", -1), -1)),
                "mood": int(clamp(activity.get("mood", 3), 1, 5)),
            }
        else:
            rec = {"date": time.strftime("%Y-%m-%d"), "activity": str(activity or ""),
                   "category": "other", "duration_min": -1, "mood": 3}
        if not rec["activity"]:
            return p
        p.history.append(rec)
        # 只保留最近 200 条，避免无限增长
        if len(p.history) > 200:
            p.history = p.history[-200:]
        return save_profile(p)


def _num(v, default=0.0):
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return float(v)
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 满意度回灌

def _clamp_normalize(weights: dict) -> dict:
    """clamp 到 [0.05, 0.35] 后归一化（和=1）。

    注意顺序：先 clamp 再归一化会破坏下界（归一化后可能 < 0.05），
    所以采用 **迭代** 方式：clamp → 归一化 → 再 clamp → 再归一化，
    直到所有权重都落在 [0.05, 0.35] 且和≈1（有限次迭代必收敛）。
    """
    out = {}
    for k in DIMENSIONS:
        v = _num(weights.get(k, DEFAULT_WEIGHTS[k]), DEFAULT_WEIGHTS[k])
        out[k] = min(WEIGHT_MAX, max(WEIGHT_MIN, v))

    for _ in range(64):
        s = sum(out.values())
        if s <= 0:
            return dict(DEFAULT_WEIGHTS)
        out = {k: v / s for k, v in out.items()}
        # 归一化后再 clamp
        clamped = {k: min(WEIGHT_MAX, max(WEIGHT_MIN, v)) for k, v in out.items()}
        if all(abs(clamped[k] - out[k]) < 1e-12 for k in out):
            out = clamped
            break
        out = clamped

    # 最后微调让和精确为 1（把残差加到最大权重上，保证不越界）
    s = sum(out.values())
    if s > 0 and abs(s - 1.0) > 1e-12:
        kmax = max(out, key=lambda k: out[k])
        out[kmax] = min(WEIGHT_MAX, max(WEIGHT_MIN, out[kmax] + (1.0 - s)))
    # 四舍五入后再校准一次，保证 round 之后和仍为 1
    out = {k: round(v, 4) for k, v in out.items()}
    drift = round(1.0 - sum(out.values()), 4)
    if abs(drift) > 1e-9:
        kmax = max(out, key=lambda k: out[k])
        out[kmax] = round(min(WEIGHT_MAX, max(WEIGHT_MIN, out[kmax] + drift)), 4)
    return out


def apply_feedback(profile, feedback) -> Profile:
    """按"实际选择 vs 推荐"与满意度微调维度权重。

    规则（ARCHITECTURE 第 5 节）：
    - 学习率 0.05；权重 clamp [0.05, 0.35] 后归一化。
    - 满意度高 → 被选选项**强**的维度权重上升（chosen_dims 优于 recommended_dims 的部分）。
    - 没去（went=false）→ 更看重成本/通勤（现实阻力）。
    - 同时把"曾选择 X，满意度 N/5"写进 facts（前端展示"档案学到的偏好"）。
    """
    if profile is None:
        profile = new_profile(str((feedback or {}).get("user_id", "demo")), "new")
    if not hasattr(profile, "weights"):
        return profile

    fb = feedback if isinstance(feedback, dict) else {}
    try:
        sat = min(5.0, max(1.0, _num(fb.get("satisfaction", 3), 3.0)))
    except Exception:
        sat = 3.0
    went = bool(fb.get("went", True))
    chosen = str(fb.get("chosen", "") or "")
    recommended = str(fb.get("recommended", "") or "")

    w = dict(getattr(profile, "weights", None) or DEFAULT_WEIGHTS)
    for k in DIMENSIONS:
        w.setdefault(k, DEFAULT_WEIGHTS[k])

    lr = LEARNING_RATE
    # 满意度偏离中性的程度：+1（很满意）~ -1（很后悔）
    mood = (sat - 3.0) / 2.0

    chosen_dims = fb.get("chosen_dims") if isinstance(fb.get("chosen_dims"), dict) else {}
    rec_dims = fb.get("recommended_dims") if isinstance(fb.get("recommended_dims"), dict) else {}

    if went and chosen_dims:
        # 被选项相对推荐项的优势维度 → 权重上升（满意度越高升得越多）
        for k in DIMENSIONS:
            cd = _num(chosen_dims.get(k), None) if k in chosen_dims else None
            rd = _num(rec_dims.get(k), None) if k in rec_dims else None
            if cd is None:
                continue
            base = rd if rd is not None else 5.0
            advantage = clamp(cd - base, -10.0, 10.0) / 10.0   # -1..1
            if advantage == 0:
                continue
            w[k] = w[k] + lr * mood * advantage * 2.0
    elif went:
        # 没有维度明细：满意 → 愉悦/社交权重上升；不满意 → 反向
        for k, scale in (("pleasure", 1.0), ("social", 0.6), ("rhythm", 0.4)):
            w[k] = w[k] + lr * mood * scale
        # 用户明确选了某个选项 → 也把"该选项的类别偏好"记下来，
        # 让后续同类选项的 pleasure/social 有据可依（闭环可见的关键）。
        cat = str(fb.get("chosen_category", "") or "")
        if cat:
            prefs = list(getattr(profile, "category_prefs", None) or [])
            entry = {"category": cat, "mood": round(mood, 2), "ts": time.time()}
            prefs.append(entry)
            profile.category_prefs = prefs[-50:]
        # 放鸽子 → 现实阻力（通勤/花费）权重上升
        for k, scale in (("commute", 0.8), ("cost", 0.5)):
            w[k] = w[k] + lr * 0.8 * scale

    # 推荐了但用户选了别的 → 轻微下调"推荐依据"的权重（自我修正）
    if went and chosen and recommended and chosen != recommended and rec_dims:
        for k in DIMENSIONS:
            rd = _num(rec_dims.get(k), None) if k in rec_dims else None
            if rd is None:
                continue
            w[k] = w[k] - lr * 0.3 * (rd / 10.0)

    profile.weights = _clamp_normalize(w)

    # ---- facts：让"档案学到的偏好"可见 ----
    if chosen:
        note = f"曾选择「{chosen}」，满意度 {int(sat)}/5"
        if not went:
            note += "（最终没去）"
        facts = list(getattr(profile, "facts", None) or [])
        if note not in facts:
            facts.append(note)
        profile.facts = facts[-50:]

    try:
        profile.updated_at = time.time()
    except Exception:
        pass
    return profile


__all__ = [
    "load_profile", "save_profile", "reset_profile", "log_activity",
    "apply_feedback", "new_profile", "make_profile", "create_profile",
    "default_profile", "preset_profile", "veteran_profile", "profile_path",
]
