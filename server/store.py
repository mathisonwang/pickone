"""server/store.py — JSON 文件持久化 + 文件锁（线程安全 + 进程安全）

数据布局（契约 contracts/api.md 第 28 行）：
    data/decisions/<decision_id>.json   外层包装:
        {"decision_id","status":"pending|running|done|failed","error":null|str,"result":Decision|null}
    data/profiles/<user_id>.json        Profile dict
    data/feedback.jsonl                 每行一条 feedback

设计要点：
- 纯标准库（fcntl + threading），任何解释器可跑。
- 写入采用 临时文件 + os.replace 原子替换，避免读到半截 JSON。
- 同一 user 的 profile 写入加文件锁（fcntl.flock），跨进程也安全。
- 所有读函数不抛异常：文件缺失/损坏 → 返回 None（或默认值）。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# ---------------------------------------------------------------- 路径常量

# server/store.py -> 项目根 choice/
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DECISIONS_DIR = DATA_DIR / "decisions"
PROFILES_DIR = DATA_DIR / "profiles"
FEEDBACK_FILE = DATA_DIR / "feedback.jsonl"
LOCK_DIR = DATA_DIR / "locks"

for _d in (DECISIONS_DIR, PROFILES_DIR, LOCK_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 进程内互斥（fcntl 只对进程间有效，同进程多线程需 threading 锁兜底）
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")


def _safe(name: str) -> str:
    """把外部传入的 id 变成安全文件名片段；非法返回空串。"""
    if not isinstance(name, str):
        return ""
    name = name.strip()
    return name if _SAFE_ID.match(name) else ""


@contextmanager
def file_lock(key: str, timeout: float = 10.0):
    """对逻辑 key 加锁：threading.RLock（进程内可重入） + flock（进程间）。

    必须用 RLock：存在嵌套加锁路径（handler 持 profile_x 锁 → persist_profile
    → save_profile → file_lock("profile_x")），普通 Lock 不可重入会死锁。
    flock 对同进程不同 fd 也天然可重入。
    """
    key = _safe(key) or "default"
    with _THREAD_LOCKS_GUARD:
        tlock = _THREAD_LOCKS.get(key)
        if tlock is None:
            tlock = threading.RLock()
            _THREAD_LOCKS[key] = tlock

    lock_path = LOCK_DIR / f"{key}.lock"
    tlock.acquire()
    fh = None
    try:
        fh = open(lock_path, "a+")
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(f"获取文件锁超时: {key}")
                time.sleep(0.05)
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
        tlock.release()


def _atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, OSError):
        return None


# ---------------------------------------------------------------- decisions

def decision_path(decision_id: str) -> Path:
    return DECISIONS_DIR / f"{_safe(decision_id)}.json"


def save_decision(wrapper: dict) -> None:
    """wrapper = {"decision_id","status","error","result"}"""
    did = _safe(str(wrapper.get("decision_id", "")))
    if not did:
        raise ValueError("非法 decision_id")
    with file_lock(f"decision_{did}"):
        _atomic_write_json(decision_path(did), wrapper)


def load_decision(decision_id: str):
    did = _safe(decision_id)
    if not did:
        return None
    with file_lock(f"decision_{did}"):
        return _read_json(decision_path(did))


def list_decisions(user_id: str | None = None, limit: int = 20,
                   statuses: tuple[str, ...] | None = None) -> list[dict]:
    """返回决策外层包装列表，按修改时间降序（新的在前）。

    statuses: 只保留这些 status（用于启动清扫遗留 pending/running）。
    """
    limit = max(1, min(int(limit or 20), 100000))
    items: list[dict] = []
    try:
        files = sorted(DECISIONS_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return items
    for p in files:
        if len(items) >= limit * 4:  # 过滤前多读一些
            break
        d = _read_json(p)
        if not isinstance(d, dict):
            continue
        if statuses and d.get("status") not in statuses:
            continue
        if user_id:
            res = d.get("result") or {}
            req = (res.get("request") or {}) if isinstance(res, dict) else {}
            owner = d.get("user_id") or req.get("user_id")
            if owner and owner != user_id:
                continue
        items.append(d)
        if len(items) >= limit:
            break
    return items


# ---------------------------------------------------------------- profiles

def profile_path(user_id: str) -> Path:
    return PROFILES_DIR / f"{_safe(user_id)}.json"


def save_profile(profile: dict) -> None:
    uid = _safe(str(profile.get("user_id", "")))
    if not uid:
        raise ValueError("非法 user_id")
    with file_lock(f"profile_{uid}"):
        profile["updated_at"] = time.time()
        _atomic_write_json(profile_path(uid), profile)


def load_profile(user_id: str):
    uid = _safe(user_id)
    if not uid:
        return None
    with file_lock(f"profile_{uid}"):
        return _read_json(profile_path(uid))


# ---------------------------------------------------------------- feedback

def append_feedback(record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False, default=str)
    with file_lock("feedback_jsonl"):
        with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


def read_feedback(limit: int = 100) -> list[dict]:
    out: list[dict] = []
    if not FEEDBACK_FILE.exists():
        return out
    with file_lock("feedback_jsonl"):
        try:
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()[-max(1, min(limit, 10000)):]
        except OSError:
            return out
    for ln in lines:
        try:
            d = json.loads(ln)
            if isinstance(d, dict):
                out.append(d)
        except json.JSONDecodeError:
            continue
    return out
