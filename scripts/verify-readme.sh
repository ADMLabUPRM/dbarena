#!/bin/sh
# Live verification of the README run instructions, stage by stage.
# Non-destructive: starts the stack and applies idempotent seeds; removes no data and stops nothing.
# Optional: DBARENA_VERIFY_EMBED_CPU=1 also starts a one-off CPU-forced embed-server (slow).
set -u
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); echo "[PASS] $1"; }
ko() { FAIL=$((FAIL + 1)); echo "[FAIL] $1"; }

env_or() {
    value=$(grep -E "^$1=" .env 2>/dev/null | head -n 1 | cut -d= -f2-)
    echo "${value:-$2}"
}

BASE="-f docker-compose.yml"
OVERLAY="-f docker-compose.yml -f docker-compose.access.yml"
GPU_OVERLAY="-f docker-compose.yml -f docker-compose.embed-gpu.yml"

ADMINDB_USER=$(env_or ADMINDB_USER dbarena)
ADMINDB_DB=$(env_or ADMINDB_DB dbarena)
TASKDB_USER=$(env_or TASKDB_USER dbarena)
TASKDB_DB=$(env_or TASKDB_DB taskdb)
RAGDB_USER=$(env_or RAGDB_USER dbarena)
RAGDB_DB=$(env_or RAGDB_DB ragdb)
FRONTEND_PORT=$(env_or FRONTEND_PORT 8080)
BACKEND_PORT=$(env_or BACKEND_PORT 8000)
MCP_PORT=$(env_or MCP_PORT 8090)
ADMINDB_PORT=$(env_or ADMINDB_PORT 5433)
TASKDB_PORT=$(env_or TASKDB_PORT 5434)
RAGDB_PORT=$(env_or RAGDB_PORT 5435)
EMBEDDING_DIM=$(env_or EMBEDDING_DIM 1024)

echo "== Stage 0: preconditions =="
command -v docker >/dev/null 2>&1 && ok "docker CLI available" || ko "docker CLI not available"
command -v make >/dev/null 2>&1 && ok "make available" || ko "make not available"
if [ -f .env ]; then ok ".env present"; else cp .env.example .env && ok ".env missing, copied from .env.example"; fi
for f in docker-compose.yml docker-compose.access.yml docker-compose.embed-gpu.yml Makefile .env.example; do
    [ -f "$f" ] && ok "file present: $f" || ko "file missing: $f"
done
if nvidia-smi >/dev/null 2>&1; then
    ok "NVIDIA GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -n 1)"
    HOST_GPU=1
else
    echo "[INFO] no NVIDIA GPU on host; the embedding stage will use the CPU variant"
    HOST_GPU=0
fi

echo "== Stage 1: compose configuration validation =="
docker compose $BASE config -q && ok "base compose file is valid" || ko "base compose file is invalid"
docker compose $OVERLAY config -q && ok "base + access overlay is valid" || ko "base + access overlay is invalid"
docker compose $GPU_OVERLAY --profile embed config -q && ok "base + GPU overlay (embed profile) is valid" \
    || ko "base + GPU overlay (embed profile) is invalid"

echo "== Stage 2: stack up with the access overlay =="
if docker compose $OVERLAY up -d; then
    ok "documented overlay start command succeeded"
else
    ko "documented overlay start command failed"
fi

wait_healthy() {
    service=$1
    limit=$2
    waited=0
    while :; do
        cid=$(docker compose $OVERLAY ps -q "$service" 2>/dev/null)
        if [ -n "$cid" ]; then
            state=$(docker inspect -f "{{.State.Health.Status}}" "$cid" 2>/dev/null || echo none)
            case "$state" in
                healthy) return 0 ;;
                none|running) ;;
            esac
        fi
        if [ "$waited" -ge "$limit" ]; then return 1; fi
        sleep 3
        waited=$((waited + 3))
    done
}

for svc in admindb taskdb ragdb judge backend mcp frontend; do
    if wait_healthy "$svc" 180; then
        ok "service healthy: $svc"
    else
        ko "service not healthy after timeout: $svc"
    fi
done

echo "== Stage 3: host endpoints (access overlay) =="
code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${BACKEND_PORT}/api/health")
[ "$code" = "200" ] && ok "backend API health on :${BACKEND_PORT}" || ko "backend API health on :${BACKEND_PORT} (HTTP $code)"
code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${FRONTEND_PORT}/")
[ "$code" = "200" ] && ok "admin GUI on :${FRONTEND_PORT}" || ko "admin GUI on :${FRONTEND_PORT} (HTTP $code)"
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:${MCP_PORT}/mcp")
[ "$code" != "000" ] && ok "MCP endpoint reachable on :${MCP_PORT} (HTTP $code)" \
    || ko "MCP endpoint unreachable on :${MCP_PORT}"
for pair in "admindb:${ADMINDB_PORT}" "taskdb:${TASKDB_PORT}"; do
    svc=${pair%%:*}
    port=${pair##*:}
    binding=$(docker inspect -f \
        "{{range \$p, \$b := .NetworkSettings.Ports}}{{range \$b}}{{.HostIP}}:{{.HostPort}} {{end}}{{end}}" \
        "dbarena-${svc}-1" 2>/dev/null | tr -d "\r")
    case "$binding" in
        *":${port} "*) ok "${svc} published on ${binding%% }" ;;
        *) ko "${svc} not published on 127.0.0.1:${port} (bindings: ${binding:-none})" ;;
    esac
done
ragdb_binding=$(docker inspect -f \
    "{{range \$p, \$b := .NetworkSettings.Ports}}{{range \$b}}{{.HostIP}}:{{.HostPort}} {{end}}{{end}}" \
    "dbarena-ragdb-1" 2>/dev/null | tr -d "\r")
case "$ragdb_binding" in
    *":${RAGDB_PORT} "*) ok "ragdb published on ${ragdb_binding%% }" ;;
    *) echo "[INFO] ragdb declares ${RAGDB_PORT}/tcp but the binding is inert while ragdb sits only on the" \
        "internal network (known finding); reach ragdb through the backend/GUI, not from the host" ;;
esac

echo "== Stage 4: internal databases =="
if docker compose $OVERLAY exec -T admindb pg_isready -U "$ADMINDB_USER" -d "$ADMINDB_DB" >/dev/null 2>&1; then
    ok "admindb accepts connections"
else
    ko "admindb does not accept connections"
fi
count=$(docker compose $OVERLAY exec -T admindb psql -U "$ADMINDB_USER" -d "$ADMINDB_DB" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='tasks'" \
    2>/dev/null | tr -d "[:space:]")
[ "${count:-0}" -ge 1 ] && ok "admindb has the task catalog table" || ko "admindb task catalog table missing"

count=$(docker compose $OVERLAY exec -T taskdb psql -U "$TASKDB_USER" -d "$TASKDB_DB" -tAc \
    "SELECT count(*) FROM warehouse" 2>/dev/null | tr -d "[:space:]")
if [ "${count:-0}" = "1" ]; then
    ok "taskdb TPC-C fixture seeded (1 warehouse)"
else
    ko "taskdb TPC-C fixture unexpected warehouse count: ${count:-none} (run: make seed)"
fi

count=$(docker compose $OVERLAY exec -T ragdb psql -U "$RAGDB_USER" -d "$RAGDB_DB" -tAc \
    "SELECT count(*) FROM documents" 2>/dev/null | tr -d "[:space:]")
[ "${count:-0}" -ge 1 ] && ok "ragdb corpus loaded (${count:-0} documents)" \
    || ko "ragdb corpus empty (run: make seed-ragdb)"
count=$(docker compose $OVERLAY exec -T ragdb psql -U "$RAGDB_USER" -d "$RAGDB_DB" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='embeddings'" \
    2>/dev/null | tr -d "[:space:]")
[ "${count:-0}" = "1" ] && ok "ragdb embeddings table exists" || ko "ragdb embeddings table missing"

[ -f data/olapdb/olap-tpch.duckdb ] && ok "olapdb DuckDB fixture file present" \
    || ko "olapdb DuckDB fixture file missing (run: make seed-olap)"
[ -f data/dsdb/tpcds.duckdb ] && ok "dsdb DuckDB fixture file present" \
    || ko "dsdb DuckDB fixture file missing (run: make seed-ds)"

echo "== Stage 5: seed idempotency =="
if make seed >/dev/null 2>&1; then
    ok "make seed ran"
else
    ko "make seed failed"
fi
count=$(docker compose $OVERLAY exec -T taskdb psql -U "$TASKDB_USER" -d "$TASKDB_DB" -tAc \
    "SELECT count(*) FROM warehouse" 2>/dev/null | tr -d "[:space:]")
if [ "${count:-0}" = "1" ]; then
    ok "make seed is idempotent (still 1 warehouse after re-run)"
else
    ko "make seed re-run changed the fixture (warehouse count: ${count:-none})"
fi
for target in seed-olap seed-ds seed-ragdb tasks; do
    if make "$target" >/dev/null 2>&1; then
        ok "make $target ran"
    else
        ko "make $target failed"
    fi
done

echo "== Stage 6: local embedding model (GPU when available, CPU otherwise) =="
embed_variant=none
if [ "$HOST_GPU" = "1" ]; then
    if docker compose $GPU_OVERLAY --profile embed up -d embed-server; then
        embed_variant=gpu
        ok "embed-server started with the GPU overlay"
    else
        ko "GPU overlay start failed; falling back to CPU as documented"
    fi
fi
if [ "$embed_variant" = "none" ]; then
    if docker compose $BASE --profile embed up -d embed-server; then
        embed_variant=cpu
        ok "embed-server started without GPU access (CPU variant)"
    else
        ko "embed-server failed to start"
    fi
fi
if wait_embed() {
        waited=0
        while :; do
            if docker compose $OVERLAY exec -T backend \
                python -c "import urllib.request; urllib.request.urlopen('http://embed-server:30000/v1/models', timeout=5)" \
                >/dev/null 2>&1; then
                return 0
            fi
            if [ "$waited" -ge "$1" ]; then return 1; fi
            sleep 5
            waited=$((waited + 5))
        done
    }; then
    ok "embed-server reachable from the backend at http://embed-server:30000 (variant: $embed_variant)"
    embed_probe=0
    attempt=0
    while [ "$attempt" -lt 3 ]; do
        if docker compose $OVERLAY exec -T backend python - <<'PYEOF'
import json, os, urllib.request

base = os.environ["EMBEDDING_BASE_URL"].rstrip("/")
model = os.environ["EMBEDDING_MODEL"]
dim = int(os.environ["EMBEDDING_DIM"])
body = json.dumps({"model": model, "input": ["dbarena verify"]}).encode()
request = urllib.request.Request(
    base + "/embeddings", data=body, headers={"Content-Type": "application/json"}
)
with urllib.request.urlopen(request, timeout=120) as response:
    payload = json.load(response)
vector = payload["data"][0]["embedding"]
assert len(vector) == dim, f"expected {dim} dims, got {len(vector)}"
print(f"EMBED_PROBE_OK dims={len(vector)} model={model}")
PYEOF
        then
            ok "embeddings endpoint returns $EMBEDDING_DIM-dimensional vectors"
            embed_probe=1
            break
        fi
        attempt=$((attempt + 1))
        echo "[INFO] embeddings probe attempt $attempt failed; retrying (warm-up can be flaky)"
        sleep 15
    done
    [ "$embed_probe" = "1" ] || ko "embeddings endpoint probe failed after retries"
else
    ko "embed-server did not become reachable from the backend within the timeout"
fi

if [ "${DBARENA_VERIFY_EMBED_CPU:-0}" = "1" ] && [ "$embed_variant" = "gpu" ]; then
    echo "== Stage 6b: forced-CPU embed-server (one-off) =="
    cid=$(docker compose $BASE --profile embed run -d --rm --name dbarena-verify-embed-cpu \
        -e SGLANG_DEVICE=cpu embed-server 2>/dev/null || true)
    if [ -n "$cid" ]; then
        cpu_ready=0
        waited=0
        while [ "$waited" -lt 600 ]; do
            if docker exec "$cid" \
                python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:30000/v1/models', timeout=5)" \
                >/dev/null 2>&1; then
                cpu_ready=1
                break
            fi
            sleep 5
            waited=$((waited + 5))
        done
        [ "$cpu_ready" = "1" ] && ok "SGLANG_DEVICE=cpu forces the CPU variant successfully" \
            || ko "CPU-forced embed-server did not become ready"
        docker rm -f dbarena-verify-embed-cpu >/dev/null 2>&1 || true
    else
        ko "could not start the CPU-forced one-off embed-server"
    fi
fi

echo "== Stage 7: smoke test =="
smoke_log=$(mktemp)
smoke_ok=0
for attempt in 1 2; do
    if make smoke >"$smoke_log" 2>&1; then
        smoke_ok=1
        break
    fi
    echo "[INFO] make smoke attempt $attempt failed:"
    tail -n 12 "$smoke_log"
    if [ "$attempt" = "1" ]; then
        echo "[INFO] retrying make smoke once (transient infrastructure failures happen)"
        sleep 5
    fi
done
rm -f "$smoke_log"
[ "$smoke_ok" = "1" ] && ok "make smoke passed" || ko "make smoke failed after retry"

echo
echo "== Summary: $PASS passed, $FAIL failed =="
echo "The stack is left running. Stop it with: docker compose -f docker-compose.yml -f docker-compose.access.yml down"
[ "$FAIL" = "0" ]
