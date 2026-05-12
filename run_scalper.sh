#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/home/intimatik/dev/scalper"
PYTHON_BIN="/home/intimatik/.pyenv/shims/python"
OTEL_INSTRUMENT_BIN="/home/intimatik/.pyenv/shims/opentelemetry-instrument"
OTEL_WRAPPER="/home/intimatik/.local/bin/otel-python-cron.sh"
LOCK_PATH="/tmp/scalper.lock"

cd "$BASE_DIR"
exec >> "$BASE_DIR/cron.log" 2>&1

/home/intimatik/.local/bin/start-alloy.sh

exec /usr/bin/flock -n "$LOCK_PATH" \
  "$OTEL_WRAPPER" scalper "$OTEL_INSTRUMENT_BIN" "$PYTHON_BIN" "$BASE_DIR/scalper.py" --publish-pages
