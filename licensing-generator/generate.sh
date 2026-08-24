#!/usr/bin/env bash
# KIN Mail license generator wrapper — macOS and Linux.
#
# First run creates a self-contained Python virtual environment right inside
# this folder (.venv/) so nothing is installed system-wide and this kit stays
# fully portable — copy the whole folder to another Mac or Linux machine and
# it works the same way there.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "Error: no python3 found on PATH." >&2
  echo "macOS: install with 'brew install python3' (or from python.org)." >&2
  echo "Linux: install with your package manager, e.g. 'sudo apt install python3 python3-venv'." >&2
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "First run: setting up a local Python environment in $VENV ..." >&2
  "$PYTHON_BIN" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet cryptography
  echo "Done. This only happens once." >&2
fi

exec "$VENV/bin/python" "$HERE/generate_license.py" "$@"
