#!/bin/sh
set -e
cd "$(dirname "$0")/.."
echo "Resetting seed data (containers and volumes are preserved)..."
MSYS_NO_PATHCONV=1 docker compose restart backend
MSYS_NO_PATHCONV=1 docker compose --profile seed run --rm initializer
MSYS_NO_PATHCONV=1 docker compose --profile seed run --rm olapdb
MSYS_NO_PATHCONV=1 docker compose exec backend python /opt/db/seed_tasks.py
echo "Reset complete."
