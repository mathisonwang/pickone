"""测试公共工具（qa_demo 组）

- 契约路径常量
- 服务可用性探测
- 简单的运行期统计收集（供 run_all.sh 汇总）

硬性环境约定（来自 docs/ARCHITECTURE.md 第 1 节）：
    解释器必须是 /home/Developer/workspace/.venv/bin/python (3.13)
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = "/home/Developer/workspace/.venv/bin/python"
BASE_URL = os.environ.get("PICKONE_BASE", "http://127.0.0.1:8888")

# 契约 api.md 中的端点表（smoke_web.py 用它比对前端 fetch 路径）
CONTRACT_ENDPOINTS = [
    "/",
    "/static/*",
    "/api/health",
    "/api/parse",
    "/api/advise",
    "/api/decision/{decision_id}",
    "/api/decisions",
    "/api/feedback",
    "/api/profile",
    "/api/profile/reset",
    "/api/log_activity",
    "/api/asr",
    "/api/stream/{decision_id}",
    # 契约外扩展（父 agent 已确认必须存在，并由其补写进 contracts/api.md）
    "/api/demo/cases",
]

# 打分维度（ARCHITECTURE 第 4 节）
DIMENSIONS = ["rhythm", "social", "commute", "cost", "pleasure", "health", "future"]
DEFAULT_WEIGHTS = {
    "rhythm": 0.22, "social": 0.18, "commute": 0.16, "cost": 0.10,
    "pleasure": 0.16, "health": 0.12, "future": 0.06,
}
EPSILON = 1.2


def check_interpreter() -> None:
    """非 venv 解释器直接报错退出（系统 python3=3.12 import site-packages 会炸）。"""
    if os.path.realpath(sys.executable) != os.path.realpath(VENV_PY):
        print(f"[FATAL] 必须用 venv 解释器运行：{VENV_PY}\n"
              f"        当前是 {sys.executable}", file=sys.stderr)
        sys.exit(2)


def server_up(timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(BASE_URL + "/api/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def http(method: str, path: str, body=None, timeout: float = 30.0,
         headers: dict | None = None, raw: bool = False):
    """返回 (status, headers, body_bytes_or_obj, err_str)。永不抛异常。

    raw=True 时 body 原样返回 bytes（给 HTML 断言用）。
    """
    url = BASE_URL + path
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            status = resp.status
            rh = dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:
            payload = b""
        status = e.code
        rh = dict(getattr(e, "headers", {}) or {})
    except Exception as e:  # URLError / timeout / refused
        return None, {}, None, f"{type(e).__name__}: {e}"
    if raw:
        return status, rh, payload, ""
    try:
        obj = json.loads(payload.decode("utf-8")) if payload else None
    except Exception as e:
        return status, rh, None, f"非法 JSON: {e}; body[:200]={payload[:200]!r}"
    return status, rh, obj, ""


def jsonschema_available() -> bool:
    try:
        import jsonschema  # noqa: F401
        return True
    except Exception:
        return False


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
