#!/usr/bin/env bash
# asr/install.sh — 幂等安装本地 ASR（已装则跳过，可反复执行）
#
#   bash asr/install.sh              # 装依赖 + 下载模型（默认 small）
#   ASR_MODEL=base bash asr/install.sh   # 改装 base（更小更快，准确率略低）
#   ASR_SKIP_WHEEL=1 bash asr/install.sh # 只补模型
#
# 硬性环境：必须用 venv 解释器（3.13）；系统 python3(3.12) import 不了该 site-packages。
set -uo pipefail   # 故意不用 -e：网络抖动要能重试而不是直接退出

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${ASR_PY:-/home/Developer/workspace/.venv/bin/python}"
UV="${ASR_UV:-/home/Developer/.local/bin/uv}"
MODELS_DIR="${ASR_MODELS_DIR:-$HERE/models}"

# huggingface.co 本机不可达（Network is unreachable），必须走镜像
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-600}"

ASR_MODEL="${ASR_MODEL:-small}"           # tiny | base | small
REPO="Systran/faster-whisper-${ASR_MODEL}"
DEST="$MODELS_DIR/faster-whisper-${ASR_MODEL}"
# 各模型 model.bin 的期望字节数（用于校验下载是否完整）
EXPECT_BYTES=483546902                    # small
[ "$ASR_MODEL" = "base" ] && EXPECT_BYTES=145217727
[ "$ASR_MODEL" = "tiny" ] && EXPECT_BYTES=75538215

say() { echo "[asr-install] $*"; }

# ---------------------------------------------------------------- 1. 解释器
if [ ! -x "$PY" ]; then
  say "ERROR: 找不到 venv 解释器 $PY"; exit 1
fi
say "python : $($PY -V 2>&1)  ($PY)"
[ -x "$UV" ] || { say "ERROR: 找不到 uv ($UV)，无法装包"; exit 1; }

# ---------------------------------------------------------------- 2. 依赖
need_pkgs=0
if [ "${ASR_SKIP_WHEEL:-0}" = "1" ]; then
  say "ASR_SKIP_WHEEL=1，跳过 pip 安装"
elif "$PY" -c "import faster_whisper, ctranslate2, av, numpy" >/dev/null 2>&1; then
  say "依赖已安装，跳过（faster-whisper $("$PY" -c 'import faster_whisper;print(faster_whisper.__version__)')）"
else
  need_pkgs=1
fi

if [ "$need_pkgs" = "1" ]; then
  say "安装依赖（网络较慢，逐个装 + 重试；已装的会自动跳过）"
  # 逐个 --no-deps 安装：整体装时单个包流中断会连累全部，逐个装更好重试。
  for pkg in numpy av ctranslate2 tokenizers onnxruntime huggingface-hub faster-whisper; do
    mod="${pkg//-/_}"
    if "$PY" -c "import $mod" >/dev/null 2>&1; then
      say "  = $pkg 已存在，跳过"; continue
    fi
    ok=0
    for try in 1 2 3 4 5; do
      if "$UV" pip install --python "$PY" --no-deps "$pkg" >/dev/null 2>&1 \
         && "$PY" -c "import $mod" >/dev/null 2>&1; then
        say "  + $pkg (try $try)"; ok=1; break
      fi
      say "  ! $pkg 第 $try 次失败，重试..."
      sleep 3
    done
    [ "$ok" = "1" ] || { say "ERROR: $pkg 安装失败（网络问题？重跑本脚本即可续装）"; exit 1; }
  done
  # 补装可能缺失的间接依赖（有则跳过）
  "$UV" pip install --python "$PY" faster-whisper >/dev/null 2>&1 || true
  "$PY" -c "import faster_whisper, ctranslate2, av, numpy" >/dev/null 2>&1 \
    || { say "ERROR: 依赖仍不可用"; "$PY" -c "import faster_whisper" ; exit 1; }
  say "依赖 OK"
fi

# ---------------------------------------------------------------- 3. 模型
mkdir -p "$DEST"
get_file() {  # get_file <name> [required_bytes]
  local f="$1" want="${2:-0}" out="$DEST/$1"
  if [ -s "$out" ] && [ "$(stat -c%s "$out")" -ge "$want" ]; then
    say "  = $f 已存在 ($(stat -c%s "$out") bytes)，跳过"; return 0
  fi
  local try
  for try in 1 2 3 4 5; do
    # -C - 断点续传；镜像偶尔 302 到 xet CDN，需要 -L
    if curl -fsSL --retry 5 --retry-all-errors -C - --max-time 1800 \
         -o "$out" "$HF_ENDPOINT/$REPO/resolve/main/$f" 2>/dev/null; then
      local got; got="$(stat -c%s "$out" 2>/dev/null || echo 0)"
      if [ "$got" -ge "$want" ]; then
        say "  + $f ($got bytes, try $try)"; return 0
      fi
      say "  ! $f 不完整 ($got/$want)，续传..."
    fi
    sleep 3
  done
  say "ERROR: $f 下载失败"; return 1
}

say "模型：$REPO -> $DEST"
get_file config.json 1000        || exit 1
get_file tokenizer.json 100000   || exit 1
get_file vocabulary.txt 100000   || exit 1
get_file model.bin "$EXPECT_BYTES" || exit 1

# ---------------------------------------------------------------- 4. 自检
say "自检（生成一句中文语音并转写）"
if "$PY" "$HERE/selftest.py" --quick; then
  say "完成 ✅  模型目录：$DEST"
else
  say "WARN: 自检未通过，请查看 bash asr/selftest.py 输出"
fi
