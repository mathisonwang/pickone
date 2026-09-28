#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
asr/selftest.py — ASR 模块自测（产出 SELFTEST.md 用的原始数据）。

  /home/Developer/workspace/.venv/bin/python asr/selftest.py          # 全量
  /home/Developer/workspace/.venv/bin/python asr/selftest.py --quick  # 只跑安装后冒烟

覆盖：
  1) 生成中文测试语音（espeak-ng 本地前缀 / 或复用已有 wav）
  2) wav 转写：文本 / 耗时 / 进程内存峰值
  3) webm(opus) 转写（浏览器 MediaRecorder 实际格式）
  4) 失败路径：不存在的文件 / 纯文本文件 / 空文件 -> "" 且 exit 0
结果写 asr/.selftest_result.json（供 README/SELFTEST 引用）。
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
TMP = os.path.join(HERE, ".tmp")
RESULT = os.path.join(HERE, ".selftest_result.json")

# espeak-ng 免 root 安装位置（.deb 手工解包，见 README）
ESPEAK_PREFIX = os.path.join(HERE, ".local")
ESPEAK_BIN = os.path.join(ESPEAK_PREFIX, "usr", "bin", "espeak-ng")
ESPEAK_DATA = os.path.join(ESPEAK_PREFIX, "usr", "lib", "aarch64-linux-gnu",
                           "espeak-ng-data")

SENTENCES = [
    ("今晚是去健身房跑步，还是去吃火锅，或者在家看电影", "e1_workout"),
    ("我已经四天没有运动了，今晚想去健身房练一会儿", "e2_practice"),
    ("明天下午三点和朋友吃饭，预算不超过两百块", "e3_dinner"),
]


def log(m):
    print(f"[selftest] {m}", flush=True)


def sh(cmd, timeout=300, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       env=env)
    return r.returncode, r.stdout, r.stderr


# --------------------------------------------------------------- 音频生成
def gen_wav(text: str, out: str) -> bool:
    """用 espeak-ng（本地解包版）生成中文 wav。"""
    if not os.path.isfile(ESPEAK_BIN) or not os.path.isdir(ESPEAK_DATA):
        return False
    env = dict(os.environ)
    env["ESPEAK_DATA_PATH"] = ESPEAK_DATA
    env["LD_LIBRARY_PATH"] = (os.path.join(ESPEAK_PREFIX, "usr", "lib",
                                           "aarch64-linux-gnu")
                              + ":" + env.get("LD_LIBRARY_PATH", ""))
    rc, so, se = sh([ESPEAK_BIN, "-v", "cmn", "-s", "125", "-p", "50",
                     "-w", out, text], env=env)
    if rc != 0 or not os.path.exists(out):
        log(f"espeak-ng failed rc={rc} {se[:200]}")
        return False
    return True


def convert(src: str, dst: str) -> bool:
    ff = shutil.which("ffmpeg")
    if not ff:
        return False
    rc, _, se = sh([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                    dst])
    if rc != 0:
        log(f"ffmpeg {src}->{dst} failed: {se[:200]}")
        return False
    return True


def audio_dur(path: str) -> float:
    rc, so, _ = sh(["ffprobe", "-v", "error", "-show_entries",
                    "format=duration", "-of", "csv=p=0", path])
    try:
        return round(float(so.strip()), 2)
    except Exception:
        return -1.0


# --------------------------------------------------------------- 内存测量
def mem_peak_mb() -> float:
    """当前进程 ru_maxrss（KB->MB）。子进程内存用 /usr/bin/time 单独测。"""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)


def run_cli_measure(path: str, lang: str = "zh"):
    """跑 CLI 子进程，测耗时 + 峰值 RSS（用 /usr/bin/time -v）。"""
    tv = "/usr/bin/time"
    if not os.path.exists(tv):
        t0 = time.time()
        rc, so, se = sh([PY, os.path.join(HERE, "transcribe.py"), path,
                         "--lang", lang], timeout=300)
        return {"text": so.strip(), "rc": rc, "secs": round(time.time() - t0, 2),
                "peak_rss_mb": None, "stderr": se.strip()[-500:]}
    tmperr = os.path.join(TMP, "time.err")
    t0 = time.time()
    r = subprocess.run([tv, "-v", "-o", tmperr, PY,
                        os.path.join(HERE, "transcribe.py"), path,
                        "--lang", lang], capture_output=True, text=True,
                       timeout=300)
    secs = round(time.time() - t0, 2)
    rss = None
    if os.path.exists(tmperr):
        for line in open(tmperr, encoding="utf-8", errors="ignore"):
            if "Maximum resident set size" in line:
                rss = round(int(line.split(":")[1].strip()) / 1024.0, 1)
    return {"text": r.stdout.strip(), "rc": r.returncode, "secs": secs,
            "peak_rss_mb": rss, "stderr": r.stderr.strip()[-500:]}


# --------------------------------------------------------------- 用例
def case_fail_path(name: str, arg: str) -> dict:
    """失败路径：必须 stdout 空/无栈 + exit 0。"""
    rc, so, se = sh([PY, os.path.join(HERE, "transcribe.py"), arg], timeout=180)
    tb = "Traceback" in se
    return {"name": name, "arg": arg, "rc": rc, "stdout": so.strip(),
            "traceback": tb, "stderr_tail": se.strip()[-300:],
            "pass": rc == 0 and not tb and so.strip() == ""}


def main() -> int:
    quick = "--quick" in sys.argv
    os.makedirs(TMP, exist_ok=True)
    out = {"generated_by": "asr/selftest.py", "python": PY,
           "cwd": os.getcwd(), "quick": quick, "cases": [], "notes": []}

    # 1) 音频生成
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    wavs = []
    have_espeak = os.path.isfile(ESPEAK_BIN)
    out["tts"] = "espeak-ng(local deb prefix)" if have_espeak else "none"
    if not have_espeak:
        out["notes"].append("espeak-ng 不可用，跳过音频生成（用已有样例）")
    sentences = SENTENCES[:1] if quick else SENTENCES
    for text, tag in sentences:
        w = os.path.join(TMP, f"{tag}.wav")
        if have_espeak and gen_wav(text, w):
            wavs.append((text, tag, w))
        elif os.path.exists(w):
            wavs.append((text, tag, w))
    if not wavs:
        out["notes"].append("没有可用测试音频，无法验证准确率")
        json.dump(out, open(RESULT, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        log("no audio available"); return 1

    # 2) wav 转写
    for text, tag, w in wavs:
        dur = audio_dur(w)
        r = run_cli_measure(w)
        r.update({"name": f"wav:{tag}", "expected": text, "audio": w,
                  "audio_dur_s": dur, "format": "wav"})
        r["pass"] = r["rc"] == 0 and r["text"] != ""
        out["cases"].append(r)
        log(f"wav {tag}: {r['secs']}s rss={r['peak_rss_mb']}MB -> {r['text'][:60]}")

    if quick:
        json.dump(out, open(RESULT, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        log(f"quick done -> {RESULT}")
        return 0 if all(c["pass"] for c in out["cases"]) else 1

    # 3) 容器格式（webm/opus 是浏览器 MediaRecorder 默认）
    text, tag, w = wavs[0]
    for ext, args in [("webm", ["-c:a", "libopus", "-b:a", "48k"]),
                      ("ogg", ["-c:a", "libopus", "-b:a", "48k"]),
                      ("mp4", ["-c:a", "aac", "-b:a", "64k"])]:
        dst = os.path.join(TMP, f"{tag}.{ext}")
        if not convert(w, dst):
            out["notes"].append(f"ffmpeg 生成 .{ext} 失败")
            continue
        r = run_cli_measure(dst)
        r.update({"name": f"{ext}:{tag}", "expected": text, "audio": dst,
                  "audio_dur_s": audio_dur(dst), "format": ext})
        r["pass"] = r["rc"] == 0 and r["text"] != ""
        out["cases"].append(r)
        log(f"{ext}: {r['secs']}s -> {r['text'][:60]}")

    # 4) 失败路径
    txtf = os.path.join(TMP, "notaudio.txt")
    open(txtf, "w").write("这只是一个纯文本文件，不是音频。\n" * 20)
    empty = os.path.join(TMP, "empty.webm")
    open(empty, "wb").close()
    out["cases"].append(case_fail_path("fail:missing_file",
                                       "/tmp/definitely_not_here_9182736.webm"))
    out["cases"].append(case_fail_path("fail:text_file", txtf))
    out["cases"].append(case_fail_path("fail:empty_webm", empty))

    # 5) 模型冷启动 vs 复用（import 内二次调用）
    t0 = time.time()
    sys.path.insert(0, ROOT)
    from asr.transcribe import transcribe, is_available  # noqa: E402
    load_s = None
    first = transcribe(wavs[0][2])
    first_s = round(time.time() - t0, 2)
    t1 = time.time()
    second = transcribe(wavs[0][2])
    second_s = round(time.time() - t1, 2)
    out["reuse"] = {"available": is_available(), "first_call_s": first_s,
                    "second_call_s": second_s, "first_chars": len(first),
                    "second_chars": len(second),
                    "peak_rss_mb": mem_peak_mb()}
    log(f"reuse: first={first_s}s second={second_s}s rss={out['reuse']['peak_rss_mb']}MB")

    json.dump(out, open(RESULT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    npass = sum(1 for c in out["cases"] if c.get("pass"))
    log(f"done: {npass}/{len(out['cases'])} pass -> {RESULT}")
    return 0 if npass == len(out["cases"]) else 1


if __name__ == "__main__":
    sys.exit(main())
