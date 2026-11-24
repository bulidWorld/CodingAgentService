#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "项目虚拟环境不存在，请先运行：uv sync" >&2
  exit 1
fi

exec "$PYTHON" -m uvicorn coding_agent_service.server:app \
  --app-dir "$SCRIPT_DIR/src" \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8000}"
