from dbarena.models.actions import ActionCall
from dbarena.models.observations import (
    ColumnInfo,
    EpisodeResultPayload,
    ErrorCode,
    ErrorPayload,
    Observation,
    ObservationType,
    QueryResultPayload,
    SchemaDescriptionPayload,
    TableDescription,
    TableDescriptionPayload,
    TableSummary,
)
from dbarena.observations.markdown import to_markdown

SQL_ACTION = ActionCall(tool="run_sql", arguments={"sql": "SELECT 1"})


class TestQueryResultMarkdown:
    def test_complete_result_states_completeness(self):
        obs = Observation(
            type=ObservationType.QUERY_RESULT,
            payload=QueryResultPayload(
                columns=["k", "v"], rows=[[1, "a"], [2, "b"]], row_count=2, complete=True, row_limit=50
            ),
            action=SQL_ACTION,
        )
        md = to_markdown(obs)
        assert "| k | v |" in md
        assert "| 1 | a |" in md
        assert "| 2 | b |" in md
        assert "Result is complete: all 2 rows are shown." in md
        assert "truncated" not in md

    def test_truncated_result_states_limit(self):
        obs = Observation(
            type=ObservationType.QUERY_RESULT,
            payload=QueryResultPayload(columns=["k"], rows=[[1]], row_count=1, complete=False, row_limit=1),
            action=SQL_ACTION,
        )
        md = to_markdown(obs)
        assert "Result is truncated: showing the first 1 rows (row limit 1)" in md
        assert "not the entire result" in md

    def test_empty_complete_result(self):
        obs = Observation(
            type=ObservationType.QUERY_RESULT,
            payload=QueryResultPayload(columns=["k"], rows=[], row_count=0, complete=True, row_limit=50),
            action=SQL_ACTION,
        )
        md = to_markdown(obs)
        assert "Result is complete: all 0 rows are shown." in md

    def test_null_and_pipe_escaping(self):
        obs = Observation(
            type=ObservationType.QUERY_RESULT,
            payload=QueryResultPayload(columns=["v"], rows=[[None], ["a|b"]], row_count=2, complete=True),
            action=SQL_ACTION,
        )
        md = to_markdown(obs)
        assert "| NULL |" in md
        assert "a\\|b" in md


class TestTableDescriptionMarkdown:
    def test_columns_render_as_table(self):
        obs = Observation(
            type=ObservationType.TABLE_DESCRIPTION,
            payload=TableDescriptionPayload(
                tables=[
                    TableDescription(
                        name="customer",
                        columns=[
                            ColumnInfo(name="c_custkey", type="integer", nullable=False, primary_key=True),
                            ColumnInfo(name="c_mktsegment", type="character(10)", nullable=True, default="BUILDING"),
                        ],
                    )
                ]
            ),
            action=ActionCall(tool="describe_table"),
        )
        md = to_markdown(obs)
        assert "### customer" in md
        assert "| c_custkey | integer | no | - | PK |" in md
        assert "| c_mktsegment | character(10) | yes | BUILDING | - |" in md

    def test_missing_table(self):
        obs = Observation(
            type=ObservationType.TABLE_DESCRIPTION,
            payload=TableDescriptionPayload(tables=[TableDescription(name="ghost", columns=[], found=False)]),
            action=ActionCall(tool="describe_table"),
        )
        md = to_markdown(obs)
        assert "Table `ghost` was not found." in md


class TestSchemaDescriptionMarkdown:
    def test_tables_render_as_table(self):
        obs = Observation(
            type=ObservationType.SCHEMA_DESCRIPTION,
            payload=SchemaDescriptionPayload(
                tables=[TableSummary(name="nation", column_count=4, columns=["n_nationkey", "n_name"])]
            ),
            action=ActionCall(tool="describe_schema"),
        )
        md = to_markdown(obs)
        assert "| nation | 4 | n_nationkey, n_name |" in md


class TestErrorMarkdown:
    def test_code_and_message(self):
        obs = Observation(
            type=ObservationType.ERROR,
            payload=ErrorPayload(code=ErrorCode.TIMEOUT, message="query exceeded 30s"),
            action=SQL_ACTION,
        )
        md = to_markdown(obs)
        assert "**Error** (`timeout`): query exceeded 30s" in md


class TestEpisodeResultMarkdown:
    def test_verdict_and_reward(self):
        obs = Observation(
            type=ObservationType.EPISODE_RESULT,
            payload=EpisodeResultPayload(matched=False, message="expected 3 rows, submitted 1", reward=0.0),
            action=ActionCall(tool="submit_final_answer"),
        )
        md = to_markdown(obs)
        assert "**incorrect**" in md
        assert "Reward: 0.0" in md
        assert "expected 3 rows, submitted 1" in md

    def test_correct_verdict(self):
        obs = Observation(
            type=ObservationType.EPISODE_RESULT,
            payload=EpisodeResultPayload(matched=True, message="ok", reward=1.0),
            action=ActionCall(tool="submit_final_answer"),
        )
        assert "**correct**" in to_markdown(obs)
