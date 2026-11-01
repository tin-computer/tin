#!/usr/bin/env bash
set -euo pipefail
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/tin-lite/diagram/browsers}"
exec /opt/tin-lite/studio-venv/bin/python /opt/tin-lite/studio/tin_launch.py "$@"
