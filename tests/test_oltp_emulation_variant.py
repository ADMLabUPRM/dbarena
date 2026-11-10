import importlib.util
import pathlib

import pytest

from dbarena.catalog.validation import parse_task_payload


def _load_seed_tasks():
    path = pathlib.Path(__file__).resolve().parents[1] / "db" / "seed_tasks.py"
    spec = importlib.util.spec_from_file_location("seed_tasks_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_tasks = _load_seed_tasks()
STATE_SEEDS = seed_tasks.STATE_SEEDS


def _seed(task_id: str) -> dict:
    return next(seed for seed in STATE_SEEDS if seed["payload"]["task_id"] == task_id)


BASE = _seed("DBA-O03-001")
VARIANT = _seed("DBA-O03-002")


def _variant_payload_with_dummies() -> dict:
    """Mirror the seeding loop's payload transformation with canned expectations."""
    params = {name: 1 for name in VARIANT["params"]}
    reference = seed_tasks._render_reference(VARIANT["reference"], params)
    return {
        **VARIANT["payload"],
        "goal": VARIANT["payload"]["goal"].format(**params),
        "sqlalchemy_url": "postgresql+psycopg://u:p@h:5432/taskdb",
        "terminal_condition": {
            "expected_tuples": [["1"]],
            "matching": dict(VARIANT["reference"].get("matching", {})),
            "state_checks": [
                {"sql": check["sql"], "expected_tuples": [["1", "x"]], "matching": dict(check["matching"])}
                for check in reference["state_checks"]
            ],
            "rules": reference["rules"],
        },
        "query_timeout": 30,
        "row_limit": 50,
    }


class TestVariantIdentity:
    def test_variant_is_registered_in_state_seeds(self):
        assert VARIANT["payload"]["task_id"] == "DBA-O03-002"

    def test_variant_copies_goal_submission_and_reference(self):
        assert VARIANT["payload"]["goal"] == BASE["payload"]["goal"]
        assert VARIANT["payload"]["submission"] == BASE["payload"]["submission"]
        assert VARIANT["payload"]["category"] == BASE["payload"]["category"]
        assert VARIANT["payload"]["task_type"] == BASE["payload"]["task_type"]
        assert VARIANT["payload"]["max_steps"] == BASE["payload"]["max_steps"]
        assert VARIANT["params"] == BASE["params"]
        assert VARIANT["reference"]["writes"] == BASE["reference"]["writes"]
        assert VARIANT["reference"]["confirmation_sql"] == BASE["reference"]["confirmation_sql"]
        assert VARIANT["reference"]["rules"] == BASE["reference"]["rules"]
        assert VARIANT["reference"]["matching"] == BASE["reference"].get("matching", {})

    def test_original_seed_entry_is_not_mutated(self):
        assert BASE["payload"]["task_id"] == "DBA-O03-001"
        assert len(BASE["reference"]["state_checks"]) == 4
        assert len(BASE["reference"]["rules"]) == 6
        assert VARIANT is not BASE
        assert VARIANT["reference"]["state_checks"] is not BASE["reference"]["state_checks"]

    def test_variant_carries_fourteen_state_checks(self):
        checks = VARIANT["reference"]["state_checks"]
        assert len(checks) == 14
        assert checks[:4] == BASE["reference"]["state_checks"]

    def test_emulation_checks_cover_table_set_and_all_nine_tables(self):
        checks = VARIANT["reference"]["state_checks"][4:]
        assert len(checks) == 10
        assert "information_schema.tables" in checks[0]["sql"]
        assert checks[0]["matching"]["order_sensitive"] is True
        by_table = {
            sql.split("FROM ", 1)[1].split(" AS t")[0]: sql
            for sql in (check["sql"] for check in checks[1:])
        }
        assert set(by_table) == set(seed_tasks.ISOLATION_EMULATION_ORDER_KEYS)
        for table, order_key in seed_tasks.ISOLATION_EMULATION_ORDER_KEYS.items():
            assert f"ORDER BY {order_key}" in by_table[table]
            assert "string_agg(t::text" in by_table[table]
            assert "md5(coalesce" in by_table[table]


class TestVariantPayload:
    def test_payload_parses_with_validation(self):
        record = parse_task_payload(_variant_payload_with_dummies())
        assert record.task_id == "DBA-O03-002"
        assert record.terminal_condition.state_checks is not None
        assert len(record.terminal_condition.state_checks) == 14
        assert len(record.terminal_condition.rules) == 6

    def test_emulation_templates_format_cleanly_with_params(self):
        params = {name: f"p_{name}" for name in VARIANT["params"]}
        rendered = seed_tasks._render_reference(VARIANT["reference"], params)
        assert len(rendered["state_checks"]) == 14
        assert all("{" not in check["sql"] for check in rendered["state_checks"])

    @pytest.mark.parametrize("task_id", ["DBA-O03-001", "DBA-O03-002"])
    def test_seeds_are_unique_in_state_seeds(self, task_id):
        assert [s["payload"]["task_id"] for s in STATE_SEEDS].count(task_id) == 1
