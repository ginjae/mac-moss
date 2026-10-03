#!/usr/bin/env bash

# This file is sourced by the runnable scripts. Values in MOSS_ENV_FILE take
# precedence over inherited variables so one deployment has a single source of
# truth. Set MOSS_ENV_FILE=/dev/null to use command-line variables only.
resolve_project_path() {
  local path="$1" default_path="${2:-}" previous
  if [[ -n "$default_path" ]]; then
    while IFS= read -r previous; do
      if [[ -n "$previous" && "$path" == "$previous/$default_path" ]]; then
        path="$default_path"
        break
      fi
    done <<< "${MOSS_PREVIOUS_PROJECT_DIRS:-}"
  fi
  case "$path" in
    /*) printf '%s\n' "$path" ;;
    *) printf '%s/%s\n' "$PROJECT_DIR" "$path" ;;
  esac
}

# A moved checkout still carries its previous location in the local venvs.
# Only exact repo defaults use this hint; custom absolute overrides stay intact.
find_previous_project_dirs() {
  local runtime hint previous runtime_root project_root
  project_root="$(cd "$PROJECT_DIR" && pwd -P)"
  for runtime in "$PROJECT_DIR/.venv" "$PROJECT_DIR/.venv-mlx"; do
    [[ -d "$runtime" ]] || continue
    runtime_root="$(cd "$runtime" && pwd -P)"
    case "$runtime_root/" in
      "$project_root/"*) ;;
      *) continue ;;
    esac
    hint=""
    if [[ -f "$runtime/pyvenv.cfg" ]]; then
      hint="$(sed -n 's/^home = //p' "$runtime/pyvenv.cfg")"
      sed -n 's/^moss_previous_project_dir = \(\/.*\)$/\1/p' "$runtime/pyvenv.cfg"
    fi
    if [[ "$hint" != /*/.cache/uv/python/* && -L "$runtime/bin/python" ]]; then
      hint="$(readlink "$runtime/bin/python")"
    fi
    if [[ "$hint" == /*/.cache/uv/python/* ]]; then
      previous="${hint%%/.cache/uv/python/*}"
      if [[ "$previous" != "$PROJECT_DIR" ]]; then
        printf '%s\n' "$previous"
      fi
    fi
  done
}
MOSS_PREVIOUS_PROJECT_DIRS="$(find_previous_project_dirs)"

MOSS_ENV_FILE="$(resolve_project_path "${MOSS_ENV_FILE:-$PROJECT_DIR/.env}" .env)"
if [[ -f "$MOSS_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$MOSS_ENV_FILE"
  set +a
fi

# Keep both runtimes, downloaded models, and package/download caches in this
# checkout. Relative overrides are always resolved from the repo root.
export MLX_VENV="$(resolve_project_path "${MLX_VENV:-.venv-mlx}" .venv-mlx)"
export MLX_MODEL_PATH="$(resolve_project_path "${MLX_MODEL_PATH:-models/moss-mlx-8bit}" models/moss-mlx-8bit)"
export MOSS_MLX_PORT="${MOSS_MLX_PORT:-8001}"
export MOSS_MLX_URL="${MOSS_MLX_URL:-http://127.0.0.1:$MOSS_MLX_PORT/v1}"
export MOSS_MLX_URL="${MOSS_MLX_URL%/}"
export HF_HOME="$(resolve_project_path "${HF_HOME:-.cache/huggingface}" .cache/huggingface)"
export HF_HUB_CACHE="$(resolve_project_path "${HF_HUB_CACHE:-$HF_HOME/hub}" .cache/huggingface/hub)"
export HF_XET_CACHE="$(resolve_project_path "${HF_XET_CACHE:-$HF_HOME/xet}" .cache/huggingface/xet)"
export UV_CACHE_DIR="$(resolve_project_path "${UV_CACHE_DIR:-.cache/uv}" .cache/uv)"
export UV_PYTHON_INSTALL_DIR="$(resolve_project_path "${UV_PYTHON_INSTALL_DIR:-.cache/uv/python}" .cache/uv/python)"

repair_local_runtime() {
  local runtime="$1" project_root base_python
  [[ -f "$runtime/pyvenv.cfg" && -d "$runtime/bin" ]] || return 0
  project_root="$(cd "$PROJECT_DIR" && pwd -P)"
  runtime="$(cd "$runtime" && pwd -P)"
  case "$runtime/" in
    "$project_root/"*) ;;
    *) return 0 ;;
  esac

  # Bootstrap with the relocatable base interpreter, even when bin/python is a
  # broken absolute symlink. Isolated mode ignores inherited Python settings.
  for base_python in "$project_root"/.cache/uv/python/*/bin/python3.12; do
    [[ -x "$base_python" ]] || continue
    "$base_python" -I -B - "$project_root" "$runtime" <<'PY'
import os
from pathlib import Path
import re
import sys

root = Path(sys.argv[1]).resolve()
runtime = Path(sys.argv[2]).resolve()
base_python = Path(sys.executable).resolve()
if not runtime.is_relative_to(root) or not base_python.is_relative_to(root):
    sys.exit(0)

config = runtime / "pyvenv.cfg"
config_text = config.read_text()
match = re.search(r"^home\s*=\s*(.+)$", config_text, re.MULTILINE)
home = match.group(1).strip() if match else ""
python_link = runtime / "bin" / "python"
hints = [home]
if python_link.is_symlink():
    hints.append(os.readlink(python_link))

old_roots = {
    value for value in re.findall(r"^moss_previous_project_dir\s*=\s*(.+)$", config_text, re.MULTILINE)
    if Path(value).is_absolute()
}
old_roots.discard(str(root))
for hint in hints:
    if Path(hint).is_absolute() and "/.cache/uv/python/" in hint:
        previous = hint.split("/.cache/uv/python/", 1)[0]
        if previous and Path(previous) != root:
            old_roots.add(previous)

# Keep the exact cached interpreter when several Python 3.12 builds are present.
candidates = [Path(home) / "python3.12"] if home else []
for previous in old_roots:
    if home.startswith(previous + "/"):
        candidates.append(root / home[len(previous) + 1:] / "python3.12")
for candidate in candidates:
    if candidate.is_file() and candidate.resolve().is_relative_to(root / ".cache" / "uv" / "python"):
        base_python = candidate.resolve()
        break

old_prefixes = [str(Path(previous) / runtime.relative_to(root)) for previous in old_roots]
changed = False
for path in (runtime / "bin").iterdir():
    if path.is_symlink() or not path.is_file():
        continue
    original = path.read_bytes()
    if b"\0" in original:
        continue
    updated = original
    for prefix in old_prefixes:
        updated = updated.replace(prefix.encode(), str(runtime).encode())
    if updated != original:
        path.write_bytes(updated)
        changed = True

config_lines = []
for line in config_text.splitlines(keepends=True):
    if line.startswith("moss_previous_project_dir = "):
        continue
    for previous in sorted(old_roots, key=len, reverse=True):
        line = line.replace(previous + "/", str(root) + "/")
    config_lines.append(line)
updated_config = re.sub(r"^home\s*=.*$", lambda _: f"home = {base_python.parent}", "".join(config_lines), flags=re.MULTILINE)
for previous in sorted(old_roots):
    record = f"moss_previous_project_dir = {previous}"
    updated_config = updated_config.rstrip("\n") + "\n" + record + "\n"
if updated_config != config_text:
    config.write_text(updated_config)
    changed = True

if python_link.is_symlink():
    target = os.path.relpath(base_python, python_link.parent)
    if os.readlink(python_link) != target:
        temporary = python_link.with_name(f".python-relocate-{os.getpid()}")
        try:
            temporary.symlink_to(target)
            temporary.replace(python_link)
        finally:
            temporary.unlink(missing_ok=True)
        changed = True

if changed:
    print(f"실행 환경 경로 복구: {runtime}")
PY
    return $?
  done
}

require_apple_silicon() {
  if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    echo "Mac MOSS는 Apple Silicon(macOS arm64) 전용입니다." >&2
    return 1
  fi
}
