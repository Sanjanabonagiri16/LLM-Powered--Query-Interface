#!/usr/bin/env bash
set -euo pipefail

# Build the integrated SQLite DB on container start.
python3 scripts/ingest.py

# Start FastAPI on platform-provided port.
exec uvicorn backend.app:app --host 0.0.0.0 --port "${PORT:-8000}"
