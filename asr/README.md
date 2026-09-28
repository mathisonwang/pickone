# asr/ — 本地离线语音识别模块（可选）

给"PickOne"的 **按住说话 → 转文字** 提供**完全离线**的中文语音识别。
独立模块，不依赖 `server/`，也不被 `server/` 强绑定：后端调不通就返回
`{"text":"","unavailable":true}`，前端自动退回手输（见 `contracts/api.md`）。

---

## 1. 方案与模型

| 项 | 选择 | 说明 |
|---|---|---|
| 引擎 | **faster-whisper 1.2.1**（CTranslate2 4.8.2） | aarch64 有官方 wheel，纯 CPU 推理，无需 CUDA |
| 模型 | **Whisper `small`**（`Systran/faster-whisper-small`） | `model.bin` = **483,546,902 B ≈ 461 MiB** |
| 精度 | **int8**（`compute_type="int8"`） | 比 fp32 快约 2~3×，内存减半，中文准确率损失很小 |
| 设备 | **CPU**，默认 `cpu_threads=4` | 本机 GB10 有 20 核；保守取 4 是因为 llama-server 也在跑 |
| 解码 | `av`(libav) 直接解 webm/opus；失败再调系统 `ffmpeg` 转 16k mono wav | 浏览器 MediaRecorder 默认 webm/opus，可直接读 |
| VAD | `vad_filter=True` | 过滤静音，避免 Whisper 幻听/重复句 |

模型目录：**`asr/models/faster-whisper-small/`**（刻意放在项目内，不放 home 根目录）
```
config.json  2.4 KB
tokenizer.json  2.2 MB
vocabulary.txt  460 KB
model.bin  483,546,902 B   # sha256 3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671
```

### 网络注意（重要，别重复踩）
- `huggingface.co` 本机 **不可达**（`Network is unreachable`）。
- `pypi.org` 与 **`hf-mirror.com` 可达**。因此下载模型必须
  `export HF_ENDPOINT=https://hf-mirror.com`（`transcribe.py` / `install.sh` 已内置）。
- pypi 很慢（实测 ~100–200 KB/s，装 `av` 用了 43 分钟），所以 `install.sh`
  用 `UV_HTTP_TIMEOUT=600` + 逐包 `--no-deps` + 5 次重试，**中断后重跑即可续装**。
- venv 内没有 `pip`，只能用 `/home/Developer/.local/bin/uv pip install --python <venv py>`。

---

## 2. 安装

```bash
bash /home/Developer/workspace/choice/asr/install.sh          # 幂等，可反复跑
ASR_MODEL=base bash .../asr/install.sh                        # 想更小更快（145MB）
```
脚本做三件事，**每一步都是"已就绪就跳过"**：
1. 检查 venv 解释器（`/home/Developer/workspace/.venv/bin/python`）与 `uv`；
2. 装 `numpy av ctranslate2 tokenizers onnxruntime huggingface-hub faster-whisper`
   （已 import 成功的包直接跳过）；
3. 下载模型 4 个文件（按文件字节数判断是否已下全，`curl -C -` 断点续传）；
4. 跑一次冒烟自检。

> 依赖清单里 `onnxruntime` 是 faster-whisper 的声明依赖（用于对齐模型分支），
> 纯 CTranslate2 的 Whisper 路径其实用不到它；但缺它 `pip` 会认为装不完整，
> 所以还是装上，避免后续 `uv pip install` 又去补。

---

## 3. 用法

### CLI
```bash
/home/Developer/workspace/.venv/bin/python asr/transcribe.py <audio_file> [--lang zh]
```
- **stdout 只有识别文本**（失败时 stdout 为空），诊断信息一律走 stderr。
- **退出码永远是 0**（契约要求"绝不报错"）。
- `--json` 直接输出 `{"text":"...","unavailable":bool}`，后端可原样透传。
- `--timeout 30` 覆盖默认 60s 墙钟。

```bash
# 例
PY=/home/Developer/workspace/.venv/bin/python
$PY asr/transcribe.py rec.webm                      # -> 今晚是去健身房跑步还是去楼下那家火锅店
$PY asr/transcribe.py rec.webm --json               # -> {"text":"...","unavailable":false}
$PY asr/transcribe.py nope.webm; echo "rc=$?"       # -> 空行, rc=0
```

### Python
```python
import sys; sys.path.insert(0, "/home/Developer/workspace/choice")
from asr.transcribe import transcribe, is_available

text = transcribe("/tmp/rec.webm", lang="zh")   # 失败 -> ""，永不抛异常
ok   = is_available()                           # 依赖+模型是否就绪（可用于 /api/health）
```

### 环境变量
| 变量 | 默认 | 用途 |
|---|---|---|
| `ASR_MODEL_DIR` | `asr/models/faster-whisper-small` | 模型目录 |
| `ASR_MODEL_ID` | `small` | 本地目录缺失时回退的 HF repo id |
| `ASR_DEVICE` / `ASR_COMPUTE` | `cpu` / `int8` | 改 `float16`+`cuda` 可试 GPU（GB10 需 CUDA 版 ctranslate2，当前 wheel 是 CPU） |
| `ASR_THREADS` | `4` | 和 llama-server 抢核时调小/调大 |
| `ASR_TIMEOUT` | `60` | 单次转写墙钟秒 |
| `HF_ENDPOINT` | `https://hf-mirror.com` | 模型镜像 |

---

## 4. 给后端同事的调用建议（`/api/asr`）

**推荐 subprocess 调用**（隔离得最干净：模型崩了不影响 web 进程；超时可控）：

```python
# server/app.py 里 /api/asr 的实现建议
import json, subprocess, tempfile, os

ASR_PY  = "/home/Developer/workspace/.venv/bin/python"
ASR_CLI = "/home/Developer/workspace/choice/asr/transcribe.py"
ASR_TIMEOUT = 45          # 秒，见下面"超时建议值"

@app.post("/api/asr")
async def asr(request):
    form = await request.form()
    up = form.get("file") or form.get("audio")
    if up is None or not hasattr(up, "read"):
        return JSONResponse({"text": "", "unavailable": True})   # 不 500
    data = await up.read()
    if not data or len(data) < 500:                              # 太短/静音没意义
        return JSONResponse({"text": "", "unavailable": True})

    suffix = os.path.splitext(up.filename or "")[1] or ".webm"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        lang = (form.get("lang") or "zh")
        try:
            r = subprocess.run(
                [ASR_PY, ASR_CLI, tmp, "--lang", str(lang)],
                capture_output=True, text=True, timeout=ASR_TIMEOUT)
            text = (r.stdout or "").strip()
        except subprocess.TimeoutExpired:
            return JSONResponse({"text": "", "unavailable": True})
        except Exception:
            return JSONResponse({"text": "", "unavailable": True})
        if not text:
            return JSONResponse({"text": "", "unavailable": True})
        return JSONResponse({"text": text})
    finally:
        try: os.unlink(tmp)
        except OSError: pass
```

要点：
1. **`ASR_TIMEOUT = 45s`**：实测 7~11s 音频 CPU int8 只要 2~4s（见 SELFTEST.md），
   45s 给首次冷启动 + 机器上 llama-server 抢核留足余量。
   前端 fetch 超时建议 **60s**。
2. **冷启动**：模型加载约 1.5~3s（461MB int8）。若嫌每次 subprocess 都冷启动，
   可在 app 启动时 `import asr.transcribe` 并在线程池里复用单例（`_MODEL` 已做进程内缓存），
   第二次调用只花推理时间。
3. **并发**：ASR 吃满 4 核，**同一时刻只跑一个**。建议加一把
   `threading.Semaphore(1)`，拿不到锁直接返回 `{"text":"","unavailable":true}`
   （比排队更符合"演示不卡住"的目标）。
4. **绝不 500**：`transcribe.py` 已经保证 exit 0 / 空串，但 subprocess 层
   （超时、fork 失败）仍要兜底成 `unavailable:true`。
5. **`/api/health`** 里加 `asr_available: is_available()`，前端可提前决定要不要显示麦克风按钮。
6. **不要占 8888**。若要把 ASR 做成常驻 HTTP 服务，用 `127.0.0.1:8890`。

---

## 5. 测试音频（`asr/.tmp/`）

演示与自测用的预录音频，均用 **edge-tts 生成的中文测试音频**（非真人录音）。
重新生成：`/home/Developer/workspace/.venv/bin/python asr/gen_audio.py`。

| 文件 | 内容 | 用途 |
|---|---|---|
| `e1_workout.webm` | 今晚是去健身房跑步，还是去吃火锅，或者在家看电影。 | 演示主案例的语音输入（识别准确） |
| `e3_dinner.webm` | 明天下午3点和朋友吃饭，预算不超过200块。 | 演示备用（识别准确） |
| `e2_practice.webm` | 我已经四天没有运动了，今晚想去健身房练一会儿。 | 准确率诚实素材：small 会把"健身房练一会儿"误识成"健身、访练一会儿" |

> 文件名由代码组统一维护；若 `asr/.tmp/` 下音频缺失，`transcribe.py` 仍会正常返回
> 空串 + `unavailable`，不影响 `/api/asr` 契约。

## 6. 实测结果（摘要，原始数据见 SELFTEST.md）

- 准确率、延迟、内存峰值、各容器格式、失败路径 → 见 `SELFTEST.md`。
- 已知短板：**espeak-ng 合成的机器音**比真人语音更难识别，
  真人录音的准确率会明显高于自测数字。

## 7. 已知缺陷
1. **首次调用慢**：模型加载 1.5~3s（进程内复用可避免）。
2. **中文准确率**：`small` 对口语/专有名词（"健身房练基本功"这类）会有偏差；
   需要更准可换 `medium`（约 1.5GB，内存够但下载极慢）。
3. **CPU 独占**：4 核满载，和 llama-server 并发时会互相拖慢。
4. **GPU 未启用**：当前 ctranslate2 wheel 是 CPU 版；GB10 上跑 CUDA 需要额外的
   CUDA 12 运行库，本次未做（下载量与风险都高）。
5. `huggingface.co` 不可达 → 换模型时记得 `HF_ENDPOINT` 镜像。
