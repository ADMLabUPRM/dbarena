import json
import re

from dbarena.models.actions import ActionCall
from dbarena.models.observations import (
    ColumnInfo,
    ErrorCode,
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

DOC_PATH = "docs/observation-format.md"


def _extract_json_blocks(doc: str) -> list[dict]:
    blocks = []
    for match in re.finditer(r"```json\n(.*?)```", doc, re.DOTALL):
        blocks.append(json.loads(match.group(1)))
    return blocks


JSON_EXAMPLE_INDEX = {
    "query_result": 0,
    "table_description": 1,
    "schema_description": 2,
    "error": 3,
    "episode_result": 4,
}


def _extract_markdown_blocks(doc: str) -> list[str]:
    blocks = []
    for match in re.finditer(r"```markdown\n(.*?)```", doc, re.DOTALL):
        blocks.append(match.group(1).rstrip("\n"))
    return blocks


class TestDocumentedExamples:
    def test_doc_exists(self):
        with open(DOC_PATH, encoding="utf-8") as f:
            doc = f.read()
        assert "dbarena-observation/v1" in doc

    def test_documented_query_result_is_builder_output(self):
        action = ActionCall(
            tool="run_sql",
            arguments={"sql": "SELECT l_orderkey, l_extendedprice FROM lineitem LIMIT 2"},
        )
        obs, _ = query_result_observation(
            action,
            ["l_orderkey", "l_extendedprice"],
            [[1, "21168.85"], [1, "45983.16"]],
            row_limit=50,
            duration_ms=4.2,
        )
        with open(DOC_PATH, encoding="utf-8") as f:
            doc = f.read()
        documented = _extract_json_blocks(doc)[JSON_EXAMPLE_INDEX["query_result"]]
        assert obs.to_dict() == documented

    def test_documented_table_description_is_builder_output(self):
        action = ActionCall(tool="describe_table", arguments={"table": "customer"})
        obs = table_description_observation(
            action,
            [
                TableDescription(
                    name="customer",
                    columns=[
                        ColumnInfo(name="c_custkey", type="bigint", nullable=False, primary_key=True),
                        ColumnInfo(name="c_name", type="character varying(25)", nullable=False),
                        ColumnInfo(name="c_acctbal", type="numeric(15,2)", nullable=True, default="NULL"),
                    ],
                )
            ],
            duration_ms=2.1,
        )
        with open(DOC_PATH, encoding="utf-8") as f:
            doc = f.read()
        documented = _extract_json_blocks(doc)[JSON_EXAMPLE_INDEX["table_description"]]
        assert obs.to_dict() == documented

    def test_documented_schema_description_is_builder_output(self):
        action = ActionCall(tool="describe_schema", arguments={})
        obs = schema_description_observation(
            action,
            [
                TableSummary(name="region", column_count=3, columns=["r_regionkey", "r_name", "r_comment"]),
                TableSummary(
                    name="nation", column_count=4, columns=["n_nationkey", "n_name", "n_regionkey", "n_comment"]
                ),
            ],
            duration_ms=8.7,
        )
        with open(DOC_PATH, encoding="utf-8") as f:
            doc = f.read()
        documented = _extract_json_blocks(doc)[JSON_EXAMPLE_INDEX["schema_description"]]
        assert obs.to_dict() == documented

    def test_documented_error_is_builder_output(self):
        action = ActionCall(tool="run_sql", arguments={"sql": "SELECT o_shuffle FROM orders"})
        obs = error_observation(
            action,
            ErrorCode.QUERY_ERROR,
            'column "o_shuffle" does not exist',
            duration_ms=1.3,
        )
        with open(DOC_PATH, encoding="utf-8") as f:
            doc = f.read()
        documented = _extract_json_blocks(doc)[JSON_EXAMPLE_INDEX["error"]]
        assert obs.to_dict() == documented

    def test_documented_episode_result_is_builder_output(self):
        action = ActionCall(tool="submit_final_answer", arguments={"tuples": [[1]]})
        obs = episode_result_observation(action, False, "Expected 2 tuples, submitted 1.", 0.0)
        with open(DOC_PATH, encoding="utf-8") as f:
            doc = f.read()
        documented = _extract_json_blocks(doc)[JSON_EXAMPLE_INDEX["episode_result"]]
        assert obs.to_dict() == documented

    def test_documented_markdown_renders_match(self):
        from dbarena.models.observations import Observation
        from dbarena.observations.markdown import to_markdown

        with open(DOC_PATH, encoding="utf-8") as f:
            doc = f.read()
        json_blocks = _extract_json_blocks(doc)
        md_blocks = _extract_markdown_blocks(doc)
        assert len(json_blocks) == 5
        assert len(md_blocks) == 5
        for envelope, expected_md in zip(json_blocks, md_blocks, strict=True):
            rendered = to_markdown(Observation.from_dict(envelope)).rstrip("\n")
            assert rendered == expected_md, f"markdown mismatch for type {envelope['type']}"
