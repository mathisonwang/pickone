#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
asr/transcribe.py — 本地离线语音识别（ASR），"PickOne" Web App 的可选模块。

设计原则（见 contracts/api.md 第 18 行 /api/asr 约定）：
  * 任何失败都返回空字符串 ""，绝不抛异常、绝不非零退出。
  * 后端因此可以无条件把结果包成 {"text": ...}，或当 "" 时返回
    {"text":"","unavailable":true} 让前端退回手输。

CLI:
  /home/Developer/workspace/.venv/bin/python asr/transcribe.py <audio_file> [--lang zh]
  -> stdout 只有识别出的纯文本（失败时 stdout 为空）

Library:
  from asr.transcribe import transcribe
  text = transcribe("/tmp/a.webm", lang="zh")   # 失败 -> ""

方案：faster-whisper (CTranslate2) + Whisper small (int8, CPU)。
模型目录：asr/models/faster-whisper-small/（见 README.md / install.sh）

⚠️ 必须用 venv 解释器运行（Python 3.13）：
   /home/Developer/workspace/.venv/bin/python
"""

from __future__ import annotations

import os
import sys
import time
import json
import argparse
import subprocess
from typing import Optional

# ---------------------------------------------------------------------------
# 配置（全部可用环境变量覆盖，方便后端调参）
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))

# 模型目录：优先 env，其次本仓库 asr/models/
MODEL_DIR = os.environ.get(
    "ASR_MODEL_DIR",
    os.path.join(HERE, "models", "faster-whisper-small"),
)
MODEL_ID = os.environ.get("ASR_MODEL_ID", "small")

# 推理设备/精度。默认 CPU + int8（本机 GB10 20 核，int8 明显更快且省内存）。
# 实测：int8 平均 1.74s/条，float32 2.73s；int8_float16 本机 CPU 后端不支持。
DEVICE = os.environ.get("ASR_DEVICE", "cpu")
COMPUTE = os.environ.get("ASR_COMPUTE", "int8")
# 占用核数保守一些（机器上还有 llama-server 在跑），可用 ASR_THREADS 调。
# 实测 GB10 20 核：threads=4 最优（1.8s），8 略快（1.65s），12/16 反而变慢
# （与 llama-server 抢核 + 线程调度开销），所以默认 4。
THREADS = int(os.environ.get("ASR_THREADS", "4"))

# 单次转写的墙钟超时（秒）。超时 -> 返回 ""。
DEFAULT_TIMEOUT = float(os.environ.get("ASR_TIMEOUT", "60"))


# 领域提示词（Whisper initial_prompt）：能明显修正专有名词，
# 例如 "健身房练一会儿" 常被听成 "健身、访练一会儿"（CER 由提示词压下来）。
# 注意：提示词里带"排练"这类词有让模型幻觉补词的风险，且会改变数字写法。
# 置空（ASR_PROMPT=""）即完全不做领域偏置。
DEFAULT_PROMPT = os.environ.get(
    "ASR_PROMPT",
    "以下是关于运动健身、吃饭聚餐和行程安排的中文对话：健身房、跑步、火锅店、看电影、预算。",
)


# 支持的音频/视频容器扩展名（浏览器 MediaRecorder 默认 webm/opus）。
SUPPORTED_EXT = {
    ".webm", ".ogg", ".oga", ".opus", ".mp4", ".m4a", ".aac",
    ".wav", ".mp3", ".flac", ".mkv", ".mov", ".amr", ".3gp", ".caf", ".wma",
}

# huggingface.co 在本机不可达，只能走镜像（见 README「网络」一节）。
HF_MIRROR = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    """诊断信息一律走 stderr，stdout 只放识别文本。"""
    print(f"[asr] {msg}", file=sys.stderr)


def _fail(reason: str) -> str:
    """统一的失败出口：记一条 stderr 日志，返回空字符串。"""
    _log(f"fail: {reason}")
    return ""


def _is_supported(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in SUPPORTED_EXT


def _ffmpeg_bin() -> Optional[str]:
    from shutil import which
    return which("ffmpeg")


def _to_wav(path: str, sr: int = 16000) -> Optional[str]:
    """
    用系统 ffmpeg 把任意输入转成 16k 单声道 wav（临时文件）。

    faster-whisper 内部用 av(libav) 解码，多数 webm/opus 可直接读；
    但浏览器录的 webm 有时缺 duration 头会让 av 卡住/报错，
    所以这里做一层兜底转码。返回临时 wav 路径，失败返回 None。
    """
    ff = _ffmpeg_bin()
    if not ff:
        return None
    import tempfile
    fd, out = tempfile.mkstemp(prefix="asr_", suffix=".wav")
    os.close(fd)
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", path, "-ac", "1", "-ar", str(sr), "-f", "wav", out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:                                  # noqa: BLE001
        _log(f"ffmpeg exec error: {e}")
        try:
            os.unlink(out)
        except OSError:
            pass
        return None
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 1000:
        _log(f"ffmpeg rc={r.returncode} err={r.stderr.strip()[:200]}")
        try:
            os.unlink(out)
        except OSError:
            pass
        return None
    return out


# 进程内单例：模型只加载一次（后端若常驻 import，可省 ~2s 冷启动）。
_MODEL = None
_MODEL_ERR: Optional[str] = None


def _get_model():
    """加载 faster-whisper WhisperModel；失败记住原因，不抛异常。"""
    global _MODEL, _MODEL_ERR
    if _MODEL is not None or _MODEL_ERR is not None:
        return _MODEL

    try:
        os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
        # 避免 xet/hf hub 联网校验拖慢冷启动
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from faster_whisper import WhisperModel           # type: ignore
    except Exception as e:                                 # noqa: BLE001
        _MODEL_ERR = f"import faster_whisper failed: {type(e).__name__}: {e}"
        return None

    # 本地目录优先；没有本地目录则退回 repo id（会尝试联网下载）。
    local_ok = os.path.isfile(os.path.join(MODEL_DIR, "model.bin"))
    src = MODEL_DIR if local_ok else MODEL_ID
    if not local_ok:
        os.environ.pop("HF_HUB_OFFLINE", None)   # 需要联网
        _log(f"local model dir missing, fallback to hub id '{MODEL_ID}'")

    try:
        t0 = time.time()
        _MODEL = WhisperModel(src, device=DEVICE, compute_type=COMPUTE,
                              cpu_threads=THREADS)
        _log(f"model loaded in {time.time() - t0:.2f}s "
             f"(src={os.path.basename(src.rstrip('/'))}, "
             f"device={DEVICE}, compute={COMPUTE}, threads={THREADS})")
    except Exception as e:                                 # noqa: BLE001
        _MODEL_ERR = f"WhisperModel load failed: {type(e).__name__}: {e}"
        _log(_MODEL_ERR)
        _MODEL = None
    return _MODEL


class _Timeout(Exception):
    pass


def _run_with_timeout(fn, timeout: float):
    """
    纯标准库的墙钟超时：在子线程里跑 fn，主线程 join(timeout)。
    （超时后子线程无法强杀，但本函数所在的进程通常会随即退出；
      后端用 subprocess + timeout 时由外层兜底。）
    """
    import threading
    box: dict = {}

    def wrapper():
        try:
            box["val"] = fn()
        except BaseException as e:                          # noqa: BLE001
            box["err"] = e

    th = threading.Thread(target=wrapper, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise _Timeout(f"transcribe exceeded {timeout}s")
    if "err" in box:
        raise box["err"]
    return box.get("val", "")


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def transcribe(path: str, lang: str = "zh",
               timeout: Optional[float] = None,
               prompt: Optional[str] = ...) -> str:
    """
    把音频文件转成文字。**永远不抛异常**，失败返回 ""。

    Args:
        path: 音频文件路径（webm/ogg/mp4/wav/mp3/... 均可）。
        lang: 语言代码，默认 "zh"。传 "auto"/None 则自动检测。
        timeout: 本次转写的墙钟秒数，默认 ASR_TIMEOUT(60)。
        prompt: Whisper 领域提示词；... = 用 DEFAULT_PROMPT，传 None = 不用。

    Returns:
        识别文本（已 strip）；无法识别 / 文件不存在 / 依赖缺失 -> ""。
    """

    timeout = DEFAULT_TIMEOUT if timeout is None else float(timeout)

    # ---- 入参校验（这些是最常见的失败路径，必须安静返回 ""）----
    if not path or not isinstance(path, str):
        return _fail("empty path")
    if not os.path.exists(path):
        return _fail(f"file not found: {path}")
    if not os.path.isfile(path):
        return _fail(f"not a regular file: {path}")
    size = os.path.getsize(path)
    if size < 100:
        return _fail(f"file too small ({size} bytes): {path}")
    if size > 200 * 1024 * 1024:
        return _fail(f"file too large ({size} bytes)")
    if not _is_supported(path):
        # 未知扩展名也试一下（后端可能拿到没有扩展名的临时文件）
        _log(f"unusual extension for {path}, trying anyway")

    try:
        return _run_with_timeout(
            lambda: _transcribe_inner(path, lang, timeout, prompt), timeout)
    except _Timeout as e:
        return _fail(str(e))
    except Exception as e:                                 # noqa: BLE001
        return _fail(f"{type(e).__name__}: {e}")


def _transcribe_inner(path: str, lang: str, timeout: float,
                      prompt=...) -> str:
    model = _get_model()
    if model is None:
        return _fail(_MODEL_ERR or "model unavailable")

    if prompt is ...:
        prompt = DEFAULT_PROMPT or None

    # 先直接喂原文件（av 能解多数 webm/opus）；不行再 ffmpeg 兜底。
    attempts = [path]
    rest = max(5.0, timeout - 5)

    def _do(target: str) -> str:
        seg_lang = None if (lang or "").lower() in ("auto", "", "none") else lang
        segments, info = model.transcribe(
            target,
            language=seg_lang,
            beam_size=5,
            vad_filter=True,                 # 静音过滤，避免幻听重复句
            vad_parameters={"min_silence_duration_ms": 400},
            initial_prompt=prompt or None,
            condition_on_previous_text=False,  # 防止一句错话污染后面
        )

        parts = []
        for s in segments:                   # 迭代才是真正开始推理
            txt = (s.text or "").strip()
            if txt:
                parts.append(txt)
            if time.time() - t_start > rest:
                raise _Timeout(f"decoding exceeded {rest}s")
        return " ".join(parts).strip()

    t_start = time.time()
    last_err = ""
    for i, target in enumerate(attempts):
        try:
            text = _do(target)
            dt = time.time() - t_start
            _log(f"ok: {len(text)} chars in {dt:.2f}s (attempt {i + 1})")
            return text
        except _Timeout:
            raise
        except Exception as e:                            # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            _log(f"attempt {i + 1} failed on {target}: {last_err}")

    # 兜底：ffmpeg 转 16k mono wav 再试一次
    ff = _ffmpeg_bin()
    if ff:
        wav = _to_wav(path)
        if wav:
            try:
                t_start = time.time()
                text = _do(wav)
                _log(f"ok: {len(text)} chars in {time.time() - t_start:.2f}s "
                     f"(ffmpeg->wav fallback)")
                return text
            except Exception as e:                        # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                _log(f"ffmpeg fallback failed: {last_err}")
            finally:
                try:
                    os.unlink(wav)
                except OSError:
                    pass
    else:
        _log("ffmpeg not available for fallback transcode")

    return _fail(f"all decode attempts failed; last={last_err}")


def is_available() -> bool:
    """依赖与模型是否就绪（后端 /api/health 可以用）。"""
    if _MODEL is not None:
        return True
    try:
        os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
        import faster_whisper                # noqa: F401
    except Exception:
        return False
    return os.path.isfile(os.path.join(MODEL_DIR, "model.bin"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="本地离线 ASR（faster-whisper）。stdout 只输出识别文本。")
    p.add_argument("audio", help="音频文件（webm/ogg/mp4/wav/mp3...）")
    p.add_argument("--lang", default="zh", help="语言代码，默认 zh；auto 自动检测")
    p.add_argument("--timeout", type=float, default=None,
                   help=f"墙钟秒数，默认 {DEFAULT_TIMEOUT:g}")
    p.add_argument("--prompt", default=...,
                   help="Whisper 领域提示词；传空串关闭默认提示词")
    p.add_argument("--json", action="store_true",
                   help="输出 {\"text\":...,\"unavailable\":bool} 便于后端直接透传")
    a = p.parse_args(argv)

    # 契约要求"绝不报错"：连 main 内部意外都要吞掉，输出空文本 + exit 0。
    try:
        prompt = DEFAULT_PROMPT if a.prompt is ... else a.prompt
        text = transcribe(a.audio, lang=a.lang, timeout=a.timeout,
                          prompt=prompt)
    except BaseException as e:                              # noqa: BLE001
        _fail(f"unexpected in main: {type(e).__name__}: {e}")
        text = ""

    if a.json:
        print(json.dumps({"text": text, "unavailable": text == ""},
                         ensure_ascii=False))
    else:
        print(text)
    return 0            # 永远 0，契约要求"绝不报错"



if __name__ == "__main__":
    sys.exit(main())
