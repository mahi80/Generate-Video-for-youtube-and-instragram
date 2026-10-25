#!/usr/bin/env sh
# Start the HTMX WebUI (same features as webui.sh, without Streamlit).
# Options are passed through, e.g. ./webui-htmx.sh --port 8520 --host 0.0.0.0

CURRENT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$CURRENT_DIR" || exit 1
export PYTHONPATH="$CURRENT_DIR${PYTHONPATH:+:$PYTHONPATH}"

if [ -x "$CURRENT_DIR/.venv/bin/python" ]; then
  exec "$CURRENT_DIR/.venv/bin/python" -m webui_htmx "$@"
elif command -v uv >/dev/null 2>&1; then
  exec uv run python -m webui_htmx "$@"
else
  echo "***** Neither the project Python (.venv) nor uv was found. Run 'uv sync --frozen' first. *****"
  exit 1
fi
