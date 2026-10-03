#!/usr/bin/env bash
set -euo pipefail

# Apple Silicon setup: the web app, MLX runtime, and model all live in this repo.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
require_apple_silicon
UV_BIN="${UV_BIN:-uv}"
if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  echo "uv가 없습니다. uv를 설치한 뒤 다시 실행하세요." >&2
  exit 1
fi

cd "$PROJECT_DIR"
repair_local_runtime "$PROJECT_DIR/.venv"
repair_local_runtime "$MLX_VENV"

echo "[1/3] 웹 앱 환경 구성 -> $PROJECT_DIR/.venv"
UV_PROJECT_ENVIRONMENT="$PROJECT_DIR/.venv" "$UV_BIN" sync --python 3.12

echo "[2/3] MLX 전사 환경 구성 -> $MLX_VENV"
mkdir -p "$(dirname "$MLX_VENV")"
if [[ ! -x "$MLX_VENV/bin/python" ]]; then
  "$UV_BIN" venv --python 3.12 "$MLX_VENV"
fi
"$UV_BIN" pip install --python "$MLX_VENV/bin/python" "mlx-audio==0.5.7" \
  "mlx-lm==0.31.3" jinja2 "fastapi>=0.116,<1" \
  "uvicorn[standard]>=0.35,<1" python-multipart

echo "[3/3] MOSS MLX 모델 다운로드"
"$PROJECT_DIR/scripts/download-mlx-model.sh"

echo
echo "설치 완료. ./scripts/start-mac.sh 로 실행하세요."
