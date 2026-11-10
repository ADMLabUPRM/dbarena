from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from dbarena.actions.base import EngineAdapter, SqlResult
from dbarena.actions.errors import QueryError, QueryTimeoutError
from dbarena.actions.sqlguard import assert_no_transaction_control
from dbarena.models.observations import ColumnInfo, TableDescription, TableSummary

PG_STATEMENT_CANCELED_SQLSTATE = "57014"
CLIENT_GRACE_SECONDS = 5.0

_COLUMNS_SQL = """
SELECT a.attname AS name,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
       NOT a.attnotnull AS nullable,
       pg_get_expr(d.adbin, d.adrelid) AS default,
       EXISTS (
           SELECT 1 FROM pg_constraint p
           WHERE p.conrelid = a.attrelid AND p.contype = 'p' AND a.attnum = ANY (p.conkey)
       ) AS primary_key
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = to_regclass(:relation)
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY a.attnum
"""

_SCHEMA_SQL = """
SELECT c.relname AS name,
       count(a.attnum) AS column_count,
       array_agg(a.attname ORDER BY a.attnum) AS columns
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = current_schema()
  AND c.relkind = 'r'
GROUP BY c.relname
ORDER BY c.relname
"""


class PostgresAdapter(EngineAdapter):
    dialect_name = "postgresql"

    def run_sql(
        self,
        engine: Engine,
        sql: str,
        row_limit: int,
        timeout_seconds: float,
        connection: Connection | None = None,
    ) -> SqlResult:
        assert_no_transaction_control(sql)
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            if connection is not None:
                future: Future = executor.submit(self._execute_on_connection, connection, sql, row_limit)
            else:
                future = executor.submit(self._execute_sql, engine, sql, row_limit, timeout_seconds)
            try:
                return future.result(timeout=timeout_seconds + CLIENT_GRACE_SECONDS)
            except FutureTimeoutError:
                future.cancel()
                raise QueryTimeoutError(
                    f"client-side guard cancelled query after {timeout_seconds + CLIENT_GRACE_SECONDS:.1f}s"
                ) from None
        finally:
            executor.shutdown(wait=False)

    def _execute_on_connection(self, connection: Connection, sql: str, row_limit: int) -> SqlResult:
        try:
            with connection.begin_nested():
                result = connection.execute(text(sql))
                return self._collect_rows(result, row_limit)
        except DBAPIError as exc:
            self._translate_dbapi_error(exc)

    def _collect_rows(self, result: Any, row_limit: int) -> SqlResult:
        if not result.returns_rows:
            return SqlResult(columns=[], rows=[], fetch_limited=False)
        columns = [str(c) for c in result.keys()]
        rows = [list(row) for row in result.fetchmany(row_limit + 1)]
        return SqlResult(columns=columns, rows=rows, fetch_limited=len(rows) > row_limit)

    def _execute_sql(self, engine: Engine, sql: str, row_limit: int, timeout_seconds: float) -> SqlResult:
        timeout_ms = max(1, int(timeout_seconds * 1000))
        try:
            with engine.connect() as conn:
                with conn.begin():
                    conn.execute(
                        text("SELECT set_config('statement_timeout', :value, true)"),
                        {"value": str(timeout_ms)},
                    )
                    result = conn.execute(text(sql))
                    return self._collect_rows(result, row_limit)
        except DBAPIError as exc:
            self._translate_dbapi_error(exc)

    def _translate_dbapi_error(self, exc: DBAPIError) -> None:
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None)
        message = str(orig) if orig is not None else str(exc)
        if sqlstate == PG_STATEMENT_CANCELED_SQLSTATE:
            raise QueryTimeoutError(message) from exc
        raise QueryError(message) from exc

    def describe_table(self, engine: Engine, table: str) -> TableDescription:
        with engine.connect() as conn:
            rows = conn.execute(text(_COLUMNS_SQL), {"relation": table}).mappings().all()
        if not rows:
            return TableDescription(name=table, columns=[], found=False)
        columns = [self._column_info(row) for row in rows]
        return TableDescription(name=table, columns=columns, found=True)

    def describe_tables(self, engine: Engine, tables: list[str]) -> list[TableDescription]:
        return [self.describe_table(engine, table) for table in tables]

    def describe_schema(self, engine: Engine) -> list[TableSummary]:
        with engine.connect() as conn:
            rows = conn.execute(text(_SCHEMA_SQL)).mappings().all()
        return [
            TableSummary(
                name=row["name"],
                column_count=int(row["column_count"]),
                columns=list(row["columns"]),
            )
            for row in rows
        ]

    def _column_info(self, row: Any) -> ColumnInfo:
        return ColumnInfo(
            name=str(row["name"]),
            type=str(row["type"]),
            nullable=bool(row["nullable"]),
            default=row["default"],
            primary_key=bool(row["primary_key"]),
        )
