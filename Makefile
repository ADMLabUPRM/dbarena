COMPOSE = docker compose

.PHONY: help build up down status logs seed seed-olap seed-ds seed-ragdb tasks seed-tasks smoke reembed reset

help:
	@echo "make build        - build all images"
	@echo "make up           - start the stack"
	@echo "make down         - stop the stack (volumes preserved)"
	@echo "make status       - show service state"
	@echo "make logs SERVICE=backend - follow logs of a service"
	@echo "make seed         - load TPC-C (1 warehouse) into taskdb (idempotent)"
	@echo "make seed-olap    - install the TPC-H SF 0.01 DuckDB fixture into olapdb (idempotent)"
	@echo "make seed-ds      - install the TPC-DS SF 0.01 retail fixture into dsdb (idempotent)"
	@echo "make seed-ragdb   - load the SciFact document corpus into ragdb, phase 1 without embeddings (idempotent)"
	@echo "make tasks        - seed starter tasks with trusted oracles (idempotent)"
	@echo "make reembed      - configure the embedding endpoint from .env and run the RAG re-embed until ready"
	@echo "make smoke        - run the smoke test against a running stack"
	@echo "make reset        - recreate containers and re-apply seed data (volumes preserved)"
build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

status:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f $(SERVICE)

seed:
	$(COMPOSE) --profile seed run --rm initializer

seed-olap:
	$(COMPOSE) --profile seed run --rm olapdb

seed-ds:
	$(COMPOSE) --profile seed run --rm dsdb

seed-ragdb:
	$(COMPOSE) --profile seed run --rm ragdb-fixture

tasks:
	$(COMPOSE) exec backend python /opt/db/seed_tasks.py

seed-tasks: seed seed-olap seed-ds seed-ragdb tasks

smoke:
	$(COMPOSE) exec -T backend python /opt/db/smoke_test.py

reembed:
	$(COMPOSE) exec -T backend python /opt/db/reembed.py

reset:
	$(COMPOSE) down
	$(COMPOSE) up -d
	$(MAKE) seed
	$(MAKE) seed-olap
	$(MAKE)
	$(MAKE) seed-ds
	$(MAKE) seed-ragdb
	$(MAKE) tasks
