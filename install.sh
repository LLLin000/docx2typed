#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV="${VENV:-.venv}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

"$PYTHON_BIN" --version
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi

if [[ "${1:-}" == "--editable" ]]; then
  "$VENV/bin/python" -m pip install -e .
else
  "$VENV/bin/python" -m pip install .
fi
"$VENV/bin/docx2typed" extract --help >/dev/null

printf 'Installed docx2typed into %s\n' "$(cd "$VENV" && pwd)"
printf 'Activate with: source %s/bin/activate\n' "$VENV"
