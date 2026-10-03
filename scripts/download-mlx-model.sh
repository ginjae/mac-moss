#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
require_apple_silicon
repair_local_runtime "$MLX_VENV"
# 8-bit is the default: ~1.2 GB on disk, verified on an M1/16 GB.
# Use vanch007/mlx-MOSS-Transcribe-Diarize-4bit (~1.0 GB) if disk is tight,
# or vanch007/mlx-MOSS-Transcribe-Diarize (~1.8 GB) for full precision.
MODEL_ID="${MLX_MODEL_ID:-vanch007/mlx-MOSS-Transcribe-Diarize-8bit}"
MODEL_DIR="$MLX_MODEL_PATH"

if [[ ! -x "$MLX_VENV/bin/python" ]]; then
  echo "MLX 환경이 없습니다. 먼저 ./scripts/setup-mlx.sh 를 실행하세요." >&2
  exit 1
fi

mkdir -p "$MODEL_DIR" "$HF_HUB_CACHE" "$HF_XET_CACHE"
echo "$MODEL_ID -> $MODEL_DIR"
"$MLX_VENV/bin/python" - "$MODEL_ID" "$MODEL_DIR" "$HF_HUB_CACHE" <<'PY'
import sys
from huggingface_hub import snapshot_download

print(snapshot_download(sys.argv[1], local_dir=sys.argv[2], cache_dir=sys.argv[3]))
PY
