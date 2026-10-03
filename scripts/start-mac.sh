#!/usr/bin/env bash
set -euo pipefail

# Apple Silicon launcher: MLX transcription server + web app.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
require_apple_silicon
mkdir -p "$PROJECT_DIR/logs"

cleanup() {
  if [[ -n "${MLX_PID:-}" ]] && kill -0 "$MLX_PID" 2>/dev/null; then
    kill "$MLX_PID"
    wait "$MLX_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "MLX 전사 서버 시작 중 (Metal)... 로그: logs/mlx.log"
"$PROJECT_DIR/scripts/run-mlx.sh" >"$PROJECT_DIR/logs/mlx.log" 2>&1 &
MLX_PID=$!

for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 "$MOSS_MLX_URL/models" >/dev/null 2>&1; then
    echo "MOSS MLX 모델 준비 완료"
    "$PROJECT_DIR/scripts/run-app.sh"
    exit $?
  fi
  if ! kill -0 "$MLX_PID" 2>/dev/null; then
    echo "MLX 서버 시작 실패. logs/mlx.log의 마지막 내용을 확인합니다." >&2
    tail -n 60 "$PROJECT_DIR/logs/mlx.log" >&2
    exit 1
  fi
  sleep 2
done

echo "2분 안에 모델이 준비되지 않았습니다. logs/mlx.log를 확인하세요." >&2
exit 1
