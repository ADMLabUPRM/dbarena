import datetime
import os
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dbarena.actions.duckdb import DuckDbAdapter
from dbarena.actions.errors import QueryError, QueryTimeoutError
from dbarena.actions.registry import default_registry
from dbarena.env.episode import EpisodeTransaction
from dbarena.models.actions import ActionCall
from dbarena.observations.builders import query_result_observation

SEED_SQL = """
CREATE TABLE arena_item (
    id bigint PRIMARY KEY,
    name character varying(25) NOT NULL,
    amount decimal(15,2),
    created date
);
INSERT INTO arena_item VALUES
    (1, 'alpha', 10.50, DATE '1995-09-01'),
    (2, 'beta', NULL, DATE '1995-09-02'),
    (3, 'gamma', 42.00, DATE '1995-09-03');
CREATE TABLE order_line (
    ol_orderkey bigint,
    ol_linenumber bigint,
    ol_amount decimal(15,2),
    PRIMARY KEY (ol_orderkey, ol_linenumber)
);
INSERT INTO order_line VALUES (1, 1, 100.50), (1, 2, 50.25);
"""


@pytest.fixture(scope="module")
def duckdb_url(tmp_path_factory) -> str:
    path = os.path.join(tmp_path_factory.mktemp("olapdb"), "fixture.duckdb")
    engine = create_engine(f"duckdb:///{path}")
    with engine.begin() as conn:
        for statement in SEED_SQL.split(";"):
            if statement.strip():
                conn.execute(text(statement))
    engine.dispose()
    return f"duckdb:///{path}"


@pytest.fixture(scope="module")
def duck_engine(duckdb_url: str) -> Engine:
    return create_engine(duckdb_url)


@pytest.fixture()
def adapter() -> DuckDbAdapter:
    return DuckDbAdapter()


class TestRegistryDispatch:
    def test_duckdb_url_dispatches_to_duckdb_adapter(self):
        adapter = default_registry().adapter_for("duckdb:///data/olap-tpch.duckdb")
        assert isinstance(adapter, DuckDbAdapter)
        assert adapter.dialect_name == "duckdb"

    def test_unknown_engine_still_unsupported(self):
        adapter = default_registry().adapter_for("mysql+pymysql://u:p@localhost/db")
        assert adapter.__class__.__name__ == "NullAdapter"


class TestRunSql:
    def test_complete_select(self, duck_engine, adapter):
        result = adapter.run_sql(
            duck_engine, "SELECT id, name FROM arena_item ORDER BY id", row_limit=50, timeout_seconds=10
        )
        assert result.columns == ["id", "name"]
        assert result.rows == [[1, "alpha"], [2, "beta"], [3, "gamma"]]
        assert result.fetch_limited is False

    def test_values_keep_native_types(self, duck_engine, adapter):
        result = adapter.run_sql(
            duck_engine, "SELECT amount, created FROM arena_item WHERE id = 1", row_limit=50, timeout_seconds=10
        )
        amount, created = result.rows[0]
        assert amount == Decimal("10.50")
        assert created == datetime.date(1995, 9, 1)

    def test_truncation_detection(self, duck_engine, adapter):
        result = adapter.run_sql(
            duck_engine, "SELECT id FROM arena_item ORDER BY id", row_limit=2, timeout_seconds=10
        )
        assert result.fetch_limited is True
        assert len(result.rows) == 3

    def test_full_observation_pipeline(self, duck_engine, adapter):
        result = adapter.run_sql(
            duck_engine, "SELECT id, amount, created FROM arena_item ORDER BY id", row_limit=50, timeout_seconds=10
        )
        obs, note = query_result_observation(
            ActionCall(tool="run_sql", arguments={"sql": "SELECT"}),
            result.columns,
            result.rows,
            row_limit=50,
        )
        assert obs.payload.complete is True
        assert obs.payload.rows == [
            [1, "10.50", "1995-09-01"],
            [2, None, "1995-09-02"],
            [3, "42.00", "1995-09-03"],
        ]
        assert note is None

    def test_syntax_error_raises_query_error(self, duck_engine, adapter):
        with pytest.raises(QueryError):
            adapter.run_sql(duck_engine, "SELECT nope FROM missing_table", row_limit=50, timeout_seconds=10)

    def test_transaction_control_rejected(self, duck_engine, adapter):
        for sql in ("COMMIT", "commit;", "END", "BEGIN", "ROLLBACK", "BEGIN TRANSACTION"):
            with pytest.raises(QueryError, match="transaction control"):
                adapter.run_sql(duck_engine, sql, row_limit=50, timeout_seconds=10)

    def test_transaction_control_rejected_across_statements(self, duck_engine, adapter):
        with pytest.raises(QueryError, match="transaction control"):
            adapter.run_sql(duck_engine, "SELECT 1; COMMIT", row_limit=50, timeout_seconds=10)

    def test_transaction_control_hidden_in_comments_and_literals(self, duck_engine, adapter):
        result = adapter.run_sql(
            duck_engine,
            "-- COMMIT\nSELECT 'not COMMIT' /* ROLLBACK */ AS value",
            row_limit=50,
            timeout_seconds=10,
        )
        assert result.rows == [["not COMMIT"]]

    def test_connection_recovers_after_error(self, duck_engine, adapter):
        with pytest.raises(QueryError):
            adapter.run_sql(duck_engine, "SELECT boom", row_limit=50, timeout_seconds=10)
        result = adapter.run_sql(duck_engine, "SELECT count(*) FROM arena_item", row_limit=50, timeout_seconds=10)
        assert result.rows == [[3]]

    def test_write_statement_returns_count_marker(self, duck_engine, adapter):
        # DuckDB marks DML with its native Count column (PG returns no rows);
        # the adapter exposes the engine's own result shape.
        adapter.run_sql(
            duck_engine,
            "CREATE OR REPLACE TABLE arena_write_probe (id integer PRIMARY KEY)",
            row_limit=50,
            timeout_seconds=10,
        )
        try:
            inserted = adapter.run_sql(
                duck_engine,
                "INSERT INTO arena_write_probe VALUES (1), (2)",
                row_limit=50,
                timeout_seconds=10,
            )
            assert inserted.columns == ["Count"]
            assert inserted.rows == [[2]]
            deleted = adapter.run_sql(
                duck_engine,
                "DELETE FROM arena_write_probe",
                row_limit=50,
                timeout_seconds=10,
            )
            assert deleted.rows == [[2]]
        finally:
            adapter.run_sql(duck_engine, "DROP TABLE IF EXISTS arena_write_probe", row_limit=50, timeout_seconds=10)

    def test_timeout_interrupts_long_query(self, duck_engine, adapter):
        with pytest.raises(QueryTimeoutError):
            adapter.run_sql(
                duck_engine,
                "WITH RECURSIVE t(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM t) SELECT count(*) FROM t",
                row_limit=50,
                timeout_seconds=1,
            )
        # the engine pool stays usable after an interrupt
        result = adapter.run_sql(duck_engine, "SELECT count(*) FROM arena_item", row_limit=50, timeout_seconds=10)
        assert result.rows == [[3]]


class TestEpisodeConnection:
    def test_writes_visible_and_rollback_restores(self, duck_engine, adapter):
        episode = EpisodeTransaction(duck_engine, 10)
        connection = episode.connection
        try:
            connection.execute(text("INSERT INTO arena_item VALUES (99, 'episode', 1.00, DATE '1995-09-09')"))
            seen = connection.execute(text("SELECT count(*) FROM arena_item")).scalar()
            assert seen == 4
        finally:
            episode.rollback()
        with duck_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM arena_item")).scalar() == 3

    def test_failed_statement_keeps_transaction_usable(self, duck_engine, adapter):
        episode = EpisodeTransaction(duck_engine, 10)
        connection = episode.connection
        try:
            connection.execute(text("INSERT INTO arena_item VALUES (98, 'kept', 2.00, DATE '1995-09-08')"))
            with pytest.raises(QueryError):
                adapter.run_sql(
                    duck_engine, "SELECT bogus FROM arena_item", row_limit=50, timeout_seconds=10,
                    connection=connection,
                )
            connection.execute(text("INSERT INTO arena_item VALUES (97, 'after', 3.00, DATE '1995-09-07')"))
            assert connection.execute(text("SELECT count(*) FROM arena_item")).scalar() == 5
        finally:
            episode.rollback()
        with duck_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM arena_item")).scalar() == 3

    def test_artifact_table_created_mid_episode_rolls_back(self, duck_engine, adapter):
        # CC-style artifacts: CREATE TABLE + INSERT inside the episode
        # transaction, visible mid-episode, gone after rollback (transactional
        # DDL keeps the fixture pristine).
        episode = EpisodeTransaction(duck_engine, 10)
        try:
            episode.connection.execute(text("CREATE TABLE arena_artifact (id integer PRIMARY KEY)"))
            episode.connection.execute(text("INSERT INTO arena_artifact VALUES (1)"))
            seen = episode.connection.execute(text("SELECT count(*) FROM arena_artifact")).scalar()
            assert seen == 1
        finally:
            episode.rollback()
        with duck_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'arena_artifact'")
            ).scalar()
            assert exists == 0

    def test_timeout_guard_works_on_episode_connection(self, duck_engine, adapter):
        # DuckDB engine semantics: an interrupt aborts the whole transaction
        # (Postgres cancels only the statement), so later statements surface
        # structured errors until rollback — and nothing persists either way.
        episode = EpisodeTransaction(duck_engine, 10)
        try:
            with pytest.raises(QueryTimeoutError):
                adapter.run_sql(
                    duck_engine,
                    "WITH RECURSIVE t(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM t) SELECT count(*) FROM t",
                    row_limit=50,
                    timeout_seconds=1,
                    connection=episode.connection,
                )
            with pytest.raises(QueryError, match="aborted"):
                adapter.run_sql(
                    duck_engine, "SELECT count(*) FROM arena_item", row_limit=50, timeout_seconds=10,
                    connection=episode.connection,
                )
        finally:
            episode.rollback()
        with duck_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM arena_item")).scalar() == 3


class TestDescribeTable:
    def test_composite_primary_key_reported(self, duck_engine, adapter):
        description = adapter.describe_table(duck_engine, "order_line")
        assert description.found is True
        assert [c.name for c in description.columns] == ["ol_orderkey", "ol_linenumber", "ol_amount"]
        assert [c.name for c in description.columns if c.primary_key] == ["ol_orderkey", "ol_linenumber"]

    def test_column_metadata(self, duck_engine, adapter):
        description = adapter.describe_table(duck_engine, "arena_item")
        amount = [c for c in description.columns if c.name == "amount"][0]
        assert amount.type.startswith("DECIMAL")
        assert amount.nullable is True
        name = [c for c in description.columns if c.name == "name"][0]
        assert name.nullable is False

    def test_missing_table_not_found(self, duck_engine, adapter):
        description = adapter.describe_table(duck_engine, "missing_table")
        assert description.found is False
        assert description.columns == []


class TestDescribeSchema:
    def test_lists_user_tables_with_columns(self, duck_engine, adapter):
        tables = adapter.describe_schema(duck_engine)
        names = [t.name for t in tables]
        assert "arena_item" in names and "order_line" in names
        line = [t for t in tables if t.name == "order_line"][0]
        assert line.column_count == 3
        assert line.columns == ["ol_orderkey", "ol_linenumber", "ol_amount"]

    def test_system_schemas_excluded(self, duck_engine, adapter):
        names = [t.name for t in adapter.describe_schema(duck_engine)]
        assert not any(name.lower().startswith(("pg_", "information_")) for name in names)
        assert len(names) == 2
