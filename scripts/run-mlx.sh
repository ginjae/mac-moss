#!/usr/bin/env bash
set -euo pipefail

# Apple Silicon transcription server using MLX and Metal.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
require_apple_silicon
repair_local_runtime "$MLX_VENV"
if [[ ! -x "$MLX_VENV/bin/python" ]]; then
  echo "MLX 환경이 없습니다. ./scripts/setup-mlx.sh 를 먼저 실행하세요." >&2
  exit 1
fi
if [[ ! -f "$MLX_MODEL_PATH/config.json" ]]; then
  echo "모델이 없습니다: $MLX_MODEL_PATH" >&2
  echo "./scripts/download-mlx-model.sh 를 실행하세요." >&2
  exit 1
fi

export MLX_MODEL_PATH
export MOSS_MODEL_NAME="${MOSS_MODEL_NAME:-moss-mtd}"

cd "$PROJECT_DIR"
exec "$MLX_VENV/bin/python" -m uvicorn src.mlx_server:app \
  --host 127.0.0.1 \
  --port "$MOSS_MLX_PORT" \
  --workers 1
