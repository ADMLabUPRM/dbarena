import datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from dbarena.actions.errors import QueryError
from dbarena.actions.postgresql import PostgresAdapter
from dbarena.models.actions import ActionCall
from dbarena.observations.builders import query_result_observation

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture()
def adapter() -> PostgresAdapter:
    return PostgresAdapter()


@pytest.fixture()
def probe_table(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS episode_probe"))
        conn.execute(text("CREATE TABLE episode_probe (id integer PRIMARY KEY)"))
    yield "episode_probe"
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS episode_probe"))


class TestRunSql:
    def test_complete_select(self, pg_engine, adapter):
        result = adapter.run_sql(
            pg_engine, "SELECT id, name FROM arena_item ORDER BY id", row_limit=50, timeout_seconds=10
        )
        assert result.columns == ["id", "name"]
        assert result.rows == [[1, "alpha"], [2, "beta"], [3, "gamma"]]
        assert result.fetch_limited is False

    def test_values_keep_native_types(self, pg_engine, adapter):
        result = adapter.run_sql(
            pg_engine, "SELECT amount, created FROM arena_item WHERE id = 1", row_limit=50, timeout_seconds=10
        )
        amount, created = result.rows[0]
        assert amount == Decimal("10.50")
        assert created == datetime.date(1995, 9, 1)

    def test_truncation_detection(self, pg_engine, adapter):
        result = adapter.run_sql(
            pg_engine, "SELECT id FROM arena_item ORDER BY id", row_limit=2, timeout_seconds=10
        )
        assert result.fetch_limited is True
        assert len(result.rows) == 3

    def test_full_observation_pipeline(self, pg_engine, adapter):
        result = adapter.run_sql(
            pg_engine, "SELECT id, amount, created FROM arena_item ORDER BY id", row_limit=50, timeout_seconds=10
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

    def test_syntax_error_raises_query_error(self, pg_engine, adapter):
        with pytest.raises(QueryError):
            adapter.run_sql(pg_engine, "SELECT nope FROM missing_table", row_limit=50, timeout_seconds=10)

    def test_transaction_control_rejected(self, pg_engine, adapter):
        for sql in ("COMMIT", "commit;", "END", "BEGIN", "ROLLBACK", "START TRANSACTION", "SAVEPOINT sp1"):
            with pytest.raises(QueryError, match="transaction control"):
                adapter.run_sql(pg_engine, sql, row_limit=50, timeout_seconds=10)

    def test_transaction_control_rejected_across_statements(self, pg_engine, adapter):
        with pytest.raises(QueryError, match="transaction control"):
            adapter.run_sql(pg_engine, "SELECT 1; COMMIT", row_limit=50, timeout_seconds=10)

    def test_transaction_control_hidden_in_comments_and_literals(self, pg_engine, adapter):
        result = adapter.run_sql(
            pg_engine,
            "-- COMMIT\nSELECT 'not COMMIT' /* ROLLBACK */ AS value",
            row_limit=50,
            timeout_seconds=10,
        )
        assert result.rows == [["not COMMIT"]]

    def test_connection_recovers_after_error(self, pg_engine, adapter):
        with pytest.raises(QueryError):
            adapter.run_sql(pg_engine, "SELECT boom", row_limit=50, timeout_seconds=10)
        result = adapter.run_sql(pg_engine, "SELECT count(*) FROM arena_item", row_limit=50, timeout_seconds=10)
        assert result.rows == [[3]]

    def test_write_statement_returns_empty_result(self, pg_engine, adapter):
        adapter.run_sql(
            pg_engine,
            "CREATE TABLE IF NOT EXISTS arena_write_probe (id integer PRIMARY KEY)",
            row_limit=50,
            timeout_seconds=10,
        )
        try:
            inserted = adapter.run_sql(
                pg_engine,
                "INSERT INTO arena_write_probe VALUES (1), (2)",
                row_limit=50,
                timeout_seconds=10,
            )
            assert inserted.columns == []
            assert inserted.rows == []
            assert inserted.fetch_limited is False
            updated = adapter.run_sql(
                pg_engine,
                "UPDATE arena_write_probe SET id = id",
                row_limit=50,
                timeout_seconds=10,
            )
            assert updated.rows == []
            deleted = adapter.run_sql(
                pg_engine,
                "DELETE FROM arena_write_probe",
                row_limit=50,
                timeout_seconds=10,
            )
            assert deleted.rows == []
        finally:
            adapter.run_sql(pg_engine, "DROP TABLE IF EXISTS arena_write_probe", row_limit=50, timeout_seconds=10)


class TestEpisodeConnection:
    def test_select_on_connection_returns_rows(self, pg_engine, adapter):
        with pg_engine.connect() as conn:
            tx = conn.begin()
            result = adapter.run_sql(
                pg_engine,
                "SELECT id, name FROM arena_item ORDER BY id",
                row_limit=50,
                timeout_seconds=10,
                connection=conn,
            )
            assert result.columns == ["id", "name"]
            assert result.rows == [[1, "alpha"], [2, "beta"], [3, "gamma"]]
            assert result.fetch_limited is False
            tx.rollback()

    def test_write_on_connection_returns_empty_and_keeps_transaction_usable(self, pg_engine, adapter, probe_table):
        with pg_engine.connect() as conn:
            tx = conn.begin()
            created = adapter.run_sql(
                pg_engine,
                "CREATE TABLE extra_probe (id integer)",
                row_limit=50,
                timeout_seconds=10,
                connection=conn,
            )
            assert created.rows == [] and created.fetch_limited is False
            inserted = adapter.run_sql(
                pg_engine,
                f"INSERT INTO {probe_table} VALUES (1)",
                row_limit=50,
                timeout_seconds=10,
                connection=conn,
            )
            assert inserted.rows == []
            counted = adapter.run_sql(
                pg_engine,
                f"SELECT count(*) FROM {probe_table}",
                row_limit=50,
                timeout_seconds=10,
                connection=conn,
            )
            assert counted.rows == [[1]]
            tx.rollback()
        with pg_engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('extra_probe')")).scalar() is None

    def test_failing_statement_rolls_back_to_savepoint(self, pg_engine, adapter, probe_table):
        with pg_engine.connect() as conn:
            tx = conn.begin()
            with pytest.raises(QueryError):
                adapter.run_sql(
                    pg_engine,
                    "SELECT boom",
                    row_limit=50,
                    timeout_seconds=10,
                    connection=conn,
                )
            inserted = adapter.run_sql(
                pg_engine,
                f"INSERT INTO {probe_table} VALUES (7)",
                row_limit=50,
                timeout_seconds=10,
                connection=conn,
            )
            assert inserted.rows == []
            counted = adapter.run_sql(
                pg_engine,
                f"SELECT count(*) FROM {probe_table}",
                row_limit=50,
                timeout_seconds=10,
                connection=conn,
            )
            assert counted.rows == [[1]]
            tx.rollback()


class TestDescribeTable:
    def test_existing_table(self, pg_engine, adapter):
        description = adapter.describe_table(pg_engine, "arena_item")
        assert description.found is True
        assert description.name == "arena_item"
        by_name = {c.name: c for c in description.columns}
        assert set(by_name) == {"id", "name", "amount", "created"}
        assert by_name["id"].type == "bigint"
        assert by_name["id"].primary_key is True
        assert by_name["id"].nullable is False
        assert by_name["name"].type == "character varying(25)"
        assert by_name["name"].nullable is False
        assert by_name["amount"].type == "numeric(15,2)"
        assert by_name["amount"].nullable is True

    def test_missing_table(self, pg_engine, adapter):
        description = adapter.describe_table(pg_engine, "ghost_table")
        assert description.found is False
        assert description.columns == []


class TestDescribeTables:
    def test_mixed_found_and_missing(self, pg_engine, adapter):
        descriptions = adapter.describe_tables(pg_engine, ["arena_item", "ghost_table"])
        assert descriptions[0].found is True
        assert descriptions[0].name == "arena_item"
        assert descriptions[1].found is False
        assert descriptions[1].name == "ghost_table"


class TestDescribeSchema:
    def test_lists_seeded_table(self, pg_engine, adapter):
        tables = adapter.describe_schema(pg_engine)
        names = [t.name for t in tables]
        assert "arena_item" in names
        summary = next(t for t in tables if t.name == "arena_item")
        assert summary.column_count == 4
        assert summary.columns == ["id", "name", "amount", "created"]
