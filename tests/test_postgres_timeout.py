import time

import pytest
from sqlalchemy import text

from dbarena.actions.errors import QueryError, QueryTimeoutError
from dbarena.actions.postgresql import CLIENT_GRACE_SECONDS, PostgresAdapter


class TestStatementTimeout:
    def test_pg_sleep_exceeding_timeout_raises_timeout_error(self, pg_engine):
        adapter = PostgresAdapter()
        with pytest.raises(QueryTimeoutError):
            adapter.run_sql(pg_engine, "SELECT pg_sleep(5)", row_limit=10, timeout_seconds=1)

    def test_connection_recovers_after_timeout(self, pg_engine):
        adapter = PostgresAdapter()
        with pytest.raises(QueryTimeoutError):
            adapter.run_sql(pg_engine, "SELECT pg_sleep(5)", row_limit=10, timeout_seconds=1)
        result = adapter.run_sql(pg_engine, "SELECT count(*) FROM arena_item", row_limit=10, timeout_seconds=10)
        assert result.rows == [[3]]

    def test_fast_sleep_within_timeout_completes(self, pg_engine):
        adapter = PostgresAdapter()
        result = adapter.run_sql(pg_engine, "SELECT pg_sleep(0.1), 42 AS v", row_limit=10, timeout_seconds=10)
        assert result.rows[0][1] == 42

    def test_timeout_isolation_per_transaction(self, pg_engine):
        adapter = PostgresAdapter()
        with pytest.raises(QueryTimeoutError):
            adapter.run_sql(pg_engine, "SELECT pg_sleep(5)", row_limit=10, timeout_seconds=1)
        with pg_engine.connect() as conn:
            assert conn.execute(text("SHOW statement_timeout")).scalar() == "0"


class TestClientSideGuard:
    def test_guard_cancels_hung_execution(self, pg_engine, monkeypatch):
        monkeypatch.setattr("dbarena.actions.postgresql.CLIENT_GRACE_SECONDS", 0.2)

        def hang(engine, sql, row_limit, timeout_seconds):
            time.sleep(30)
            raise AssertionError("worker should have been abandoned")

        adapter = PostgresAdapter()
        monkeypatch.setattr(adapter, "_execute_sql", hang)
        started = time.perf_counter()
        with pytest.raises(QueryTimeoutError, match="client-side guard"):
            adapter.run_sql(pg_engine, "SELECT 1", row_limit=10, timeout_seconds=0.5)
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0
        assert CLIENT_GRACE_SECONDS == 5.0

    def test_guard_message_reports_budget(self, pg_engine, monkeypatch):
        monkeypatch.setattr("dbarena.actions.postgresql.CLIENT_GRACE_SECONDS", 0.1)

        def hang(engine, sql, row_limit, timeout_seconds):
            time.sleep(10)

        adapter = PostgresAdapter()
        monkeypatch.setattr(adapter, "_execute_sql", hang)
        with pytest.raises(QueryTimeoutError, match="0.6s"):
            adapter.run_sql(pg_engine, "SELECT 1", row_limit=10, timeout_seconds=0.5)


class TestTimeoutErrorMapping:
    def test_syntax_error_is_not_timeout(self, pg_engine):
        adapter = PostgresAdapter()
        with pytest.raises(QueryError) as excinfo:
            adapter.run_sql(pg_engine, "SELECT nonexistent_column FROM arena_item", row_limit=10, timeout_seconds=5)
        assert not isinstance(excinfo.value, QueryTimeoutError)
