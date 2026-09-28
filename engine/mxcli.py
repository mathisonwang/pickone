"""engine/mxcli.py — mxagent CLI 子进程封装（唯一允许调 LLM 的地方）。

硬性约定（docs/ARCHITECTURE.md 第 1/6 节 + contracts/api.md 第 29~32 行）：
- 统一走 CLI 子进程，避免 event loop 冲突。
- 可执行文件由环境变量 **MXAGENT_BIN** 决定（默认 `mxagent`）。
  `MXAGENT_BIN=/nonexistent/mxagent` → 必须降级，绝不抛异常。
- 单次超时 120s，**失败即降级，不重试**。
- 模型 qwen3.8-flash-next（本机唯一可用模型）。
- 输出解析：逐行 rstrip + 剥 ```json 围栏 + 正则抓第一个平衡 {...}（见 schema.py）。
- `<snapshot>.d/agents/*.jsonl` 是层级推演的真实数据来源。
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import time
from typing import Any

# ---------------------------------------------------------------- 常量

DEFAULT_MXAGENT_BIN = "mxagent"
DEFAULT_MODEL = "qwen3.8-flash-next"
DEFAULT_TIMEOUT = 120.0          # 秒；契约硬性要求
SNAPSHOT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "snapshots")


def mxagent_bin() -> str:
    """当前生效的 mxagent 可执行文件（每次调用都读，便于测试时改环境变量）。"""
    return (os.environ.get("MXAGENT_BIN") or DEFAULT_MXAGENT_BIN).strip() \
        or DEFAULT_MXAGENT_BIN


def model_name() -> str:
    return (os.environ.get("MXAGENT_MODEL") or DEFAULT_MODEL).strip() \
        or DEFAULT_MODEL


def _resolve(exe: str) -> str | None:
    """把可执行文件解析成绝对路径；不存在返回 None。"""
    if not exe:
        return None
    if os.path.sep in exe or exe.startswith("."):
        p = os.path.abspath(os.path.expanduser(exe))
        return p if (os.path.isfile(p) and os.access(p, os.X_OK)) else None
    return shutil.which(exe)


def available() -> bool:
    """mxagent 是否可用（探测 --version，快速失败）。"""
    exe = _resolve(mxagent_bin())
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------- 调用

class MxResult:
    """一次 mxagent 调用的结果。ok=False 时调用方必须走降级。"""

    __slots__ = ("ok", "text", "error", "returncode", "elapsed", "snapshot")

    def __init__(self, ok: bool, text: str = "", error: str = "",
                 returncode: int | None = None, elapsed: float = 0.0,
                 snapshot: str = ""):
        self.ok = ok
        self.text = text
        self.error = error
        self.returncode = returncode
        self.elapsed = elapsed
        self.snapshot = snapshot

    def __repr__(self):
        return (f"MxResult(ok={self.ok}, rc={self.returncode}, "
                f"elapsed={self.elapsed:.1f}s, err={self.error[:60]!r})")


def run(task: str, level: int = 0, snapshot: str = "", timeout: float | None = None,
        extra_args: list[str] | None = None, censor: str = "") -> MxResult:
    """跑一次 mxagent。任何失败都返回 ok=False，**绝不抛异常**。

    :param task: 任务提示词（里面应已包含"只输出 JSON"的要求）
    :param level: 0（直接执行）或 2（层级委派，产生子 agent）
    :param snapshot: snapshot 路径；给了就会写 `<snapshot>.d/agents/*.jsonl`
    :param timeout: 超时秒数，默认 120s
    :param extra_args: 额外 CLI 参数
    :param censor: censor 名或文件路径（--censor）
    """
    timeout = float(timeout or DEFAULT_TIMEOUT)
    t0 = time.time()
    exe = _resolve(mxagent_bin())
    if exe is None:
        return MxResult(False, error=f"mxagent 不可用：{mxagent_bin()}（找不到可执行文件）",
                        elapsed=time.time() - t0)

    # ⚠️ 不要无条件传 --model：本机 endpoint 的 alias（qwen3.8-flash-next）
    # 能被 --list models 列出，但传给 --model 会报 "Model not found" 直接退出 1。
    # 只有 MXAGENT_MODEL 显式指定时才传，否则用 mxagent 自己的默认模型。
    cmd = [exe, "--task", str(task)]
    _model = (os.environ.get("MXAGENT_MODEL") or "").strip()
    if _model:
        cmd += ["--model", _model]
    cmd += ["--style", "clean", "--not_allow_ask", "--level", str(int(level))]
    if snapshot:
        cmd += ["--snapshot", str(snapshot)]
    if censor:
        cmd += ["--censor", str(censor)]
    if extra_args:
        cmd += [str(a) for a in extra_args]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return MxResult(False, error=f"mxagent 超时（>{timeout:.0f}s）",
                        elapsed=time.time() - t0, snapshot=snapshot)
    except Exception as e:  # noqa: BLE001 — FileNotFoundError / PermissionError / OSError
        return MxResult(False, error=f"mxagent 调用异常：{type(e).__name__}: {e}",
                        elapsed=time.time() - t0, snapshot=snapshot)

    elapsed = time.time() - t0
    out = (r.stdout or b"").decode("utf-8", errors="replace")
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        return MxResult(False, text=out,
                        error=f"mxagent 退出码 {r.returncode}: {err[:200]}",
                        returncode=r.returncode, elapsed=elapsed, snapshot=snapshot)
    if not out.strip():
        return MxResult(False, error="mxagent 输出为空", returncode=r.returncode,
                        elapsed=elapsed, snapshot=snapshot)
    return MxResult(True, text=out, returncode=r.returncode, elapsed=elapsed,
                    snapshot=snapshot)


def run_json(task: str, schema_name: str, level: int = 0, snapshot: str = "",
             timeout: float | None = None) -> tuple[Any | None, MxResult]:
    """跑 mxagent 并容错解析 JSON。返回 (obj_or_None, MxResult)。

    解析失败 / schema 校验失败 → obj=None（调用方降级）。
    """
    from . import schema as _schema

    res = run(task, level=level, snapshot=snapshot, timeout=timeout)
    if not res.ok:
        return None, res
    obj = _schema.parse_json_loose(res.text)
    if obj is None:
        return None, MxResult(False, text=res.text,
                              error="LLM 输出无法解析为 JSON",
                              returncode=res.returncode, elapsed=res.elapsed,
                              snapshot=snapshot)
    ok, errs, obj = _schema.validate(obj, schema_name)
    if not ok:
        return None, MxResult(False, text=res.text,
                              error="LLM 输出不符合 schema: " + "; ".join(errs[:3]),
                              returncode=res.returncode, elapsed=res.elapsed,
                              snapshot=snapshot)
    return obj, res


# ---------------------------------------------------------------- snapshot 层级

def _read_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict):
                    out.append(d)
    except Exception:
        return []
    return out


def _summarize_content(content: str, limit: int = 160) -> str:
    """把 agent 的一条 content 压成一句话摘要（给人看，不要原始 JSON）。"""
    if not content:
        return ""
    text = str(content).replace("\r", " ").replace("\n", " ").strip()
    text = re_sub(r"\s+", " ", text)
    if not text:
        return ""

    # 1) 如果是 JSON（LLM 按"只输出 JSON"要求回答），抽出人类可读的字段
    if text.lstrip().startswith(("{", "[")):
        try:
            from . import schema as _schema

            obj = _schema.parse_json_loose(text)
            if obj is not None:
                text = _humanize_json(obj) or text
        except Exception:
            pass

    # 2) 去掉 markdown 代码围栏残留
    text = re_sub(r"```(?:json|JSON)?", " ", text)
    text = re_sub(r"\s+", " ", text).strip(" `")

    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _humanize_json(obj, depth: int = 0) -> str:
    """把 LLM 的 JSON 输出压成一句中文摘要（取 desc/summary/label 等可读字段）。"""
    if depth > 3:
        return ""
    parts: list[str] = []
    if isinstance(obj, dict):
        for key in ("summary", "desc", "description", "risk", "title", "label",
                    "conclusion", "answer", "text"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        for key in ("options", "effects", "points", "items", "children"):
            v = obj.get(key)
            if isinstance(v, list):
                for item in v[:3]:
                    sub = _humanize_json(item, depth + 1)
                    if sub:
                        parts.append(sub)
        if not parts:
            for v in obj.values():
                if isinstance(v, str) and 4 <= len(v.strip()) <= 120:
                    parts.append(v.strip())
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    parts.append(str(v))
    elif isinstance(obj, list):
        for item in obj[:3]:
            sub = _humanize_json(item, depth + 1)
            if sub:
                parts.append(sub)
    elif isinstance(obj, str) and obj.strip():
        parts.append(obj.strip())
    seen = set()
    uniq = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return "；".join(uniq[:4])


def re_sub(pattern: str, repl: str, text: str) -> str:
    import re
    return re.sub(pattern, repl, text)


def agent_files(snapshot: str) -> list[str]:
    """`<snapshot>.d/agents/*.jsonl` 全部文件（不存在返回空列表）。"""
    if not snapshot:
        return []
    d = snapshot if snapshot.endswith(".d") else snapshot + ".d"
    pattern = os.path.join(d, "agents", "*.jsonl")
    try:
        return sorted(glob.glob(pattern))
    except Exception:
        return []


def agent_name(path: str) -> str:
    """`Agent.option_analysis.jsonl` → `option_analysis`。"""
    base = os.path.basename(path)
    if base.endswith(".jsonl"):
        base = base[:-len(".jsonl")]
    if base.startswith("Agent."):
        base = base[len("Agent."):]
    return base or "root"


def snapshot_tree(snapshot: str, max_children: int = 12) -> dict:
    """把 `<snapshot>.d/agents/*.jsonl` 整理成 reasoning_tree 的 children 列表。

    返回 {"children": [{"agent","summary","children":[...]}]}；没有文件返回空。
    summary 取该 agent 最后一条有内容的 assistant/user 消息摘要。
    """
    children: list[dict] = []
    for path in agent_files(snapshot):
        name = agent_name(path)
        rows = _read_jsonl(path)
        summary = ""
        # 优先最后一条 assistant 的长内容，其次最后一条 user
        for row in reversed(rows):
            role = str(row.get("role", ""))
            content = str(row.get("content") or "")
            if role == "assistant" and len(content) > 20:
                summary = _summarize_content(content)
                break
        if not summary:
            for row in reversed(rows):
                content = str(row.get("content") or "")
                if len(content) > 10:
                    summary = _summarize_content(content)
                    break
        if not summary:
            summary = f"{name} 已完成推演"
        children.append({"agent": name, "summary": summary, "children": []})
        if len(children) >= max_children:
            break
    return {"children": children}


def snapshot_dir_for(decision_id: str) -> str:
    """给一次决策分配独立 snapshot 目录（ARCHITECTURE 第 6 节）。"""
    safe = "".join(c for c in str(decision_id or "dec")
                   if c.isalnum() or c in "-_")[:40] or "dec"
    d = os.path.join(SNAPSHOT_ROOT, safe)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "snap")


__all__ = [
    "MxResult", "run", "run_json", "available", "mxagent_bin", "model_name",
    "snapshot_tree", "snapshot_dir_for", "agent_files", "agent_name",
    "DEFAULT_TIMEOUT", "DEFAULT_MODEL",
]
