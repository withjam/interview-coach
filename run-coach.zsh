#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR=${0:a:h}
cd "$SCRIPT_DIR"

# Load project-local environment variables if .env exists
if [[ -f .env ]]; then
  set -o allexport
  source .env
  set +o allexport
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: uv is not installed or not on PATH." >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "Creating uv virtual environment (.venv)..."
  uv venv
fi

echo "Installing/updating dependencies with uv..."
uv pip install -r requirements.txt

echo "Starting coach.py via uv..."
uv run coach.py
