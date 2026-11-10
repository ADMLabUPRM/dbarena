import pytest
from sqlalchemy import create_engine

from dbarena.actions.base import (
    EngineAdapter,
    FakeAdapter,
    SqlResult,
    UnsupportedEngineError,
)
from dbarena.actions.registry import (
    ACTION_NAMES,
    RUN_RAG_QUERY,
    RUN_SQL,
    SUBMIT_FINAL_ANSWER,
    EngineRegistry,
    dialect_of,
)


def _fake_adapter() -> FakeAdapter:
    return FakeAdapter(
        result=SqlResult(columns=["n"], rows=[["r1"], ["r2"]], fetch_limited=False),
        tables=[],
        schema=[],
    )


class TestEngineRegistry:
    def test_dispatch_to_registered_adapter(self):
        registry = EngineRegistry()
        adapter = _fake_adapter()
        registry.register("postgresql", adapter)
        assert registry.adapter_for("postgresql+psycopg://u:p@localhost/db") is adapter

    def test_unsupported_engine_returns_null_adapter(self):
        registry = EngineRegistry()
        resolved = registry.adapter_for("mysql+pymysql://u:p@localhost/db")
        assert isinstance(resolved, EngineAdapter)
        with pytest.raises(UnsupportedEngineError, match="mysql"):
            resolved.run_sql(None, "SELECT 1", 10, 5.0)

    def test_unsupported_engine_fails_for_every_action(self):
        registry = EngineRegistry()
        resolved = registry.adapter_for("duckdb:///:memory:")
        with pytest.raises(UnsupportedEngineError):
            resolved.describe_table(None, "t")
        with pytest.raises(UnsupportedEngineError):
            resolved.describe_tables(None, ["t"])
        with pytest.raises(UnsupportedEngineError):
            resolved.describe_schema(None)

    def test_registered_adapter_receives_calls(self):
        registry = EngineRegistry()
        adapter = _fake_adapter()
        registry.register("fake", adapter)
        engine = create_engine("sqlite:///:memory:")
        result = registry.adapter_for("fake://x").run_sql(engine, "SELECT 1", 25, 9.0)
        assert result.columns == ["n"]
        assert result.rows == [["r1"], ["r2"]]
        assert adapter.calls[0] == (
            "run_sql",
            {"sql": "SELECT 1", "row_limit": 25, "timeout": 9.0, "connection": None},
        )

    def test_dialect_of(self):
        assert dialect_of("postgresql+psycopg://u:p@h/db") == "postgresql"
        assert dialect_of("sqlite:///f.db") == "sqlite"

    def test_action_names_include_all_six(self):
        assert ACTION_NAMES == (
            "run_sql",
            "describe_table",
            "describe_tables",
            "describe_schema",
            "submit_final_answer",
            "run_rag_query",
        )
        assert RUN_SQL in ACTION_NAMES and SUBMIT_FINAL_ANSWER in ACTION_NAMES
        assert RUN_RAG_QUERY in ACTION_NAMES
