"""server/app.py — PickOne Web 后端

starlette + uvicorn，监听 0.0.0.0:8888（可用 PICKONE_HOST / PICKONE_PORT 覆盖）。
严格实现 contracts/api.md；任何情况下不返回 500。

运行：bash server/run.sh   （或 .venv/bin/python server/app.py）
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

# --- 确保能 import server.* 与项目根的 engine/ -------------------------------
SERVER_DIR = Path(__file__).resolve().parent
ROOT = SERVER_DIR.parent
for p in (str(ROOT), str(SERVER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import engine_bridge as bridge
import store

VERSION = "0.1.0"
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# --- ASR（契约 contracts/api.md 第 22~27 行） --------------------------------
VENV_PY = "/home/Developer/workspace/.venv/bin/python"
ASR_SCRIPT = ROOT / "asr" / "transcribe.py"
UPLOADS_DIR = DATA_DIR / "uploads"
ASR_MAX_BYTES = 10 * 1024 * 1024          # 10MB
ASR_EXTS = {"webm", "ogg", "mp4", "wav"}  # 后缀白名单


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pickone.app")

# ---------------------------------------------------------------- mxagent 探测

_MXAGENT_CACHE = {"ok": None, "ts": 0.0}


def mxagent_available() -> bool:
    now = time.time()
    if _MXAGENT_CACHE["ok"] is not None and now - _MXAGENT_CACHE["ts"] < 60:
        return _MXAGENT_CACHE["ok"]
    ok = False
    exe = shutil.which("mxagent") or "/home/Developer/workspace/.venv/bin/mxagent"
    try:
        r = subprocess.run([exe, "--help"], capture_output=True, timeout=10)
        ok = (r.returncode == 0)
    except Exception:  # noqa: BLE001
        ok = False
    _MXAGENT_CACHE.update(ok=ok, ts=now)
    return ok


# ---------------------------------------------------------------- 响应辅助

def J(obj, status: int = 200) -> JSONResponse:
    return JSONResponse(obj, status_code=status, headers={"Cache-Control": "no-store"})


def ERR(msg: str, status: int = 400) -> JSONResponse:
    return J({"ok": False, "error": str(msg)}, status)


async def parse_body(request: Request) -> dict:
    """容错解析请求体：空 body / 非 JSON / form 均不抛异常，返回 dict。"""
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        d = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(d, dict):
            return d
        return {"_value": d}
    except Exception:  # noqa: BLE001
        pass
    try:  # 退而求其次当 form 解析
        form = await request.form()
        if form:
            return {k: v for k, v in form.items()}
    except Exception:  # noqa: BLE001
        pass
    return {"_raw": raw[:2000].decode("utf-8", errors="replace")}


def safe_handler(fn):
    """把 handler 包成永不 500 的版本。"""
    async def wrapped(request: Request):
        try:
            return await fn(request)
        except Exception as e:  # noqa: BLE001
            log.error("handler %s 异常: %s\n%s", getattr(fn, "__name__", "?"), e,
                      traceback.format_exc())
            return ERR(f"服务器处理出错：{e}")
    wrapped.__name__ = getattr(fn, "__name__", "handler")
    return wrapped


# ---------------------------------------------------------------- 档案读写（server 侧兜底存储）

def get_profile_dict(user_id: str) -> dict:
    """读档案：engine 侧与 server 侧是同一个文件，但仍做一次合并兜底。

    合并规则：以 **engine 侧（含闭环学习产物）** 为基准，
    server 侧只补充 engine 没有的键（避免旧数据把新学的权重覆盖掉）。
    """
    p = bridge.profile_load(user_id)
    stored = store.load_profile(user_id)
    if p and stored:
        merged = dict(stored)                 # 先放 server 侧
        merged.update(p)                      # engine 侧（更新）覆盖
        for k, v in p.items():                # engine 侧缺失的键用 server 补
            merged.setdefault(k, v)
        merged.setdefault("user_id", user_id)
        return merged
    if p:
        p.setdefault("user_id", user_id)
        return p
    if stored:
        stored.setdefault("user_id", user_id)
        return stored
    return bridge.stub_profile(user_id, "new")


def persist_profile(profile: dict) -> None:
    try:
        store.save_profile(profile)
    except Exception as e:  # noqa: BLE001
        log.warning("server 侧档案落盘失败: %r", e)
    try:
        bridge.profile_save(profile)
    except Exception as e:  # noqa: BLE001
        log.warning("engine 侧档案保存失败: %r", e)


# ---------------------------------------------------------------- 决策后台任务

ADVISE_HARD_TIMEOUT = float(os.environ.get("PICKONE_ADVISE_TIMEOUT", "150"))


def _run_decision(decision_id: str, text: str, user_id: str, use_llm: bool):
    """后台线程：extract + advise，结果写 data/decisions/<id>.json。

    带硬超时看门狗：LLM 是共享资源（本机 llama-server --parallel 1），
    随时可能被别人占满。绝不能让任务长时间停在 running 让用户干等，
    超时即标记 done + degraded 结果（由引擎的启发式兜底保证有内容）。
    """
    wrapper = {"decision_id": decision_id, "status": "running",
               "error": None, "result": None, "user_id": user_id}
    box = {}

    def _work():
        try:
            request = bridge.extract(text, use_llm=use_llm)
            profile = get_profile_dict(user_id)
            decision = bridge.advise(request, profile, use_llm=use_llm)
            decision["decision_id"] = decision_id  # 强制与外层一致，前端轮询才不会错
            box["result"] = decision
        except Exception as e:  # noqa: BLE001
            box["error"] = e
            box["tb"] = traceback.format_exc()

    try:
        store.save_decision(wrapper)
        t = threading.Thread(target=_work, daemon=True, name=f"advise-work-{decision_id}")
        t.start()
        t.join(ADVISE_HARD_TIMEOUT)

        if "result" in box:
            wrapper.update(status="done", result=box["result"], error=None)
            store.save_decision(wrapper)
            log.info("decision %s 完成 (degraded=%s)", decision_id,
                     (box["result"] or {}).get("degraded"))
            return

        if "error" in box:
            log.error("decision %s 失败: %s\n%s", decision_id, box["error"], box.get("tb"))
            wrapper.update(status="failed", error=f"决策生成失败：{box['error']}")
            store.save_decision(wrapper)
            return

        # 超时：引擎线程可能还在跑，但不能让用户一直等
        log.warning("decision %s 超过 %ss 硬超时，转为降级结果", decision_id, ADVISE_HARD_TIMEOUT)
        fallback = None
        try:
            request = bridge.extract(text, use_llm=False)
            profile = get_profile_dict(user_id)
            fallback = bridge.advise(request, profile, use_llm=False)
            fallback["decision_id"] = decision_id
            fallback["degraded"] = True
            fallback.setdefault("notes", []).append(
                f"AI 推理超过 {int(ADVISE_HARD_TIMEOUT)} 秒未返回，已自动切换为离线快速模式")
        except Exception as e:  # noqa: BLE001
            log.error("decision %s 超时后的降级兜底也失败: %r", decision_id, e)
        if fallback is not None:
            wrapper.update(status="done", result=fallback, error=None)
        else:
            wrapper.update(status="failed", error="AI 推理超时，请稍后重试或使用离线快速模式")
        store.save_decision(wrapper)
    except Exception as e:  # noqa: BLE001
        log.error("decision %s 失败: %s\n%s", decision_id, e, traceback.format_exc())
        try:
            wrapper.update(status="failed", error=f"决策生成失败：{e}")
            store.save_decision(wrapper)
        except Exception:  # noqa: BLE001
            log.error("decision %s 失败状态也无法落库", decision_id)


def sweep_stale_decisions():
    """启动清扫：把上次遗留的 pending/running 标记为 failed。

    否则服务重启后前端会永远轮询一个再也不会完成的任务，
    页面卡在"AI 正在推演"——这是当场翻车型缺陷。
    """
    try:
        stale = store.list_decisions(None, 100000, statuses=("pending", "running"))
    except Exception as e:  # noqa: BLE001
        log.warning("清扫遗留任务失败: %r", e)
        return
    for w in stale:
        try:
            store.save_decision({
                "decision_id": w.get("decision_id"),
                "status": "failed",
                "error": "服务重启导致任务中断，请重新提问",
                "result": None,
                "user_id": w.get("user_id"),
            })
        except Exception:  # noqa: BLE001
            continue
    if stale:
        log.info("已清扫 %d 个遗留未完成任务", len(stale))


# ---------------------------------------------------------------- 各端点

PLACEHOLDER_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>PickOne — 前端未就绪</title>
<style>body{font-family:sans-serif;max-width:640px;margin:80px auto;color:#333}
code{background:#f4f4f4;padding:2px 6px;border-radius:4px}</style></head>
<body><h1>PickOne 后端已启动 ✅</h1>
<p>前端 <code>web/index.html</code> 尚未创建（前端同事负责）。</p>
<p>后端 API 可用，见 <code>/api/health</code> 与 <code>contracts/api.md</code>。</p>
</body></html>"""


async def h_index(request: Request):
    idx = WEB_DIR / "index.html"
    try:
        if idx.is_file():
            return HTMLResponse(idx.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        log.warning("读取 index.html 失败: %r", e)
    return HTMLResponse(PLACEHOLDER_HTML, status_code=200)


async def h_health(request: Request):
    return J({"ok": True,
              "mxagent_available": mxagent_available(),
              "version": VERSION,
              "engine": bridge.engine_status()})


async def h_parse(request: Request):
    body = await parse_body(request)
    text = body.get("text") or ""
    if not str(text).strip() and "_raw" in body:
        text = body["_raw"]  # 容错：非 JSON 的纯文本请求体直接当输入

    if not str(text).strip():
        return ERR("缺少 text 字段：请告诉我在纠结什么")
    use_llm = bool(body.get("use_llm", True))
    req = bridge.extract(str(text), use_llm=use_llm)
    return J(req)


async def h_advise(request: Request):
    body = await parse_body(request)
    text = body.get("text") or ""
    if not str(text).strip() and "_raw" in body:
        text = body["_raw"]  # 容错：非 JSON 的纯文本请求体直接当输入
    if not str(text).strip():
        return ERR("缺少 text 字段：请告诉我在纠结什么")
    user_id = str(body.get("user_id") or "demo")
    use_llm = bool(body.get("use_llm", True))
    decision_id = "dec-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    store.save_decision({"decision_id": decision_id, "status": "pending",
                         "error": None, "result": None, "user_id": user_id})
    t = threading.Thread(target=_run_decision, args=(decision_id, str(text),
                                                     user_id, use_llm),
                         daemon=True, name=f"advise-{decision_id}")
    t.start()
    return J({"decision_id": decision_id, "status": "running"})


def _decision_summary(wrapper: dict) -> dict:
    """列表用的 Decision 摘要。"""
    res = wrapper.get("result") or {}
    cards = res.get("cards") or []
    top = cards[0] if cards and isinstance(cards[0], dict) else {}
    return {
        "decision_id": wrapper.get("decision_id"),
        "status": wrapper.get("status"),
        "created_at": res.get("created_at"),
        "raw_text": ((res.get("request") or {}).get("raw_text", "") or "")[:120],
        "top_card_title": top.get("title", ""),
        "confidence": res.get("confidence"),
        "degraded": res.get("degraded"),
        "error": wrapper.get("error"),
    }


async def h_decision(request: Request):
    decision_id = request.path_params.get("decision_id", "")
    w = store.load_decision(decision_id)
    if w is None:
        return ERR(f"未找到决策：{decision_id}", 404)
    return J(w)


async def h_decisions(request: Request):
    qp = request.query_params
    user_id = qp.get("user_id") or None
    try:
        limit = int(qp.get("limit", "20"))
    except ValueError:
        limit = 20
    items = [_decision_summary(w) for w in store.list_decisions(user_id, limit)]
    return J({"items": items})


async def h_feedback(request: Request):
    body = await parse_body(request)
    decision_id = str(body.get("decision_id") or "")
    _w = store.load_decision(decision_id) if decision_id else None
    if not decision_id or _w is None:
        return ERR(f"decision_id 不存在：{decision_id or '(空)'}", 404)
    # user_id 以决策记录为准（前端可能忘传；传错时以决策归属为准，避免改错档案）
    user_id = str(_w.get("user_id") or body.get("user_id") or "demo")
    try:
        sat = int(body.get("satisfaction", 3))
    except (TypeError, ValueError):
        sat = 3
    # 从决策结果里反查"用户实际选项"的类别，供闭环学习（category_prefs）用
    chosen_category = ""
    try:
        _w = store.load_decision(decision_id) or {}
        _res = _w.get("result") or {}
        _chosen = str(body.get("chosen", "") or "")
        for _s in (_res.get("scored") or []):
            _opt = _s.get("option") or {}
            if _chosen and (_chosen == _opt.get("id")
                            or _chosen == _opt.get("label")):
                chosen_category = str(_opt.get("category", "") or "")
                break
    except Exception:  # noqa: BLE001
        chosen_category = ""
    record = {
        "decision_id": decision_id,
        "chosen": body.get("chosen", ""),
        "chosen_category": chosen_category,
        "went": bool(body.get("went", True)),
        "satisfaction": sat,
        "note": str(body.get("note", "")),
        "user_id": user_id,
        "ts": time.time(),
    }
    store.append_feedback(record)
    # 回灌档案（get_profile_dict / persist_profile 内部已按 user 加锁，
    # 不再外层重复加锁，避免长临界区）
    profile = get_profile_dict(record["user_id"])
    profile = bridge.apply_feedback(profile, record)
    profile["user_id"] = record["user_id"]
    persist_profile(profile)
    return J({"ok": True, "weights": profile.get("weights") or {}})


async def h_profile(request: Request):
    user_id = request.query_params.get("user_id") or "demo"
    p = get_profile_dict(user_id)
    p.setdefault("user_id", user_id)
    return J(p)


async def h_profile_reset(request: Request):
    body = await parse_body(request)
    user_id = str(body.get("user_id") or "demo")
    preset = str(body.get("preset") or "new")
    if preset not in ("new", "veteran"):
        return ERR('preset 只能是 "new" 或 "veteran"')
    p = bridge.profile_reset(user_id, preset)
    p["user_id"] = user_id
    persist_profile(p)
    return J(p)



async def h_log_activity(request: Request):
    body = await parse_body(request)
    user_id = str(body.get("user_id") or "demo")
    activity_name = str(body.get("activity") or "").strip()
    if not activity_name:
        return ERR("缺少 activity 字段")
    try:
        dur = int(float(body.get("duration_min", 0)))
    except (TypeError, ValueError):
        dur = 0
    try:
        mood = max(1, min(5, int(float(body.get("mood", 3)))))
    except (TypeError, ValueError):
        mood = 3
    act = {
        "date": time.strftime("%Y-%m-%d"),
        "activity": activity_name,
        "category": str(body.get("category") or "other"),
        "duration_min": dur,
        "mood": mood,
    }
    ok_real = bridge.profile_log_activity(user_id, act)
    if not ok_real:
        # server 侧兜底：写进 data/profiles/<uid>.json 的 history
        # （persist_profile 内部按 user 加锁）
        p = get_profile_dict(user_id)
        hist = list(p.get("history") or [])
        hist.append(act)
        p["history"] = hist[-500:]
        p["user_id"] = user_id
        persist_profile(p)
    return J({"ok": True})


async def h_asr(request: Request):
    """语音转文字。契约：存在 asr/transcribe.py 才 subprocess 调用（超时 30s）；
    脚本不存在/超时/空输出/任何异常 → 200 {"text":"","unavailable":true}，绝不 500。
    脚本不存在/超时/空输出/任何异常 → 200 {"text":"","unavailable":true}，绝不 500。
    上传存 data/uploads/，上限 10MB，后缀白名单 webm|ogg|mp4|wav。
    """
    body = b""
    try:
        body = await request.body()
    except Exception:  # noqa: BLE001
        body = b""
    if len(body) > ASR_MAX_BYTES:
        return J({"text": "", "unavailable": True,
                  "reason": f"音频超过 {ASR_MAX_BYTES // (1024 * 1024)}MB 上限"})
    if not body:
        return J({"text": "", "unavailable": True})

    # 解析 multipart 取文件与 lang；非 multipart 时整个 body 当音频
    suffix = ".webm"
    lang = "zh"
    audio = body
    try:
        ct = request.headers.get("content-type", "")
        if "multipart/form-data" in ct:
            form = await request.form()
            lang = str(form.get("lang") or "zh")
            file_obj = None
            for key in ("file", "audio"):
                if key in form:
                    file_obj = form[key]
                    break
            if file_obj is None or not hasattr(file_obj, "read"):
                return J({"text": "", "unavailable": True,
                          "reason": "multipart 中未找到 file/audio 字段"})
            audio = await file_obj.read()
            fname = str(getattr(file_obj, "filename", "") or "")
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if ext in ASR_EXTS:
                suffix = "." + ext
            else:
                # 从 content-type 猜一下，猜不出默认 webm
                ctype = str(getattr(file_obj, "content_type", "") or "")
                if "wav" in ctype:
                    suffix = ".wav"
                elif "ogg" in ctype:
                    suffix = ".ogg"
                elif "mp4" in ctype or "m4a" in ctype or "aac" in ctype:
                    suffix = ".mp4"
    except Exception:  # noqa: BLE001
        pass  # 非 multipart：整个 body 当音频，后缀默认 webm

    if not audio or len(audio) > ASR_MAX_BYTES:
        return J({"text": "", "unavailable": True})

    if not ASR_SCRIPT.is_file():
        return J({"text": "", "unavailable": True})

    # 落盘临时音频 → subprocess 调 CLI
    tmp_path = None
    try:
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = UPLOADS_DIR / f"asr-{time.strftime('%Y%m%d-%H%M%S')}-" \
                               f"{uuid.uuid4().hex[:6]}{suffix}"
        tmp_path.write_bytes(audio)
        cmd = [VENV_PY, str(ASR_SCRIPT), str(tmp_path)]
        if lang:
            cmd += ["--lang", lang]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        text = (r.stdout or b"").decode("utf-8", errors="replace").strip()
        if text:
            return J({"text": text})
        log.info("[asr] transcribe 无输出（rc=%s）→ unavailable", r.returncode)
        return J({"text": "", "unavailable": True})
    except subprocess.TimeoutExpired:
        log.warning("[asr] transcribe 超时 30s → unavailable")
        return J({"text": "", "unavailable": True})
    except Exception as e:  # noqa: BLE001
        log.warning("[asr] transcribe 异常: %r → unavailable", e)
        return J({"text": "", "unavailable": True})
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass



async def h_demo_cases(request: Request):
    """内置演示样例（契约 contracts/api.md 第 19 行）：
    → {"ok":true,"items":[{"id","title","text"}]}，≥3 条，text 中文非空 >20 字。
    """
    items = [
        {
            "id": "workout_vs_hotpot_vs_movie",
            "title": "健身房 vs 火锅 vs 看电影",
            "text": ("今晚好纠结：方案一去健身房跑步，单程通勤40分钟，年卡折合每次25块钱，"
                     "但最近工作太累；方案二去楼下那家火锅店，步行5分钟，人均80，"
                     "一个人吃又有点尴尬；方案三在家看电影，零通勤零花费，"
                     "但今晚首映怕被剧透。我预算今晚不超过100块，"
                     "而且明天早上7点要起床上班，选哪个好？"),
        },
        {
            "id": "gym_card_vs_treadmill",
            "title": "办健身卡 vs 买跑步机",
            "text": ("周末想把运动捡起来：楼下健身房年卡1800，折合每次25块，"
                     "有团课有泳池，但来回加洗澡要一个多小时，最近工作太累总提不起劲；"
                     "或者买台跑步机放家里2800块，下雨天也能跑，"
                     "就是怕像去年那台动感单车一样骑了三次就挂衣服。"
                     "我每周想运动3次，这周一次还没去，选哪个？"),
        },
        {
            "id": "delivery_vs_cook",
            "title": "点外卖 vs 回家做",
            "text": ("晚饭怎么解决：加班到八点，点外卖30分钟送到、人均35，"
                     "但重油重盐这个月已经胖了四斤；还是去楼下超市买点菜回家做，"
                     "健康省钱、人均15块，但买菜做饭洗碗一套下来要一个半小时，"
                     "洗完碗都快十点了。明天早上7点要起床上班，选哪个？"),
        },
    ]
    return J({"ok": True, "items": items})




# ---------------------------------------------------------------- SSE（可选）

async def h_stream(request: Request):
    """轻量 SSE：轮询落库文件推送状态变化；120s 后自动结束。前端亦可只用轮询。"""
    from starlette.responses import StreamingResponse
    decision_id = request.path_params.get("decision_id", "")

    async def gen():
        last = ""
        t0 = time.time()
        while time.time() - t0 < 120:
            w = store.load_decision(decision_id)
            cur = json.dumps({"status": (w or {}).get("status", "unknown"),
                              "error": (w or {}).get("error")}, ensure_ascii=False)
            if cur != last:
                yield f"event: status\ndata: {cur}\n\n"
                last = cur
            if (w or {}).get("status") in ("done", "failed"):
                w2 = store.load_decision(decision_id) or {}
                yield f"event: result\ndata: {json.dumps(w2, ensure_ascii=False, default=str)}\n\n"
                return
            await asyncio.sleep(1.0)
        yield "event: timeout\ndata: {}\n\n"

    try:
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
    except Exception as e:  # noqa: BLE001
        log.warning("SSE 启动失败: %r", e)
        return ERR("SSE 不可用")



import asyncio  # noqa: E402  (h_stream 用到，放此处避免顶部噪音)

# ---------------------------------------------------------------- 应用组装

routes = [
    Route("/", h_index),
    Route("/api/health", safe_handler(h_health)),
    Route("/api/parse", safe_handler(h_parse), methods=["POST"]),
    Route("/api/advise", safe_handler(h_advise), methods=["POST"]),
    Route("/api/decision/{decision_id}", safe_handler(h_decision)),
    Route("/api/decisions", safe_handler(h_decisions)),
    Route("/api/feedback", safe_handler(h_feedback), methods=["POST"]),
    Route("/api/profile", safe_handler(h_profile)),
    Route("/api/profile/reset", safe_handler(h_profile_reset), methods=["POST"]),
    Route("/api/log_activity", safe_handler(h_log_activity), methods=["POST"]),
    Route("/api/asr", safe_handler(h_asr), methods=["POST"]),
    Route("/api/demo/cases", safe_handler(h_demo_cases)),
    Route("/api/stream/{decision_id}", safe_handler(h_stream)),
]

# 静态资源：web/ 存在才挂载（挂载在 "/" 的最后，作为兜底）
if WEB_DIR.is_dir():
    routes.append(Mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static"))

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    ),
]

async def _not_found(request, exc):
    """404：契约要求返回 JSON，而不是框架默认的 'Not Found' 纯文本。"""
    return JSONResponse({"ok": False, "error": "接口不存在"}, status_code=404)


async def _method_not_allowed(request, exc):
    """405：同样返回契约 JSON。"""
    return JSONResponse({"ok": False, "error": "请求方法不被支持"}, status_code=405)


async def _server_error(request, exc):
    """兜底：任何未捕获异常也不要把 Python traceback 暴露给前端。"""
    log.exception("未处理异常: %r", exc)
    return JSONResponse({"ok": False, "error": "服务内部错误，请稍后再试"}, status_code=500)


app = Starlette(
    routes=routes,
    middleware=middleware,
    exception_handlers={
        404: _not_found,
        405: _method_not_allowed,
        500: _server_error,
    },
)


# ---------------------------------------------------------------- 启动入口

def _resolve_bind():
    # 默认 0.0.0.0:8888（监听所有网卡，内网/公网映射都能到达）
    host = os.environ.get("PICKONE_HOST", "0.0.0.0")
    port = int(os.environ.get("PICKONE_PORT", "8888"))
    return host, port


def main():
    import uvicorn
    host, port = _resolve_bind()
    log.info("[pickone] 启动 %s:%s (web=%s, engine=%s)", host, port, WEB_DIR,
             bridge.engine_status())
    sweep_stale_decisions()  # 把上次遗留的 pending/running 标记为 failed
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except OSError as e:
        if host != "0.0.0.0":
            log.warning("[pickone] 绑定 %s:%s 失败(%s)，回退 0.0.0.0:%s", host, port, e, port)
            uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
        else:
            raise


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
