from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from dbarena.actions.base import EngineAdapter, SqlResult
from dbarena.actions.errors import QueryError, QueryTimeoutError
from dbarena.actions.sqlguard import assert_no_transaction_control
from dbarena.models.observations import ColumnInfo, TableDescription, TableSummary

CLIENT_GRACE_SECONDS = 5.0

_COLUMNS_SQL = """
SELECT c.column_name AS name,
       c.data_type AS type,
       c.is_nullable AS nullable,
       c.column_default AS default,
       EXISTS (
           SELECT 1 FROM duckdb_constraints() p
           WHERE p.database_name = c.database_name
             AND p.schema_name = c.schema_name
             AND p.table_name = c.table_name
             AND p.constraint_type = 'PRIMARY KEY'
             AND list_contains(p.constraint_column_names, c.column_name)
       ) AS primary_key
FROM duckdb_columns() c
WHERE c.database_name = current_database()
  AND c.schema_name = current_schema()
  AND c.table_name = :relation
ORDER BY c.column_index
"""

_SCHEMA_SQL = """
SELECT t.table_name AS name,
       count(c.column_name) AS column_count,
       list(c.column_name ORDER BY c.column_index) AS columns
FROM duckdb_tables() t
JOIN duckdb_columns() c
  ON c.database_name = t.database_name
 AND c.schema_name = t.schema_name
 AND c.table_name = t.table_name
WHERE t.database_name = current_database()
  AND t.schema_name = current_schema()
GROUP BY t.table_name
ORDER BY t.table_name
"""


def _dbapi_connection(connection: Connection) -> Any:
    """Return the underlying DBAPI connection for interrupt support, if reachable."""
    try:
        raw = connection.connection.dbapi_connection  # type: ignore[union-attr]
    except Exception:
        return None
    for candidate in (raw, getattr(raw, "_obj", None), getattr(raw, "conn", None)):
        if candidate is not None and hasattr(candidate, "interrupt"):
            return candidate
    return None


def _is_interrupt(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    if orig is not None:
        if type(orig).__name__ in ("InterruptException", "InterruptedError"):
            return True
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pg_code", None)
        if sqlstate == "57014":
            return True
        message = str(orig)
    else:
        message = str(exc)
    return "interrupt" in message.lower()


class DuckDbAdapter(EngineAdapter):
    dialect_name = "duckdb"

    def run_sql(
        self,
        engine: Engine,
        sql: str,
        row_limit: int,
        timeout_seconds: float,
        connection: Connection | None = None,
    ) -> SqlResult:
        assert_no_transaction_control(sql)
        holder: dict[str, Any] = {}
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            if connection is not None:
                future: Future = executor.submit(
                    self._execute_on_connection, connection, sql, row_limit, holder
                )
            else:
                future = executor.submit(
                    self._execute_sql, engine, sql, row_limit, holder
                )
            try:
                return future.result(timeout=max(0.1, timeout_seconds))
            except FutureTimeoutError:
                raw = holder.get("dbapi")
                if raw is not None:
                    try:
                        raw.interrupt()
                    except Exception:
                        pass
                try:
                    return future.result(timeout=CLIENT_GRACE_SECONDS)
                except FutureTimeoutError:
                    future.cancel()
                    raise QueryTimeoutError(
                        f"client-side guard interrupted query after {timeout_seconds:.1f}s"
                    ) from None
        finally:
            executor.shutdown(wait=False)

    def _execute_on_connection(
        self, connection: Connection, sql: str, row_limit: int, holder: dict[str, Any]
    ) -> SqlResult:
        holder["dbapi"] = _dbapi_connection(connection)
        try:
            # DuckDB has no SAVEPOINT support; a failed statement leaves the
            # transaction usable (per-statement atomicity), so no nested
            # savepoint is needed to keep the episode transaction healthy.
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

    def _execute_sql(self, engine: Engine, sql: str, row_limit: int, holder: dict[str, Any]) -> SqlResult:
        try:
            with engine.connect() as conn:
                holder["dbapi"] = _dbapi_connection(conn)
                with conn.begin():
                    result = conn.execute(text(sql))
                    return self._collect_rows(result, row_limit)
        except DBAPIError as exc:
            self._translate_dbapi_error(exc)

    def _translate_dbapi_error(self, exc: DBAPIError) -> None:
        orig = getattr(exc, "orig", None)
        message = str(orig) if orig is not None else str(exc)
        if _is_interrupt(exc):
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
