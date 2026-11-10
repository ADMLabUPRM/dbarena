from sqlalchemy import Engine

from dbarena.actions.base import EngineAdapter, SqlResult, UnsupportedEngineError
from dbarena.models.observations import TableDescription, TableSummary


class NullAdapter(EngineAdapter):
    def __init__(self, dialect_name: str):
        self.dialect_name = dialect_name

    def _raise(self) -> None:
        raise UnsupportedEngineError(self.dialect_name)

    def run_sql(self, engine: Engine, sql: str, row_limit: int, timeout_seconds: float) -> SqlResult:
        self._raise()

    def describe_table(self, engine: Engine, table: str) -> TableDescription:
        self._raise()

    def describe_tables(self, engine: Engine, tables: list[str]) -> list[TableDescription]:
        self._raise()

    def describe_schema(self, engine: Engine) -> list[TableSummary]:
        self._raise()
