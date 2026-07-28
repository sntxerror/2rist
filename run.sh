#!/usr/bin/env bash
# Запуск: ./run.sh [порт]
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/uvicorn app.api:app --host 127.0.0.1 --port "${1:-8080}" --reload
