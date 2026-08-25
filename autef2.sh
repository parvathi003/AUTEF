#!/usr/bin/env bash
# Run AUTEF v2 without installing it. See autef2.bat for the Windows twin.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$HERE/src:${PYTHONPATH:-}"

if [ -x "$HERE/.venv/bin/python" ]; then
    PY="$HERE/.venv/bin/python"
elif [ -x "$HERE/.venv/Scripts/python.exe" ]; then
    PY="$HERE/.venv/Scripts/python.exe"
else
    PY="python3"
fi

exec "$PY" -m autef2 "$@"
