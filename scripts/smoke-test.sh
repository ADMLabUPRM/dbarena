#!/bin/sh
set -e
cd "$(dirname "$0")/.."
MSYS_NO_PATHCONV=1 docker compose exec -T backend python /opt/db/smoke_test.py
