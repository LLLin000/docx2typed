#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV="${VENV:-.venv}"
EDITABLE=0
PRINT_MCP_CONFIG=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [--editable] [--print-mcp-config] [--python PATH] [--venv PATH]

Installs the published docx2typed package into a local virtual environment.
--editable installs the current checkout instead, for source development.
EOF
}

while (($#)); do
  case "$1" in
    --editable)
      EDITABLE=1
      shift
      ;;
    --print-mcp-config)
      PRINT_MCP_CONFIG=1
      shift
      ;;
    --python)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      PYTHON_BIN="$2"
      shift 2
      ;;
    --venv)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      VENV="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  printf 'Python executable not found: %s\n' "$PYTHON_BIN" >&2
  exit 1
}
"$PYTHON_BIN" --version
"$PYTHON_BIN" -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"

if [[ ! -x "$VENV/bin/python" ]]; then
  [[ ! -e "$VENV" ]] || {
    printf 'Existing path is not a Python virtual environment: %s\n' "$VENV" >&2
    exit 1
  }
  "$PYTHON_BIN" -m venv "$VENV"
fi

if (( EDITABLE )); then
  "$VENV/bin/python" -m pip install -e "$ROOT_DIR"
else
  "$VENV/bin/python" -m pip install --upgrade docx2typed
fi
"$VENV/bin/python" -c "import sys; assert sys.version_info >= (3, 11), 'virtual environment uses Python older than 3.11'"

for entry_point in docx2typed docx2typed-mcp docx2typed-review; do
  [[ -x "$VENV/bin/$entry_point" ]] || {
    printf 'Installed entry point is missing: %s/bin/%s\n' "$VENV" "$entry_point" >&2
    exit 1
  }
done
"$VENV/bin/docx2typed" extract --help >/dev/null
"$VENV/bin/docx2typed" review --help >/dev/null
"$VENV/bin/docx2typed-review" --help >/dev/null

version="$("$VENV/bin/python" -c "import importlib.metadata as m; print(m.version('docx2typed'))")"
printf 'Installed docx2typed %s into %s\n' "$version" "$(cd "$VENV" && pwd)"
printf 'Activate with: source "%s/bin/activate"\n' "$VENV"

if (( PRINT_MCP_CONFIG )); then
  "$VENV/bin/python" - "$VENV/bin/python" <<'PY'
import json
import os
import sys

print(json.dumps({
    "mcpServers": {
        "docx2typed": {
            "command": os.path.realpath(sys.argv[1]),
            "args": ["-m", "docx2typed", "mcp"],
        }
    }
}, indent=2))
PY
fi
