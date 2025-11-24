#!/usr/bin/env bash
# Start the FastAPI server in the Nix dev environment

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

nix develop --command bash -c "cd '$SCRIPT_DIR' && PYTHONPATH='$SCRIPT_DIR' uvicorn api.main:app --reload --host 127.0.0.1 --port 8000"

