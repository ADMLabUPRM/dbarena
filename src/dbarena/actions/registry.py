from sqlalchemy.engine import make_url

from dbarena.actions.base import EngineAdapter
from dbarena.actions.duckdb import DuckDbAdapter
from dbarena.actions.null_adapter import NullAdapter
from dbarena.actions.postgresql import PostgresAdapter

RUN_SQL = "run_sql"
DESCRIBE_TABLE = "describe_table"
DESCRIBE_TABLES = "describe_tables"
DESCRIBE_SCHEMA = "describe_schema"
SUBMIT_FINAL_ANSWER = "submit_final_answer"
RUN_RAG_QUERY = "run_rag_query"

ACTION_NAMES = (RUN_SQL, DESCRIBE_TABLE, DESCRIBE_TABLES, DESCRIBE_SCHEMA, SUBMIT_FINAL_ANSWER, RUN_RAG_QUERY)


class EngineRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, EngineAdapter] = {}

    def register(self, dialect_name: str, adapter: EngineAdapter) -> None:
        self._adapters[dialect_name] = adapter

    def adapter_for(self, sqlalchemy_url: str) -> EngineAdapter:
        backend = make_url(sqlalchemy_url).get_backend_name()
        return self._adapters.get(backend, NullAdapter(backend))


def default_registry() -> EngineRegistry:
    registry = EngineRegistry()
    registry.register("postgresql", PostgresAdapter())
    registry.register("duckdb", DuckDbAdapter())
    return registry


def dialect_of(sqlalchemy_url: str) -> str:
    return make_url(sqlalchemy_url).get_backend_name()
