import importlib.util
import pathlib

import pytest
from sqlalchemy import text

from dbarena.catalog.migrations import ensure_embedding_schema
from dbarena.catalog.records import TaskRecord
from dbarena.embeddings import EmbeddingConfig, EmbeddingError, EmbeddingResult
from dbarena.env.environment import DBArenaEnv
from dbarena.models.terminal import MatchingRules, TerminalCondition

R02_TASK_ID = "DBA-R02-001"

GOLD = ["INC-1041", "INC-1047", "INC-1052", "INC-1058"]


def _load_seed_tasks():
    path = pathlib.Path(__file__).resolve().parents[1] / "db" / "seed_tasks.py"
    spec = importlib.util.spec_from_file_location("seed_tasks_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_tasks = _load_seed_tasks()
seed_rag_tasks = seed_tasks.seed_rag_tasks


class RecordingStore:
    def __init__(self):
        self.created = []

    def delete(self, task_id: str) -> bool:
        return False

    def create(self, record: TaskRecord) -> TaskRecord:
        self.created.append(record)
        return record


@pytest.fixture()
def admin_tables(pg_engine):
    ensure_embedding_schema(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM rag_fixture_meta"))
    yield pg_engine
    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM rag_fixture_meta"))


def _insert_fixture_meta(engine, gold=GOLD, version="4.2.0", cutoff="2024-06-01"):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rag_fixture_meta (query_text, version, resolved_cutoff, gold_incident_ids) "
                "VALUES ('connection pool exhaustion under load', :version, :cutoff, CAST(:gold AS jsonb))"
            ),
            {"version": version, "cutoff": cutoff, "gold": __import__("json").dumps(gold)},
        )


def test_seed_rag_task_derives_oracle_from_authored_labels(pg_url, pg_engine, admin_tables):
    _insert_fixture_meta(pg_engine)
    store = RecordingStore()
    seeded = seed_rag_tasks(store, pg_engine, "postgresql+psycopg://x")
    assert [payload["task_id"] for payload in seeded] == [R02_TASK_ID]
    record = store.created[0]
    assert record.category == "RAG"
    assert record.task_type == "IS"
    assert record.sqlalchemy_url == "postgresql+psycopg://x"
    assert record.terminal_condition.expected_tuples == [[incident] for incident in GOLD]
    assert "connection pool exhaustion under load" in record.goal
    assert "version 4.2.0" in record.goal
    assert "2024-06-01" in record.goal
    # mechanism-free goal: no embedding mechanics leak into the request
    assert "embed" not in record.goal.lower()
    assert "cosine" not in record.goal.lower()
    assert "vector" not in record.goal.lower()
    assert "[incident_id]" in record.submission


def test_seed_rag_task_skips_without_fixture_meta(pg_url, pg_engine, admin_tables):
    store = RecordingStore()
    seeded = seed_rag_tasks(store, pg_engine, "postgresql+psycopg://x")
    assert seeded == [] and store.created == []


def _service(pg_engine, embedded, total, stale=0):
    from gui.backend.app.embedding_store import EmbeddingService

    service = EmbeddingService(pg_engine, ragdb_url=None, catalog=None)
    service._fixture_counts = lambda config: (embedded, total, stale)  # type: ignore[method-assign]
    return service


def test_config_persistence_and_status(pg_engine, admin_tables):
    service = _service(pg_engine, 0, 0)
    assert service.get_config() is None
    config = EmbeddingConfig(
        base_url="http://embed:1/v1",
        model="m",
        dim=8,
        api_key="k",
        max_model_tokens=256,
        chunk_chars=600,
        chunk_overlap=100,
    )
    saved = service.save_config(config)
    loaded = service.get_config()
    assert loaded.fingerprint == saved.fingerprint
    assert loaded.max_model_tokens == 256 and loaded.chunk_chars == 600 and loaded.chunk_overlap == 100
    status = service.status()
    assert status["config"] == {
        "base_url": "http://embed:1/v1",
        "model": "m",
        "dim": 8,
        "max_model_tokens": 256,
        "chunk_chars": 600,
        "chunk_overlap": 100,
    }
    assert "api_key" not in status["config"]


def test_save_config_rejects_invalid_chunking(pg_engine, admin_tables):
    service = _service(pg_engine, 0, 0)
    with pytest.raises(Exception, match="chunk_overlap"):
        service.save_config(
            EmbeddingConfig(base_url="http://e:1/v1", model="m", dim=8, chunk_chars=100, chunk_overlap=100)
        )


def test_gate_blocks_rag_until_ready(pg_engine, admin_tables):
    _insert_fixture_meta(pg_engine)
    service = _service(pg_engine, 0, 5183)
    service.save_config(EmbeddingConfig(base_url="http://embed:1/v1", model="m", dim=8))

    class FakeTask:
        category = "RAG"

    message = service.gate()(FakeTask())
    assert message is not None and "embedding missing" in message

    service._fixture_counts = lambda config: (5183, 5183, 0)  # type: ignore[method-assign]
    assert service.gate()(FakeTask()) is None

    class OltpTask:
        category = "OLTP"

    service._fixture_counts = lambda config: (0, 5183, 0)  # type: ignore[method-assign]
    assert service.gate()(OltpTask()) is None

    service._fixture_counts = lambda config: (5183, 5183, 7)  # type: ignore[method-assign]
    assert "Re-embed" in service.gate()(FakeTask())


class _SingleTaskCatalog:
    def __init__(self, record):
        self._record = record

    def get(self, task_id):
        return self._record if task_id == self._record.task_id else None


def _task(pg_url):
    return TaskRecord(
        task_id=R02_TASK_ID,
        goal="Find the incident reports about connection pool exhaustion under load.",
        submission="Call submit_final_answer with one tuple per matching incident: [incident_id].",
        category="RAG",
        task_type="IS",
        max_steps=8,
        sqlalchemy_url=pg_url,
        terminal_condition=TerminalCondition(
            expected_tuples=[["INC-1"]],
            matching=MatchingRules(order_sensitive=False, numeric_tolerance=None, null_handling="exact"),
        ),
        query_timeout=10,
        row_limit=None,
    )


def test_run_rag_query_substitutes_and_executes(pg_url, pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS documents (incident_id text PRIMARY KEY, title text)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS embeddings (incident_id text PRIMARY KEY, embedding vector(3))"))
        conn.execute(text("TRUNCATE documents, embeddings"))
        conn.execute(text("INSERT INTO documents VALUES ('INC-1', 'pool exhaustion'), ('INC-2', 'disk full')"))
        conn.execute(text("INSERT INTO embeddings VALUES ('INC-1', '[1.0, 0.0, 0.0]'), ('INC-2', '[0.0, 1.0, 0.0]')"))
    env = DBArenaEnv(
        _SingleTaskCatalog(_task(pg_url)),
        embed_resolver=lambda value: EmbeddingResult(vector=[1.0, 0.0, 0.0], model="m", dim=3),
    )
    env.reset(options={"task_id": R02_TASK_ID})
    obs, reward, terminated, _truncated, _info = env.step(
        {
            "tool": "run_rag_query",
            "arguments": {
                "sql": (
                    "SELECT d.incident_id FROM documents d "
                    "JOIN embeddings e ON e.incident_id = d.incident_id "
                    "ORDER BY e.embedding <=> :query_vector LIMIT 2"
                ),
                "text": "pool exhaustion",
            },
        }
    )
    assert obs["type"] == "query_result" and reward == 0.0 and not terminated
    rows = obs["payload"]["rows"]
    assert rows[0][0] == "INC-1"  # nearest by the injected vector
    assert obs["payload"]["row_count"] == 2
    env.close()  # roll back the episode transaction so the cleanup TRUNCATE can take its locks
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE documents, embeddings"))


def test_run_rag_query_requires_placeholder(pg_url):
    env = DBArenaEnv(
        _SingleTaskCatalog(_task(pg_url)),
        embed_resolver=lambda value: EmbeddingResult(vector=[1.0], model="m", dim=1),
    )
    env.reset(options={"task_id": R02_TASK_ID})
    obs, _reward, _terminated, _truncated, _info = env.step(
        {"tool": "run_rag_query", "arguments": {"sql": "SELECT 1", "text": "hello"}}
    )
    assert obs["type"] == "error"
    assert obs["payload"]["code"] == "invalid_arguments"
    assert ":query_vector" in obs["payload"]["message"]


def test_run_rag_query_without_resolver_is_a_structured_error(pg_url):
    env = DBArenaEnv(_SingleTaskCatalog(_task(pg_url)))
    env.reset(options={"task_id": R02_TASK_ID})
    obs, _reward, _terminated, _truncated, _info = env.step(
        {
            "tool": "run_rag_query",
            "arguments": {"sql": "SELECT :query_vector", "text": "hello"},
        }
    )
    assert obs["type"] == "error"
    assert obs["payload"]["code"] == "embedding_error"
    assert "not configured" in obs["payload"]["message"]


def test_run_rag_query_resolver_failure_is_a_structured_error(pg_url):
    def failing(value):
        raise EmbeddingError("request_failed", "endpoint unreachable")

    env = DBArenaEnv(_SingleTaskCatalog(_task(pg_url)), embed_resolver=failing)
    env.reset(options={"task_id": R02_TASK_ID})
    obs, _reward, _terminated, _truncated, _info = env.step(
        {
            "tool": "run_rag_query",
            "arguments": {"sql": "SELECT :query_vector", "text": "hello"},
        }
    )
    assert obs["type"] == "error"
    assert obs["payload"]["code"] == "embedding_error"
    assert "endpoint unreachable" in obs["payload"]["message"]
