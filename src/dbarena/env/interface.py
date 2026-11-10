from dataclasses import replace

from dbarena.actions.registry import (
    DESCRIBE_SCHEMA,
    DESCRIBE_TABLE,
    DESCRIBE_TABLES,
    RUN_RAG_QUERY,
    RUN_SQL,
    SUBMIT_FINAL_ANSWER,
)
from dbarena.models.actions import ActionCall
from dbarena.models.observations import (
    ColumnInfo,
    ErrorCode,
    Observation,
    ObservationType,
    TableDescription,
    TableSummary,
)
from dbarena.observations.builders import (
    episode_result_observation,
    error_observation,
    query_result_observation,
    schema_description_observation,
    table_description_observation,
)


def _example_query_result() -> Observation:
    action = ActionCall(tool=RUN_SQL, arguments={"sql": "SELECT r_name FROM region ORDER BY r_name LIMIT 2"})
    observation, _ = query_result_observation(action, ["r_name"], [["AFRICA"], ["AMERICA"]], row_limit=50)
    return observation


def _example_table_description() -> Observation:
    action = ActionCall(tool=DESCRIBE_TABLE, arguments={"table": "region"})
    description = TableDescription(
        name="region",
        columns=[
            ColumnInfo(name="r_regionkey", type="integer", nullable=False, primary_key=True),
            ColumnInfo(name="r_name", type="character(25)", nullable=False),
            ColumnInfo(name="r_comment", type="varchar(152)", nullable=True),
        ],
    )
    return table_description_observation(action, [description])


def _example_schema_description() -> Observation:
    action = ActionCall(tool=DESCRIBE_SCHEMA, arguments={})
    summary = TableSummary(name="region", column_count=3, columns=["r_regionkey", "r_name", "r_comment"])
    return schema_description_observation(action, [summary])


def _example_episode_result() -> Observation:
    action = ActionCall(tool=SUBMIT_FINAL_ANSWER, arguments={"tuples": [["42.00"]]})
    observation = episode_result_observation(action, True, "All expected tuples were matched.", 1.0)
    return replace(observation, reward=1.0, terminated=True, truncated=False, step_number=2)


def _example_error() -> Observation:
    action = ActionCall(tool=RUN_SQL, arguments={"sql": "SELECT nope FROM region"})
    observation = error_observation(action, ErrorCode.QUERY_ERROR, 'column "nope" does not exist')
    return replace(observation, reward=0.0, terminated=False, truncated=False, step_number=1)


def action_specs() -> list[dict]:
    return [
        {
            "tool": RUN_SQL,
            "summary": "Execute a SQL statement against the target database and receive its result rows "
            "(row-limited, with an explicit completeness flag).",
            "arguments": {
                "sql": "required string: the SQL statement to execute; transaction control statements "
                "(COMMIT, END, BEGIN, START TRANSACTION, SAVEPOINT, RELEASE, ROLLBACK, ABORT, "
                "PREPARE TRANSACTION) are rejected, and episode writes are isolated and rolled back "
                "automatically when the episode ends"
            },
            "example": {"tool": RUN_SQL, "arguments": {"sql": "SELECT r_name FROM region ORDER BY r_name LIMIT 2"}},
        },
        {
            "tool": DESCRIBE_TABLE,
            "summary": "Describe one table: column names, types, nullability, defaults, and primary key.",
            "arguments": {"table": "required string: name of the table to describe"},
            "example": {"tool": DESCRIBE_TABLE, "arguments": {"table": "region"}},
        },
        {
            "tool": DESCRIBE_TABLES,
            "summary": "Describe several tables in one call; tables that do not exist are reported with found=false.",
            "arguments": {"tables": "required array of strings: the table names to describe"},
            "example": {"tool": DESCRIBE_TABLES, "arguments": {"tables": ["region", "nation"]}},
        },
        {
            "tool": DESCRIBE_SCHEMA,
            "summary": "List every table in the target schema with column counts and column names.",
            "arguments": {},
            "example": {"tool": DESCRIBE_SCHEMA, "arguments": {}},
        },
        {
            "tool": SUBMIT_FINAL_ANSWER,
            "summary": "Submit the final answer and end the episode. Tasks verified by reference tuples "
            "take 'tuples' (matched against the confirmation tuples and any declared state checks); "
            "tasks verified by an LLM judge take 'answer' (a natural-language response judged against "
            "the expected result — only a pass/fail verdict is returned, with no feedback).",
            "arguments": {
                "tuples": "required for tuple-verified tasks: an array of tuples (arrays of values)",
                "answer": "required for judge-verified tasks: a non-empty natural-language answer "
                "satisfying the task goal (for example, an explanation citing the supporting incident ids)",
            },
            "example": {"tool": SUBMIT_FINAL_ANSWER, "arguments": {"tuples": [["42.00"]]}},
        },
        {
            "tool": RUN_RAG_QUERY,
            "summary": "Execute a retrieval statement that needs a query embedding: the environment "
            "embeds the supplied text with the configured model and injects the resulting vector at "
            "the ':query_vector' placeholder before running the statement. The vector is never "
            "returned to the agent.",
            "arguments": {
                "sql": "required string: the statement to execute; it must contain the ':query_vector' "
                "placeholder where the query embedding belongs (use it like a pgvector value, e.g. "
                "'ORDER BY embedding <=> :query_vector')",
                "text": "required string: the text to embed and substitute at the placeholder",
            },
            "example": {
                "tool": RUN_RAG_QUERY,
                "arguments": {
                    "sql": (
                        "SELECT d.incident_id FROM documents d "
                        "JOIN embeddings e ON e.incident_id = d.incident_id "
                        "WHERE d.product_version = '4.2.0' "
                        "ORDER BY e.embedding <=> :query_vector LIMIT 5"
                    ),
                    "text": "connection pool exhaustion under load",
                },
            },
        },
    ]


def observation_space_catalog() -> list[dict]:
    entries = [
        (
            ObservationType.QUERY_RESULT,
            "Result rows of a run_sql action, with an explicit completeness statement.",
            _example_query_result(),
        ),
        (
            ObservationType.TABLE_DESCRIPTION,
            "Column layout of one or more tables (types, nullability, defaults, primary keys).",
            _example_table_description(),
        ),
        (
            ObservationType.SCHEMA_DESCRIPTION,
            "Tables of the target schema with column counts and column names.",
            _example_schema_description(),
        ),
        (
            ObservationType.EPISODE_RESULT,
            "Outcome of a submitted final answer: matched against the terminal condition, with the reward.",
            _example_episode_result(),
        ),
        (
            ObservationType.ERROR,
            "Structured failure of an action; never contains fabricated result data.",
            _example_error(),
        ),
    ]
    return [
        {"type": obs_type.value, "description": description, "example": observation.to_dict()}
        for obs_type, description, observation in entries
    ]
