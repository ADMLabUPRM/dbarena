#!/bin/sh
set -e
cd "$(dirname "$0")/.."
service="${1:-backend}"
docker compose logs -f "$service"
