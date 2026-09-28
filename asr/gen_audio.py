#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""asr/.tmp 测试音频重新生成脚本（edge-tts，中文普通话）。

用法：
    /home/Developer/workspace/.venv/bin/python asr/gen_audio.py

产出（asr/.tmp/）：
    e1_workout.mp3 / .wav / .webm   今晚是去健身房跑步，还是去吃火锅，或者在家看电影
    e2_practice.mp3 / .wav / .webm  我已经四天没有运动了，今晚想去健身房练一会儿
    e3_dinner.mp3 / .wav / .webm    明天下午三点和朋友吃饭，预算不超过两百块
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, ".tmp")

VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "+0%"

CASES = [
    ("e1_workout", "今晚是去健身房跑步，还是去吃火锅，或者在家看电影"),
    ("e2_practice", "我已经四天没有运动了，今晚想去健身房练一会儿"),
    ("e3_dinner", "明天下午三点和朋友吃饭，预算不超过两百块"),
]


def sh(cmd, timeout=180):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


async def _synth(text: str, out_mp3: str) -> bool:
    import edge_tts
    communicate = edge_tts.Communicate(text, VOICE, rate=RATE)
    await communicate.save(out_mp3)
    return os.path.isfile(out_mp3) and os.path.getsize(out_mp3) > 0


def convert(src: str, dst: str, extra=None) -> bool:
    ff = shutil.which("ffmpeg")
    if not ff:
        print(f"[gen] ffmpeg 不存在，跳过 {dst}")
        return False
    rc, _, se = sh([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src]
                   + (extra or []) + [dst])
    if rc != 0:
        print(f"[gen] ffmpeg {src}->{dst} 失败: {se[:200]}")
        return False
    return True


def main() -> int:
    os.makedirs(TMP, exist_ok=True)
    ok = True
    for tag, text in CASES:
        mp3 = os.path.join(TMP, f"{tag}.mp3")
        try:
            if not asyncio.run(_synth(text, mp3)):
                print(f"[gen] edge-tts 合成失败: {tag}")
                ok = False
                continue
        except Exception as e:  # noqa: BLE001
            print(f"[gen] edge-tts 异常 {tag}: {type(e).__name__}: {e}")
            ok = False
            continue
        print(f"[gen] {tag}.mp3  ({os.path.getsize(mp3)} bytes)  <- {text}")
        # wav（16k 单声道，贴近 ASR 输入）
        if not convert(mp3, os.path.join(TMP, f"{tag}.wav"),
                       ["-ar", "16000", "-ac", "1"]):
            ok = False
        # webm/opus（浏览器 MediaRecorder 实际格式）
        if not convert(mp3, os.path.join(TMP, f"{tag}.webm"),
                       ["-c:a", "libopus", "-b:a", "48k"]):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
