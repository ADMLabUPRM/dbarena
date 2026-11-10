#!/bin/sh
set -e
cd "$(dirname "$0")/.."
docker compose ps
for service in admindb taskdb backend frontend; do
    health=$(docker inspect --format '{{.State.Health.Status}}' "dbarena-$service-1" 2>/dev/null || echo "no healthcheck")
    echo "$service: $health"
done
