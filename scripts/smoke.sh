#!/usr/bin/env bash
# Thin shell wrapper around scripts/smoke.py — see that file for details.
# Honors $PYTHON if set, otherwise uses the python3 on PATH.
set -euo pipefail
PY="${PYTHON:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PY" "$SCRIPT_DIR/smoke.py" "$@"
