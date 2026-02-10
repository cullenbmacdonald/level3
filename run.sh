#!/usr/bin/env bash
while true; do
    uv run uvicorn level3.main:app --host 0.0.0.0 --port 8000
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 42 ]; then
        echo "Process exited with code $EXIT_CODE, stopping."
        exit $EXIT_CODE
    fi
    echo "Restart requested, reloading..."
done
