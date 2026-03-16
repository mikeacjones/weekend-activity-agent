#!/bin/sh
set -e

# Install any dynamic tool dependencies persisted from previous runs
if [ -f /app/dynamic_tools/requirements.txt ]; then
    echo "Installing dynamic tool dependencies..."
    uv pip install -r /app/dynamic_tools/requirements.txt
fi

case "$1" in
    worker)
        uv run python start.py
        exec uv run python worker.py
        ;;
    start)
        exec uv run python start.py
        ;;
    cli)
        shift
        exec uv run python cli.py "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
