from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, Engine

from dbarena.models.observations import TableDescription, TableSummary


class UnsupportedEngineError(Exception):
    def __init__(self, engine_name: str):
        self.engine_name = engine_name
        super().__init__(f"no adapter registered for engine {engine_name!r}")


@dataclass
class SqlResult:
    columns: list[str]
    rows: list[list[Any]]
    fetch_limited: bool = False


class EngineAdapter(ABC):
    dialect_name: str = ""

    @abstractmethod
    def run_sql(
        self,
        engine: Engine,
        sql: str,
        row_limit: int,
        timeout_seconds: float,
        connection: Connection | None = None,
    ) -> SqlResult: ...

    @abstractmethod
    def describe_table(self, engine: Engine, table: str) -> TableDescription: ...

    @abstractmethod
    def describe_tables(self, engine: Engine, tables: list[str]) -> list[TableDescription]: ...

    @abstractmethod
    def describe_schema(self, engine: Engine) -> list[TableSummary]: ...


@dataclass
class FakeSqlResult(SqlResult):
    statements: list[str] = field(default_factory=list)


class FakeAdapter(EngineAdapter):
    dialect_name = "fake"

    def __init__(
        self,
        result: SqlResult | None = None,
        tables: list[TableDescription] | None = None,
        schema: list[TableSummary] | None = None,
    ):
        self.result = result or SqlResult(columns=["id"], rows=[[1]], fetch_limited=False)
        self.tables = tables or []
        self.schema = schema or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_sql(
        self,
        engine: Engine,
        sql: str,
        row_limit: int,
        timeout_seconds: float,
        connection: Connection | None = None,
    ) -> SqlResult:
        self.calls.append(
            ("run_sql", {"sql": sql, "row_limit": row_limit, "timeout": timeout_seconds, "connection": connection})
        )
        return self.result

    def describe_table(self, engine: Engine, table: str) -> TableDescription:
        self.calls.append(("describe_table", {"table": table}))
        for t in self.tables:
            if t.name == table:
                return t
        return TableDescription(name=table, columns=[], found=False)

    def describe_tables(self, engine: Engine, tables: list[str]) -> list[TableDescription]:
        self.calls.append(("describe_tables", {"tables": list(tables)}))
        return [self.describe_table(engine, t) for t in tables]

    def describe_schema(self, engine: Engine) -> list[TableSummary]:
        self.calls.append(("describe_schema", {}))
        return list(self.schema)
