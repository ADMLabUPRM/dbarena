"""The backend embedding lifecycle: persisted configuration, status derivation,
single-flight re-embed jobs, and the resolver/gate callables used by the gym.

Control plane lives in admindb (`embedding_config`, `embedding_jobs`,
`rag_fixture_meta` written by the ragdb fixture loader); embedding data lives in
ragdb (`documents`, `embeddings`). The re-embed job runs on a daemon thread —
SQLAlchemy engines here are synchronous, and one job at a time is the contract.

Embeddings are passage-level: every document is chunked over its whole length
(sliding window, configurable size/overlap) and each chunk becomes one
`embeddings` row keyed by (incident_id, chunk_index).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine, create_engine, text

from dbarena.embeddings import (
    EMBEDDING_STATES,
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
    chunk_text,
    derive_status,
)

R02_TASK_ID = "DBA-R02-001"
_VALIDATION_TOP_K = 5
_JOB_STATES = ("running", "done", "validation_failed", "failed")
# Chunk budget per API call and per-doc batch size; adaptive halving on
# token-limit 400s reduces the effective chunk size (floor 200 chars).
_CHUNKS_PER_CALL = 96
_DOC_BATCH = 64
_MIN_CHUNK_CHARS = 200


class EmbeddingServiceError(Exception):
    pass


class EmbeddingService:
    def __init__(
        self,
        admin_engine: Engine,
        ragdb_url: str | None = None,
        catalog: Any = None,
        env: dict[str, str] | None = None,
        client_factory: Callable[[EmbeddingConfig], Any] | None = None,
    ):
        self.admin_engine = admin_engine
        self.ragdb_url = ragdb_url
        self.catalog = catalog
        self._env = env
        self._client_factory = client_factory or (lambda config: EmbeddingClient(config))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ragdb_engine: Engine | None = None

    # ------------------------------------------------------------------ config

    def ensure_tables(self) -> None:
        from dbarena.catalog.migrations import ensure_embedding_schema

        ensure_embedding_schema(self.admin_engine)

    def bootstrap_from_env(self) -> None:
        if self.get_config() is None:
            config = EmbeddingConfig.from_env(self._env)
            if config is not None:
                self.save_config(config)

    def get_config(self) -> EmbeddingConfig | None:
        with self.admin_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT base_url, model, dim, api_key, max_model_tokens, chunk_chars, chunk_overlap "
                    "FROM embedding_config WHERE id"
                )
            ).mappings().first()
        if row is None:
            return None
        return EmbeddingConfig(
            base_url=row["base_url"],
            model=row["model"],
            dim=int(row["dim"]),
            api_key=row["api_key"] or "",
            max_model_tokens=int(row["max_model_tokens"]),
            chunk_chars=int(row["chunk_chars"]),
            chunk_overlap=int(row["chunk_overlap"]),
        )

    def save_config(self, config: EmbeddingConfig) -> EmbeddingConfig:
        if not config.base_url or not config.model or config.dim <= 0:
            raise EmbeddingServiceError("base_url, model, and a positive dim are required")
        if config.max_model_tokens <= 0:
            raise EmbeddingServiceError("max_model_tokens must be positive")
        if config.chunk_chars <= 0:
            raise EmbeddingServiceError("chunk_chars must be positive")
        if not 0 <= config.chunk_overlap < config.chunk_chars:
            raise EmbeddingServiceError("chunk_overlap must be >= 0 and smaller than chunk_chars")
        with self.admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO embedding_config (id, base_url, model, dim, api_key, "
                    "max_model_tokens, chunk_chars, chunk_overlap) "
                    "VALUES (true, :base_url, :model, :dim, :api_key, :max_model_tokens, :chunk_chars, :chunk_overlap) "
                    "ON CONFLICT (id) DO UPDATE SET base_url = :base_url, model = :model, "
                    "dim = :dim, api_key = :api_key, max_model_tokens = :max_model_tokens, "
                    "chunk_chars = :chunk_chars, chunk_overlap = :chunk_overlap, updated_at = now()"
                ),
                {
                    "base_url": config.base_url,
                    "model": config.model,
                    "dim": config.dim,
                    "api_key": config.api_key or "",
                    "max_model_tokens": config.max_model_tokens,
                    "chunk_chars": config.chunk_chars,
                    "chunk_overlap": config.chunk_overlap,
                },
            )
        return config

    # ------------------------------------------------------------------ status

    def _ragdb(self) -> Engine | None:
        if not self.ragdb_url:
            return None
        if self._ragdb_engine is None:
            self._ragdb_engine = create_engine(self.ragdb_url, pool_pre_ping=True)
        return self._ragdb_engine

    def _fixture_counts(self, config: EmbeddingConfig | None) -> tuple[int, int, int]:
        """(embedded, total, stale) document counts from ragdb; (0, 0, 0) when unreachable."""
        engine = self._ragdb()
        if engine is None:
            return 0, 0, 0
        try:
            with engine.connect() as conn:
                total = conn.execute(text("SELECT count(*) FROM documents")).scalar_one()
                if config is None:
                    return 0, total, 0
                embedded = conn.execute(
                    text(
                        "SELECT count(DISTINCT incident_id) FROM embeddings "
                        "WHERE model_version = :fingerprint"
                    ),
                    {"fingerprint": config.fingerprint},
                ).scalar_one()
                stale = conn.execute(
                    text(
                        "SELECT count(DISTINCT incident_id) FROM embeddings "
                        "WHERE model_version <> :fingerprint"
                    ),
                    {"fingerprint": config.fingerprint},
                ).scalar_one()
            return embedded, total, stale
        except Exception:
            return 0, 0, 0

    def status(self) -> dict[str, Any]:
        config = self.get_config()
        embedded, total, stale = self._fixture_counts(config)
        state = derive_status(config, embedded, total, stale)
        job = self._active_job_summary()
        message = None
        if job is not None and job["state"] in ("validation_failed", "failed"):
            message = job.get("detail")
            state = "stale" if state == "ready" else state
        return {
            "state": state,
            "states": list(EMBEDDING_STATES),
            "config": config.public_dict() if config else None,
            "embedded": embedded,
            "total": total,
            "job": job,
            "message": message,
        }

    def _active_job_summary(self) -> dict[str, Any] | None:
        with self.admin_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT job_id, state, completed, remaining, total, detail, started_at, ended_at "
                    "FROM embedding_jobs ORDER BY started_at DESC LIMIT 1"
                )
            ).mappings().first()
        if row is None:
            return None
        return {
            "job_id": str(row["job_id"]),
            "state": row["state"],
            "completed": int(row["completed"]),
            "remaining": int(row["remaining"]),
            "total": int(row["total"]),
            "detail": row["detail"],
        }

    # ------------------------------------------------------------------ jobs

    def start_reembed(self) -> dict[str, Any]:
        config = self.get_config()
        if config is None:
            raise EmbeddingServiceError("embedding model is not configured")
        with self._lock:
            active = self._active_job_summary()
            if active is not None and active["state"] == "running":
                raise EmbeddingServiceError("a re-embed job is already running")
            with self.admin_engine.begin() as conn:
                job_id = conn.execute(
                    text(
                        "INSERT INTO embedding_jobs (state, fingerprint, completed, remaining, total) "
                        "VALUES ('running', :fingerprint, 0, 0, 0) RETURNING job_id"
                    ),
                    {"fingerprint": config.fingerprint},
                ).scalar_one()
            thread = threading.Thread(target=self._run_job, args=(str(job_id), config), daemon=True)
            self._thread = thread
            thread.start()
        return {"job_id": str(job_id), "state": "running"}

    def _update_job(self, job_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{key} = :{key}" for key in fields)
        fields["id"] = job_id
        with self.admin_engine.begin() as conn:
            conn.execute(
                text(f"UPDATE embedding_jobs SET {assignments} WHERE job_id = CAST(:id AS uuid)"),
                fields,
            )

    def _run_job(self, job_id: str, config: EmbeddingConfig) -> None:
        engine = self._ragdb()
        if engine is None:
            self._finish(job_id, "failed", "RAGDB_URL is not configured")
            return
        try:
            self._prepare_embeddings_table(engine, config)
            self._embed_missing(engine, self._client_factory, config, job_id)
            detail = self._validate(config)
            if detail is None:
                self._finish(job_id, "done", None)
            else:
                self._finish(job_id, "validation_failed", detail)
        except EmbeddingError as exc:
            self._finish(job_id, "failed", f"{exc.code}: {exc}")
        except Exception as exc:  # noqa: BLE001 - job failures surface in status
            self._finish(job_id, "failed", str(exc))

    def _prepare_embeddings_table(self, engine: Engine, config: EmbeddingConfig) -> None:
        """Recreate the embeddings table when its shape or dimension differs."""
        with engine.begin() as conn:
            declared = conn.execute(
                text(
                    "SELECT format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a "
                    "WHERE a.attrelid = to_regclass('embeddings') AND a.attname = 'embedding'"
                )
            ).scalar_one_or_none()
            declared_dim = None
            if declared:
                match = re.search(r"\d+", declared)
                declared_dim = int(match.group()) if match else None
            has_chunk_index = conn.execute(
                text(
                    "SELECT count(*) FROM pg_attribute "
                    "WHERE attrelid = to_regclass('embeddings') AND attname = 'chunk_index'"
                )
            ).scalar_one()
            has_key_column = conn.execute(
                text(
                    "SELECT count(*) FROM pg_attribute "
                    "WHERE attrelid = to_regclass('embeddings') AND attname = 'incident_id'"
                )
            ).scalar_one()
            needs_recreate = (
                has_chunk_index == 0
                or has_key_column == 0
                or (declared_dim is not None and declared_dim != config.dim)
            )
            if needs_recreate:
                conn.execute(text("DROP TABLE IF EXISTS embeddings"))
                conn.execute(
                    text(
                        "CREATE TABLE embeddings ("
                        "incident_id text NOT NULL REFERENCES documents(incident_id) ON DELETE CASCADE, "
                        "chunk_index integer NOT NULL, "
                        "model_version text NOT NULL, "
                        f"embedding vector({config.dim}) NOT NULL, "
                        "PRIMARY KEY (incident_id, chunk_index))"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX embeddings_hnsw ON embeddings USING hnsw (embedding vector_cosine_ops)"
                    )
                )
            elif conn.execute(
                text("SELECT count(*) FROM embeddings WHERE model_version <> :fp"),
                {"fp": config.fingerprint},
            ).scalar_one():
                conn.execute(text("DELETE FROM embeddings"))

    def _embed_missing(
        self,
        engine: Engine,
        client_factory: Callable[[EmbeddingConfig], Any],
        config: EmbeddingConfig,
        job_id: str,
    ) -> None:
        # Adaptive halving restarts the whole pass with a uniform chunk size:
        # mixed-granularity vectors under one fingerprint would poison ranking.
        effective_chars = config.chunk_chars
        while True:
            complete = self._embed_pass_locked(
                engine, client_factory, config, job_id, effective_chars
            )
            if complete:
                break
            effective_chars = max(_MIN_CHUNK_CHARS, effective_chars // 2)
        with engine.connect() as conn:
            total = conn.execute(text("SELECT count(*) FROM documents")).scalar_one()
        self._update_job(job_id, completed=total, remaining=0, total=total)

    def _embed_pass_locked(
        self,
        engine: Engine,
        client_factory: Callable[[EmbeddingConfig], Any],
        config: EmbeddingConfig,
        job_id: str,
        effective_chars: int,
    ) -> bool:
        """One uniform-granularity embedding pass. Returns True when complete.

        A token-limit 400 clears this fingerprint's rows and returns False so
        the caller restarts the pass with a halved chunk size (floor 200) —
        every stored vector then shares one chunk granularity.
        """
        with engine.connect() as conn:
            total = conn.execute(text("SELECT count(*) FROM documents")).scalar_one()
            while True:
                rows = conn.execute(
                    text(
                        "SELECT d.incident_id, d.title, d.body FROM documents d "
                        "WHERE NOT EXISTS ("
                        "  SELECT 1 FROM embeddings e WHERE e.incident_id = d.incident_id "
                        "  AND e.model_version = :fp) "
                        "ORDER BY d.incident_id LIMIT :batch"
                    ),
                    {"fp": config.fingerprint, "batch": _DOC_BATCH},
                ).all()
                if not rows:
                    return True
                try:
                    self._embed_doc_batch(engine, client_factory, config, rows, effective_chars)
                except EmbeddingError as exc:
                    if exc.code != "request_failed" or effective_chars <= _MIN_CHUNK_CHARS:
                        raise
                    with engine.begin() as clear_conn:
                        clear_conn.execute(
                            text("DELETE FROM embeddings WHERE model_version = :fp"),
                            {"fp": config.fingerprint},
                        )
                    self._update_job(
                        job_id,
                        detail=(
                            f"token-limit 400s reduced the effective chunk size from {effective_chars} "
                            f"to {max(_MIN_CHUNK_CHARS, effective_chars // 2)} chars; "
                            "re-embedding the corpus uniformly"
                        ),
                        completed=0,
                        remaining=total,
                    )
                    return False
                done = conn.execute(
                    text(
                        "SELECT count(DISTINCT incident_id) FROM embeddings WHERE model_version = :fp"
                    ),
                    {"fp": config.fingerprint},
                ).scalar_one()
                self._update_job(job_id, completed=done, remaining=max(0, total - done), total=total)

    def _embed_doc_batch(
        self,
        engine: Engine,
        client_factory: Callable[[EmbeddingConfig], Any],
        config: EmbeddingConfig,
        rows: list[Any],
        effective_chars: int,
    ) -> None:
        """Embed one batch of documents (whole-document chunking, no cap)."""
        passages: list[tuple[str, int, str]] = []
        for row in rows:
            body = f"{row.title}. {row.body}"
            for chunk_index, chunk in enumerate(chunk_text(body, effective_chars, config.chunk_overlap)):
                passages.append((row.incident_id, chunk_index, chunk))
        results = self._embed_passages(client_factory, config, passages)
        self._insert_passages(engine, config, results)

    def _embed_passages(
        self,
        client_factory: Callable[[EmbeddingConfig], Any],
        config: EmbeddingConfig,
        passages: list[tuple[str, int, str]],
    ) -> list[tuple[str, int, Any]]:
        """Embed passages in calls of _CHUNKS_PER_CALL; results aligned to input order."""
        client = client_factory(config)
        results: list[tuple[str, int, Any]] = []
        for start in range(0, len(passages), _CHUNKS_PER_CALL):
            window = passages[start : start + _CHUNKS_PER_CALL]
            embedded = client.embed([chunk for _, _, chunk in window])
            for (incident_id, chunk_index, _chunk), result in zip(window, embedded, strict=True):
                results.append((incident_id, chunk_index, result))
        return results

    def _insert_passages(self, engine: Engine, config: EmbeddingConfig, results: list[tuple[str, int, Any]]) -> None:
        with engine.begin() as embed_conn:
            for incident_id, chunk_index, result in results:
                embed_conn.execute(
                    text(
                        "INSERT INTO embeddings (incident_id, chunk_index, model_version, embedding) "
                        "VALUES (:sid, :idx, :fp, CAST(:vec AS vector)) "
                        "ON CONFLICT (incident_id, chunk_index) DO UPDATE SET "
                        "model_version = :fp, embedding = CAST(:vec AS vector)"
                    ),
                    {"sid": incident_id, "idx": chunk_index, "fp": config.fingerprint, "vec": result.vector_literal},
                )

    def _finish(self, job_id: str, state: str, detail: str | None) -> None:
        with self.admin_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE embedding_jobs SET state = :state, detail = :detail, ended_at = now() "
                    "WHERE job_id = CAST(:id AS uuid)"
                ),
                {"state": state, "detail": detail, "id": job_id},
            )

    def _validate(self, config: EmbeddingConfig) -> str | None:
        """Gold-label ranking check; None passes, a string is the failure detail."""
        if self.catalog is None:
            return None
        task = self.catalog.get(R02_TASK_ID)
        if task is None:
            return None
        engine = self._ragdb()
        if engine is None:
            return "ragdb is not reachable for validation"
        with self.admin_engine.connect() as conn:
            meta = conn.execute(
                text(
                    "SELECT query_text, version, resolved_cutoff, gold_incident_ids "
                    "FROM rag_fixture_meta WHERE id"
                )
            ).mappings().first()
        if meta is None:
            return None
        gold = [str(item) for item in meta["gold_incident_ids"]]
        try:
            client = self._client_factory(config)
            # embed the clean corpus query, not the agent-facing goal wrapper —
            # the fixture validates that the model ranks the gold incidents for
            # the query itself
            result = client.embed([meta["query_text"]])[0]
        except EmbeddingError as exc:
            return f"validation could not embed the task query: {exc}"
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT incident_id FROM ("
                    "  SELECT d.incident_id, min(e.embedding <=> CAST(:qv AS vector)) AS dist "
                    "  FROM documents d JOIN embeddings e ON e.incident_id = d.incident_id "
                    "  WHERE d.product_version = :version "
                    "  AND (d.resolved_date IS NULL OR d.resolved_date >= :cutoff) "
                    "  GROUP BY d.incident_id ORDER BY dist LIMIT :k"
                    ") ranked"
                ),
                {
                    "version": meta["version"],
                    "cutoff": meta["resolved_cutoff"],
                    "qv": result.vector_literal,
                    "k": max(len(gold), _VALIDATION_TOP_K),
                },
            ).all()
        retrieved = {row[0] for row in rows}
        missing = [doc_id for doc_id in gold if doc_id not in retrieved]
        if missing:
            return (
                f"the configured model does not retrieve {len(missing)} of {len(gold)} gold documents "
                f"for the R02 query ({', '.join(missing[:5])}); try a stronger embedding model"
            )
        return None

    # ------------------------------------------------------------------ gym hooks

    def resolver(self) -> Callable[[str], Any]:
        def resolve(value: str) -> Any:
            config = self.get_config()
            if config is None:
                raise EmbeddingError(
                    "not_configured", "embedding model is not configured; configure it in the admin GUI"
                )
            client = self._client_factory(config)
            return client.embed([value])[0]

        return resolve

    def gate(self) -> Callable[[Any], str | None]:
        def gate(task: Any) -> str | None:
            if getattr(task, "category", None) != "RAG":
                return None
            state = self.status()["state"]
            if state == "ready":
                return None
            if state == "unconfigured":
                return (
                    "embedding missing: the embedding model is not configured. "
                    "Configure it in the admin GUI and re-embed the corpus."
                )
            if state == "stale":
                return (
                    "embedding missing: the embedding configuration changed after the last embedding run. "
                    "Re-embed the corpus before starting RAG episodes."
                )
            return "embedding missing: the document corpus is not fully embedded. Run re-embed from the admin GUI."

        return gate
