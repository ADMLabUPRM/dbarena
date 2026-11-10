# DBArena 

A WebArena-style benchmark environment for database tasks: a Gymnasium environment over SQL databases (OLTP, analytics, OLAP, RAG), a task catalog with exact oracles and LLM-judged tasks, a standard observation format (`dbarena-observation/v1`), an admin GUI for task parametrization and manual stepping, an MCP server for AI agents, and a fully dockerized stack.

This README is the complete run guide. New to the project? Follow **Prerequisites → Recommended hardware → Quickstart** — everything after that is reference material.

## Prerequisites

- **Docker Engine + Docker Compose v2** — the only hard requirement. No host Python, pip, Node, or virtualenv is needed to run, seed, or smoke-test the stack.
- **Disk:** see [Recommended hardware](#recommended-hardware) below.
- **Optional — NVIDIA GPU** for accelerated embeddings:
  - Linux: NVIDIA driver + `nvidia-container-toolkit` (the GPU runtime must let `--gpus all` work).
  - Windows: Docker Desktop with WSL2 backend + NVIDIA driver with WSL2 GPU support.
  - Without a GPU everything still runs; see [Local embedding model](#local-embedding-model-gpu--cpu) for the CPU story and the external-endpoint fallback.
- **Optional — `make`** (`make` targets wrap the `docker compose` commands below). On Windows without `make`, use the compose equivalents shown alongside each step.

## Recommended hardware

| Profile | What you get | Disk | RAM | GPU |
| --- | --- | --- | --- | --- |
| **Core stack** (no embeddings) | Everything runs; RAG tasks and the two RAG smoke checks stay gated | ~3 GB | 8 GB is enough | none |
| **+ in-stack embeddings** (recommended) | Full stack: RAG tasks unlocked, `make smoke` fully green | +≈40 GB (the SGLang image) + ~2 GB model load | 16 GB comfortable | any CUDA-capable GPU supported by PyTorch — verified on an 8 GB laptop GPU (RTX 4070) |

- The ≈40 GB line item is the SGLang image, built or pulled on the first embed-server start; the PostgreSQL base images are shared and small.
- Without a GPU the CPU embedding variant works but is slow — prefer an external endpoint (see [Local embedding model](#local-embedding-model-gpu--cpu)) for sustained CPU use.
- Storage grows with use (episode history, embeddings); the DuckDB fixtures add ~20 MB on the host.

## Quickstart

All commands run from the repository root.

At a glance (details below; without a GPU, drop the GPU overlay from the embed-server command — see step 6):

```bash
Copy-Item .env.example .env     # bash: cp .env.example .env — then edit passwords
make build
docker compose -f docker-compose.yml -f docker-compose.access.yml up -d
make seed-tasks
make smoke                      # only the 2 RAG checks fail at this point
docker compose -f docker-compose.yml -f docker-compose.embed-gpu.yml --profile embed up -d embed-server
make reembed                    # waits until the embedding lifecycle is ready
make smoke                      # now SMOKE PASSED
```

### 1. Configure

```bash
cp .env.example .env
```

```powershell
Copy-Item .env.example .env
```

Edit `.env`: change the database passwords (`ADMINDB_PASSWORD`, `TASKDB_PASSWORD`, `RAGDB_PASSWORD`) and, if the defaults collide with other services, the overlay ports (`FRONTEND_PORT=8080`, `BACKEND_PORT=8000`, `MCP_PORT=8090`, `ADMINDB_PORT=5433`, `TASKDB_PORT=5434`) and `RAGDB_PORT=5435`. Secrets stay in `.env` — never commit it.

### 2. Build

```bash
make build
```

```bash
docker compose build
```

This builds the core images only. The `embed-server` image (multi-GB) is built or pulled on its first start in [Local embedding model](#local-embedding-model-gpu--cpu).

### 3. Start — always with the access overlay

```bash
docker compose -f docker-compose.yml -f docker-compose.access.yml up -d
```

> **Pitfall:** the base `docker-compose.yml` publishes **no ports** (the `dbarena` network is internal). Starting with the base file only — including `make up` — produces a healthy-looking stack with nothing reachable from the host. Always pass both `-f` flags (the overlay binds everything to `127.0.0.1` only), and after any lifecycle command confirm the `PORTS` column is populated: `docker compose -f docker-compose.yml -f docker-compose.access.yml ps`.

### 4. Seed the databases

Each seed target is idempotent: re-running it against already-seeded data changes nothing.

```bash
make seed-tasks
```

`make seed-tasks` chains the five steps below (equivalents: `docker compose --profile seed run --rm <service>` for the first four, then `docker compose exec backend python /opt/db/seed_tasks.py`):

| Target | Service | Loads |
| --- | --- | --- |
| `make seed` | `initializer` | TPC-C 5.11 (1 warehouse) into `taskdb`; drops any legacy TPC-H tables |
| `make seed-olap` | `olapdb` | TPC-H SF 0.01 fixture file into `./data/olapdb/olap-tpch.duckdb` |
| `make seed-ds` | `dsdb` | TPC-DS SF 0.01 retail fixture file into `./data/dsdb/tpcds.duckdb` |
| `make seed-ragdb` | `ragdb-fixture` | incident-report corpus into `ragdb` with an empty `embeddings` table — no embedding endpoint contacted |
| `make tasks` | (backend exec) | the starter task catalog: OLTP, analytics (DuckDB), OLAP (TPC-DS), and RAG tasks with trusted oracles |

### 5. Smoke-test

```bash
make smoke
```

Verifies stack health, task CRUD through the API, seeded TPC-C queries, one playground round trip, OLAP and RAG round trips, CC episodes on the DuckDB fixture (including rollback and the isolation-emulation variant), and persisted episode history. Everything passes except the two RAG checks until the embedding lifecycle reports `ready` — step 6 covers that.

### 6. Optional but recommended — RAG embeddings

Nothing else depends on this step; it unlocks the two RAG smoke checks and every `category: RAG` task:

1. Start the embedding server and wait for `healthy` (the first start builds a multi-GB image and loads the model): with an NVIDIA GPU, `docker compose -f docker-compose.yml -f docker-compose.embed-gpu.yml --profile embed up -d embed-server`; without one, drop the GPU overlay — `docker compose -f docker-compose.yml --profile embed up -d embed-server`.
2. Run **`make reembed`** — it configures the endpoint from the `EMBEDDING_*` variables in `.env`, starts the re-embed job, and polls until the lifecycle reports `ready` (the admin GUI modal's **Save and re-embed** is the browser equivalent).
3. Re-run `make smoke` — now `SMOKE PASSED` includes the RAG checks.

CPU variant, external endpoints, and the full lifecycle: [Local embedding model](#local-embedding-model-gpu--cpu).

### 7. Open it

| What | Where |
| --- | --- |
| Admin GUI (playground, task editor) | `http://127.0.0.1:8080` |
| API health / docs | `http://127.0.0.1:8000/api/health` · `http://127.0.0.1:8000/api/docs` |
| MCP endpoint for AI agents | `http://127.0.0.1:8090/mcp` |
| `admindb` / `taskdb` (psql) | `127.0.0.1:5433` / `127.0.0.1:5434` |

Configure embeddings from the GUI (an alert appears while the status is `unconfigured` / `missing` / `stale`): enter the endpoint URL, model, and dimension, then **Save and re-embed**. RAG tasks stay locked until the lifecycle reports `ready` — see the next section. From the terminal, `make reembed` does the same thing: it configures the endpoint from the `EMBEDDING_*` variables in `.env`, starts the re-embed job, and waits until the lifecycle reports `ready`.

## What's in the stack

Long-running services (all on the internal `dbarena` network, managed by `docker compose`):

| Service | Tech | Role | Host port |
| --- | --- | --- | --- |
| `backend` | FastAPI (Python 3.12) | Admin API + Gymnasium environment; owns all databases | `127.0.0.1:8000` via access overlay |
| `frontend` | React + nginx | Admin GUI (proxies `/api` to the backend) | `127.0.0.1:8080` via access overlay |
| `mcp` | Python MCP server | Streamable-HTTP MCP endpoint for AI clients | `127.0.0.1:8090` via access overlay |
| `judge` | FastAPI | LLM answer judge for judged RAG tasks (`:9100`, internal only); its LLM endpoint is configured via `JUDGE_LLM_*` in `.env` or from the admin GUI (stored values take precedence) | none |
| `embed-server` | SGLang (embed profile) | Local embedding model, OpenAI-compatible API (`:30000`, internal only) | none |
| `admindb` | PostgreSQL 17 (`postgres:17.11-alpine`) | Task catalog, sessions, GUI state, judge/embedding config | `127.0.0.1:5433` via access overlay |
| `taskdb` | PostgreSQL 17 (`postgres:17.11-alpine`) | OLTP playground database (TPC-C fixture) | `127.0.0.1:5434` via access overlay |
| `ragdb` | PostgreSQL 17 + pgvector (`pgvector/pgvector:pg17`) | RAG corpus + embeddings | declares `5435` — inert, see [Databases](#databases) |

One-shot initializer services (seed profile, run by the `make seed*` targets): `initializer` (TPC-C into taskdb), `olapdb` (TPC-H DuckDB file), `dsdb` (TPC-DS DuckDB file), `ragdb-fixture` (incident-report corpus into ragdb).

Storage:

| Store | Tech | Fixture | Storage | Host access |
| --- | --- | --- | --- | --- |
| `admindb` | PostgreSQL 17 | task catalog + config (DDL in `db/init`) | named volume `admindb_data` | `127.0.0.1:5433` (overlay) |
| `taskdb` | PostgreSQL 17 | TPC-C 5.11, 1 warehouse, 9 tables | named volume `taskdb_data` | `127.0.0.1:5434` (overlay) |
| `ragdb` | PostgreSQL 17 + pgvector | authored incident-report corpus, empty `embeddings` table | named volume `ragdb_data` | **none** (internal only) |
| `olapdb` | DuckDB **file** | TPC-H SF 0.01 | bind mount `./data/olapdb/olap-tpch.duckdb` → backend `/data` | none (file) |
| `dsdb` | DuckDB **file** | TPC-DS SF 0.01 retail | bind mount `./data/dsdb/tpcds.duckdb` → backend `/ds` | none (file) |

## Local embedding model (GPU / CPU)

> **Optional** — only `category: RAG` tasks (and the two RAG smoke checks) depend on this; the startup commands live in Quickstart step 6.

The stack ships an in-stack embedding server (compose service `embed-server`, profile `embed`): SGLang (`lmsysorg/sglang:v0.5.21`) serving **`BAAI/bge-large-en-v1.5`** — a 335M-parameter encoder producing **1024-dimensional** vectors — behind the OpenAI-compatible `/v1/embeddings` API on port `30000`, reachable from the stack as `http://embed-server:30000/v1` (internal network only). Model weights are baked into the image at build time (`HF_HUB_OFFLINE=1`), so the runtime stack never contacts Hugging Face.

### How GPU/CPU detection works

The entrypoint (`db/embed-server/entrypoint.py`) checks the device at startup (`nvidia-smi` + `torch.cuda.is_available`) and launches SGLang on CUDA (attention backend `triton`) or CPU (attention backend `torch_native` — triton has no CPU driver); if a CUDA launch fails, it retries once on CPU. Force a device with `SGLANG_DEVICE=cpu` or `SGLANG_DEVICE=cuda`; tune GPU memory with `SGLANG_MEM_FRACTION` (default `0.5`).

Hardware: the 335M-parameter model is small — any CUDA-capable GPU supported by PyTorch works. CPU inference works but is slow; for sustained CPU workloads prefer an external endpoint. First minutes can be flaky on some laptop GPUs (occasional connection refusals right after startup while CUDA settles); the backend tolerates this — just wait for `healthy`.

### Use an external endpoint instead

Any OpenAI-compatible `/v1/embeddings` server works (SGLang, Ollama, LM Studio, OpenAI, Azure, …). Configure in `.env` (bootstrap defaults; also editable live from the admin GUI modal):

```dotenv
EMBEDDING_BASE_URL=http://embed-server:30000/v1
EMBEDDING_API_KEY=
EMBEDDING_MODEL=BAAI/bge-large-en-v1.5
EMBEDDING_DIM=1024
```

`host.docker.internal` resolves through the compose `host-gateway` mapping for host-local endpoints. For cloud endpoints note that the stack network is internal by design — the `backend` (and `ragdb-fixture`, at seed time) need an egress-capable network, which you grant by attaching them to a non-internal network through a small compose override file.

### The embedding lifecycle

1. `make seed-ragdb` loads the corpus **without embeddings** (offline, phase 1).
2. Configuring the endpoint starts a single-flight re-embed job with live progress: the admin GUI modal (**Save and re-embed**) or `make reembed`.
3. Changing the URL, model, or dimension flips the status to `stale` and requests a re-embed; a dimension change recreates the `embeddings` table.
4. Starting a session on a `category: RAG` task fails with a structured "embedding missing" error until the status is `ready` — other tasks are unaffected. `run_rag_query` embeds the query server-side; the vector never reaches the agent.

## Databases

Five data stores, two storage kinds. All seeding is idempotent, and all stores persist across `docker compose down` / restarts.

### `admindb` — catalog and control plane (PostgreSQL 17)

Task catalog, sessions, episode history, and GUI/agent configuration (embedding endpoint, judge settings — stored values override `.env` bootstrap). Initialized by `db/init` DDL on first start; data lives in the named volume `admindb_data`. Published on `127.0.0.1:5433` by the access overlay.

### `taskdb` — OLTP playground (PostgreSQL 17)

The OLTP task target: a TPC-C 5.11 fixture at 1 warehouse (`warehouse`, `district`, `customer`, `history`, `new_order`, `oorder`, `order_line`, `stock`, `item`, with Postgres-safe lowercase names and referential integrity). Seeded by `make seed` (drops legacy TPC-H tables if present); data lives in the named volume `taskdb_data`. Published on `127.0.0.1:5434` by the access overlay.

### `ragdb` — RAG corpus + vectors (PostgreSQL 17 + pgvector)

The authored incident-report corpus (51 documents with genuine `product_version` / `status` / `resolved_date` metadata and planted relevance structure) plus the passage-level `embeddings` table (HNSW cosine index, dimension from `EMBEDDING_DIM`, default 1024). Seeded offline by `make seed-ragdb`; data lives in the named volume `ragdb_data`.

> **Port caveat (verified live):** the compose file declares `${RAGDB_PORT:-5435}:5432`, but the binding is **inert** — `ragdb` sits only on the internal network, so host connections to 5435 fail. Reach `ragdb` through the backend/GUI; direct `psql` access requires attaching it to the access overlay in a local overlay file.

### `olapdb` and `dsdb` — DuckDB fixture files (no service)

Two analytical fixtures are plain **files**, not services: `./data/olapdb/olap-tpch.duckdb` (TPC-H SF 0.01, mounted at backend `/data`, `OLAPDB_URL=duckdb:////data/olap-tpch.duckdb`) and `./data/dsdb/tpcds.duckdb` (TPC-DS SF 0.01 retail, backend `/ds`, `DSDB_URL=duckdb:////ds/tpcds.duckdb`). No host port exists or is needed.

**DuckDB single-writer contract:** a DuckDB file allows one read-write process at a time. The backend owns each file between episodes (engines open lazily per episode and dispose at reset); the `make tasks` seeder opens a short-lived connection and exits. Episode writes never corrupt the fixture: state-check tasks roll back everything — including `CREATE TABLE` — at episode end.

### Resetting a store

Named volumes (survives `down`; true reset needs volume removal):

```bash
docker compose -f docker-compose.yml -f docker-compose.access.yml down
docker volume rm dbarena_admindb_data   # or dbarena_taskdb_data / dbarena_ragdb_data
docker compose -f docker-compose.yml -f docker-compose.access.yml up -d
make seed        # repeat with seed-olap / seed-ds / seed-ragdb / make tasks as needed
```

For `ragdb`, re-running `make seed-ragdb` alone refreshes documents and fixture metadata but **preserves embeddings**; the volume removal above is the true reset.

DuckDB bind mounts (true reset = delete the file and re-seed):

```bash
docker compose stop backend
rm data/olapdb/olap-tpch.duckdb     # or data/dsdb/tpcds.duckdb
make seed-olap                       # or make seed-ds
docker compose -f docker-compose.yml -f docker-compose.access.yml up -d
make tasks
```

`make reset` does the everyday version: stop, recreate, and re-apply all seed data while preserving volumes.

## MCP: using DBArena from AI agents

The `mcp` service exposes the admin API to AI clients (Claude Desktop, IDE copilots, agent harnesses) over streamable-HTTP at `http://127.0.0.1:8090/mcp` (access overlay). It is a thin stateless proxy — nine tools, one backend call each:

| Tool | Operation |
| --- | --- |
| `dbarena_health` | Backend health and admin-database reachability |
| `list_task_classifications` | Allowed categories (`OLTP`, `OLAP`, `ANALYTICS`, `RAG`) and intent types (`NAV`, `IS`, `CC`, `MIX`) |
| `list_tasks` / `get_task` | Task catalog read (`list_tasks` returns `{"tasks": [...]}`; tasks carry `terminal_condition` as a counts-only summary and omit `sqlalchemy_url` — grading details stay in the admin GUI) |
| `start_session` | Start an episode for a task |
| `step_session` | Apply one action (`run_sql`, `describe_table`, `describe_tables`, `describe_schema`, `run_rag_query`, `submit_final_answer`) |
| `reset_session` / `get_session` | Reset an episode, read a session's step history |
| `get_interface` | Self-describing action specs and observation catalog |

**stdio clients (e.g. Claude Desktop):** build once (`docker compose build mcp`), then add to the client's `mcpServers`:

```json
{
  "mcpServers": {
    "dbarena": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "DBARENA_API_URL=http://host.docker.internal:8000",
        "-e", "MCP_TRANSPORT=stdio",
        "dbarena-mcp"
      ]
    }
  }
}
```

Without Docker on the client: `pip install -e ".[mcp]"` from the repository root and point the client at `python -m mcp_server` with `DBARENA_API_URL=http://127.0.0.1:8000`.

Configuration (flags override environment variables; `python -m mcp_server --help` shows all): `DBARENA_API_URL` (default `http://127.0.0.1:8000`), `MCP_TRANSPORT` (`stdio` default, or `streamable-http`), `MCP_PORT` / `MCP_HTTP_HOST` (default `8090` / `127.0.0.1`), `MCP_SESSION_IDLE_TIMEOUT` (default `0`), `DBARENA_TIMEOUT_SECONDS` (default `120`; episode steps can block up to a task's query timeout).

## Operating the stack

```bash
make status                     # docker compose ps — check STATE and PORTS columns
make logs SERVICE=backend       # docker compose logs -f backend
make down                       # docker compose down — volumes preserved
make reset                      # stop, recreate, re-apply all seed data
```

`make reset` is the everyday reset: it stops and recreates the containers, then re-runs `make seed`, `make seed-olap`, `make seed-ds`, `make seed-ragdb`, and `make tasks`. Named volumes survive `down` and `reset` — for a true fixture reset see [Resetting a store](#resetting-a-store).

**After code changes:** `docker compose restart` does not pick up image changes. Rebuild the changed service and bring the stack back with the overlay:

```bash
docker compose build backend    # or mcp, frontend, judge, embed-server, ...
docker compose -f docker-compose.yml -f docker-compose.access.yml up -d
```

## Development and tests (containerized)

Python development and tests run in Docker. The dev image installs the package with dev extras; the repository is mounted so edits take effect without rebuilding.

```bash
docker build -t dbarena-dev -f dev/Dockerfile .
docker run --rm -v "$(pwd)":/app -w /app dbarena-dev python -m pytest tests/
```

Integration tests need an ephemeral PostgreSQL — without `TEST_PG_URL` they skip silently (a "green" run may not have exercised them):

```bash
docker network create dbarena-test
docker run -d --name dbarena-test-pg --network dbarena-test \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_DB=arena pgvector/pgvector:pg17
docker run --rm --network dbarena-test -v "$(pwd)":/app -w /app \
  -e TEST_PG_URL=postgresql+psycopg://postgres:testpw@dbarena-test-pg:5432/arena \
  dbarena-dev python -m pytest tests/
docker rm -f dbarena-test-pg
```

Lint: `docker run --rm -v "$(pwd)":/app -w /app dbarena-dev ruff check src tests db mcp_server`.

Frontend development uses Vite with an `/api` proxy (target `DBARENA_API_URL`, default `http://127.0.0.1:8000`):

```bash
cd gui/frontend && npm install && npm run dev
```

## Authors

- **Ivan Conde-Sarmiento** — CISE Program, University of Puerto Rico, Mayagüez — ivan.conde@upr.edu
- **Manuel Rodriguez-Martinez** — CSE Department, University of Puerto Rico, Mayagüez — manuel.rodriguez7@upr.edu

## Acknowledgments

This material is based upon work supported by the National Science Foundation under Grant No. OAC-2407329. This paper was supported in part by a grant from the Center of Excellence for Transportation and Logistics (CETL) at University of Puerto Rico, Mayagüez. Any opinions, findings, and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the National Science Foundation, or the CETL.
