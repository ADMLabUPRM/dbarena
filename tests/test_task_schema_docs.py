import json
import re
from pathlib import Path

from dbarena.catalog.validation import parse_task_payload, validate_task_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "task-schema.md"


def _documented_examples() -> list[dict]:
    doc = DOC_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", doc, re.DOTALL)
    assert blocks, "task-schema.md must contain at least one JSON example block"
    return [json.loads(block) for block in blocks]


def _documented_example() -> dict:
    return _documented_examples()[0]


class TestDocumentedTaskSchema:
    def test_every_documented_example_passes_validation(self):
        for example in _documented_examples():
            assert validate_task_payload(example) == []

    def test_state_check_example_parses_with_checks(self):
        examples = _documented_examples()
        assert len(examples) >= 2
        condition = parse_task_payload(examples[1]).terminal_condition
        assert len(condition.state_checks) >= 1
        assert all(check.sql.strip() for check in condition.state_checks)
    def test_documented_example_passes_validation(self):
        example = _documented_example()
        assert validate_task_payload(example) == []

    def test_documented_example_parses_to_record(self):
        record = parse_task_payload(_documented_example())
        assert record.task_id == "DBA-A03-001"
        assert record.max_steps == 12
        assert record.query_timeout == 30
        assert record.row_limit == 50
        assert record.terminal_condition.expected_tuples == [["38.43"]]
        assert record.terminal_condition.matching.numeric_tolerance == 0.01
        assert record.terminal_condition.matching.order_sensitive is False
        assert record.terminal_condition.matching.null_handling == "exact"

    def test_documented_example_round_trips_through_record(self):
        record = parse_task_payload(_documented_example())
        reparsed = parse_task_payload(
            {
                "task_id": record.task_id,
                "goal": record.goal,
                "submission": record.submission,
                "category": record.category,
                "task_type": record.task_type,
                "max_steps": record.max_steps,
                "sqlalchemy_url": record.sqlalchemy_url,
                "terminal_condition": record.terminal_condition.to_dict(),
                "query_timeout": record.query_timeout,
                "row_limit": record.row_limit,
            }
        )
        assert reparsed == record

    def test_doc_documents_all_required_fields(self):
        doc = DOC_PATH.read_text(encoding="utf-8")
        required = (
            "task_id",
            "goal",
            "submission",
            "category",
            "task_type",
            "max_steps",
            "sqlalchemy_url",
            "terminal_condition",
            "query_timeout",
        )
        for field in required:
            assert f"`{field}`" in doc
