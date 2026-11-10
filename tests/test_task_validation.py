import pytest

from dbarena.catalog.errors import TaskValidationError
from dbarena.catalog.validation import parse_task_payload, validate_task_payload


def _valid_task() -> dict:
    return {
        "task_id": "DBA-A03-001",
        "goal": "What percentage of discounted shipment revenue came from promotional parts?",
        "submission": "Call submit_final_answer with exactly one tuple: [percentage as a numeric string].",
        "category": "OLAP",
        "task_type": "IS",
        "max_steps": 12,
        "sqlalchemy_url": "postgresql+psycopg://postgres:pw@taskdb:5432/taskdb",
        "terminal_condition": {
            "expected_tuples": [["42.15"]],
            "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
        },
        "query_timeout": 30,
        "row_limit": 50,
    }


def _errors_for_field(data: dict, field: str) -> list[str]:
    return [message for name, message in validate_task_payload(data) if name == field]


class TestValidTask:
    def test_minimal_valid_task(self):
        task = {
            "task_id": "t",
            "goal": "g",
            "submission": "submit the answer as tuples",
            "category": "OLTP",
            "task_type": "CC",
            "max_steps": 1,
            "sqlalchemy_url": "postgresql://u:p@h/db",
            "terminal_condition": {"expected_tuples": [[1]]},
            "query_timeout": 30,
        }
        assert validate_task_payload(task) == []

    def test_full_task_parses_to_record(self):
        record = parse_task_payload(_valid_task())
        assert record.task_id == "DBA-A03-001"
        assert record.category == "OLAP"
        assert record.task_type == "IS"
        assert record.max_steps == 12
        assert record.query_timeout == 30
        assert record.row_limit == 50
        assert record.terminal_condition.matching.numeric_tolerance == 0.01

    def test_empty_expected_tuples_means_empty_result_expectation(self):
        data = _valid_task()
        data["terminal_condition"]["expected_tuples"] = []
        assert validate_task_payload(data) == []


class TestTaskIdAndGoal:
    def test_missing_goal(self):
        data = _valid_task()
        del data["goal"]
        assert _errors_for_field(data, "goal")

    def test_blank_goal(self):
        data = _valid_task()
        data["goal"] = "   "
        assert _errors_for_field(data, "goal")

    def test_missing_submission(self):
        data = _valid_task()
        del data["submission"]
        assert _errors_for_field(data, "submission")

    def test_blank_submission(self):
        data = _valid_task()
        data["submission"] = "  "
        assert _errors_for_field(data, "submission")

    def test_missing_category(self):
        data = _valid_task()
        del data["category"]
        assert _errors_for_field(data, "category")

    @pytest.mark.parametrize("value", ["oltp", "analytics", "", "OLTPS", None])
    def test_invalid_category(self, value):
        data = _valid_task()
        data["category"] = value
        assert _errors_for_field(data, "category")

    def test_analytics_category_accepted(self):
        data = _valid_task()
        data["category"] = "ANALYTICS"
        assert validate_task_payload(data) == []
        record = parse_task_payload(data)
        assert record.category == "ANALYTICS"

    def test_missing_task_type(self):
        data = _valid_task()
        del data["task_type"]
        assert _errors_for_field(data, "task_type")

    @pytest.mark.parametrize("value", ["read", "write", "", "IS ", None])
    def test_invalid_task_type(self, value):
        data = _valid_task()
        data["task_type"] = value
        assert _errors_for_field(data, "task_type")

    def test_missing_task_id(self):
        data = _valid_task()
        del data["task_id"]
        assert _errors_for_field(data, "task_id")

    def test_oversized_task_id(self):
        data = _valid_task()
        data["task_id"] = "x" * 300
        assert _errors_for_field(data, "task_id")


class TestLimits:
    @pytest.mark.parametrize("value", [0, -3, "five", 2.5, None, True])
    def test_invalid_max_steps(self, value):
        data = _valid_task()
        data["max_steps"] = value
        assert _errors_for_field(data, "max_steps")

    @pytest.mark.parametrize("value", [0, -1, "soon", None, 0.5])
    def test_invalid_query_timeout(self, value):
        data = _valid_task()
        data["query_timeout"] = value
        assert _errors_for_field(data, "query_timeout")

    def test_zero_row_limit(self):
        data = _valid_task()
        data["row_limit"] = 0
        assert _errors_for_field(data, "row_limit")

    def test_null_row_limit_allowed(self):
        data = _valid_task()
        data["row_limit"] = None
        assert validate_task_payload(data) == []


class TestSqlalchemyUrl:
    def test_missing_url(self):
        data = _valid_task()
        del data["sqlalchemy_url"]
        assert _errors_for_field(data, "sqlalchemy_url")

    @pytest.mark.parametrize("value", ["not a url", "://missing-scheme", "postgres\\weird"])
    def test_unparseable_url(self, value):
        data = _valid_task()
        data["sqlalchemy_url"] = value
        assert _errors_for_field(data, "sqlalchemy_url")


class TestTerminalCondition:
    def test_missing_condition(self):
        data = _valid_task()
        del data["terminal_condition"]
        assert validate_task_payload(data)

    def test_condition_not_object(self):
        data = _valid_task()
        data["terminal_condition"] = [[1]]
        assert _errors_for_field(data, "terminal_condition")

    def test_missing_expected_tuples(self):
        data = _valid_task()
        data["terminal_condition"] = {"matching": {}}
        assert any(
            name.startswith("terminal_condition.expected_tuples")
            for name, _ in validate_task_payload(data)
        )

    def test_expected_tuples_not_array(self):
        data = _valid_task()
        data["terminal_condition"]["expected_tuples"] = "one row"
        assert any(
            name.startswith("terminal_condition.expected_tuples")
            for name, _ in validate_task_payload(data)
        )

    def test_tuple_element_not_array(self):
        data = _valid_task()
        data["terminal_condition"]["expected_tuples"] = [[1], "row"]
        assert any(
            name.startswith("terminal_condition.expected_tuples[1]")
            for name, _ in validate_task_payload(data)
        )

    def test_negative_tolerance(self):
        data = _valid_task()
        data["terminal_condition"]["matching"]["numeric_tolerance"] = -0.5
        assert any(
            name.startswith("terminal_condition.matching")
            for name, _ in validate_task_payload(data)
        )

    def test_unsupported_null_handling(self):
        data = _valid_task()
        data["terminal_condition"]["matching"]["null_handling"] = "coalesce"
        assert any(
            name.startswith("terminal_condition.matching")
            for name, _ in validate_task_payload(data)
        )

    def test_order_sensitive_not_boolean(self):
        data = _valid_task()
        data["terminal_condition"]["matching"]["order_sensitive"] = "yes"
        assert any(
            name.startswith("terminal_condition.matching")
            for name, _ in validate_task_payload(data)
        )


class TestStateChecks:
    def _task_with_check(self, **overrides) -> dict:
        data = _valid_task()
        check = {
            "sql": "SELECT count(*) FROM orders",
            "expected_tuples": [[25]],
            "matching": {"numeric_tolerance": 0.01},
        }
        check.update(overrides)
        data["terminal_condition"]["state_checks"] = [check]
        return data

    def test_valid_state_checks_accepted(self):
        data = self._task_with_check()
        assert validate_task_payload(data) == []
        record = parse_task_payload(data)
        assert len(record.terminal_condition.state_checks) == 1
        assert record.terminal_condition.state_checks[0].sql == "SELECT count(*) FROM orders"
        assert record.terminal_condition.state_checks[0].matching.numeric_tolerance == 0.01

    def test_absent_state_checks_accepted(self):
        data = _valid_task()
        assert validate_task_payload(data) == []
        assert parse_task_payload(data).terminal_condition.state_checks == ()

    def test_state_checks_not_array(self):
        data = self._task_with_check()
        data["terminal_condition"]["state_checks"] = "nope"
        assert _errors_for_field(data, "terminal_condition.state_checks")

    def test_check_entry_not_object(self):
        data = self._task_with_check()
        data["terminal_condition"]["state_checks"] = ["SELECT 1"]
        assert _errors_for_field(data, "terminal_condition.state_checks[0]")

    def test_check_missing_sql(self):
        data = self._task_with_check()
        del data["terminal_condition"]["state_checks"][0]["sql"]
        assert _errors_for_field(data, "terminal_condition.state_checks[0].sql")

    def test_check_blank_sql(self):
        data = self._task_with_check(sql="   ")
        assert _errors_for_field(data, "terminal_condition.state_checks[0].sql")

    def test_check_sql_not_string(self):
        data = self._task_with_check(sql=42)
        assert _errors_for_field(data, "terminal_condition.state_checks[0].sql")

    def test_check_missing_expected_tuples(self):
        data = self._task_with_check()
        del data["terminal_condition"]["state_checks"][0]["expected_tuples"]
        assert any(
            name == "terminal_condition.state_checks[0].expected_tuples"
            for name, _ in validate_task_payload(data)
        )

    def test_check_expected_tuples_not_array(self):
        data = self._task_with_check(expected_tuples="rows")
        assert any(
            name == "terminal_condition.state_checks[0].expected_tuples"
            for name, _ in validate_task_payload(data)
        )

    def test_check_expected_tuple_element_not_array(self):
        data = self._task_with_check(expected_tuples=[[1], "row"])
        assert any(
            name == "terminal_condition.state_checks[0].expected_tuples[1]"
            for name, _ in validate_task_payload(data)
        )

    def test_check_invalid_matching_rules(self):
        data = self._task_with_check(matching={"null_handling": "coalesce"})
        assert _errors_for_field(data, "terminal_condition.state_checks[0].matching.null_handling")

    def test_check_matching_not_object(self):
        data = self._task_with_check(matching="strict")
        assert _errors_for_field(data, "terminal_condition.state_checks[0].matching")

    def test_error_names_first_failing_check_only_path(self):
        data = self._task_with_check()
        second = {"sql": "", "expected_tuples": [[1]]}
        data["terminal_condition"]["state_checks"].append(second)
        names = {name for name, _ in validate_task_payload(data)}
        assert "terminal_condition.state_checks[0].expected_tuples" not in names
        assert "terminal_condition.state_checks[1].sql" in names


class TestRules:
    def _task_with_rule(self, **overrides) -> dict:
        data = _valid_task()
        rule = {
            "sql": "SELECT count(*) = 1 FROM orders WHERE o_orderkey = 60001",
            "description": "exactly one order",
        }
        rule.update(overrides)
        data["terminal_condition"]["rules"] = [rule]
        return data

    def test_valid_rules_accepted(self):
        data = self._task_with_rule()
        assert validate_task_payload(data) == []
        record = parse_task_payload(data)
        assert len(record.terminal_condition.rules) == 1
        assert record.terminal_condition.rules[0].description == "exactly one order"

    def test_rules_without_description_accepted(self):
        data = self._task_with_rule(description=None)
        del data["terminal_condition"]["rules"][0]["description"]
        assert validate_task_payload(data) == []
        assert parse_task_payload(data).terminal_condition.rules[0].description is None

    def test_rules_with_null_description_accepted(self):
        data = self._task_with_rule(description=None)
        assert validate_task_payload(data) == []

    def test_absent_rules_accepted(self):
        data = _valid_task()
        assert validate_task_payload(data) == []
        assert parse_task_payload(data).terminal_condition.rules == ()

    def test_rules_not_array(self):
        data = self._task_with_rule()
        data["terminal_condition"]["rules"] = "nope"
        assert _errors_for_field(data, "terminal_condition.rules")

    def test_rule_entry_not_object(self):
        data = self._task_with_rule()
        data["terminal_condition"]["rules"] = ["SELECT 1"]
        assert _errors_for_field(data, "terminal_condition.rules[0]")

    def test_rule_missing_sql(self):
        data = self._task_with_rule()
        del data["terminal_condition"]["rules"][0]["sql"]
        assert _errors_for_field(data, "terminal_condition.rules[0].sql")

    def test_rule_blank_sql(self):
        data = self._task_with_rule(sql="   ")
        assert _errors_for_field(data, "terminal_condition.rules[0].sql")

    def test_rule_sql_not_string(self):
        data = self._task_with_rule(sql=42)
        assert _errors_for_field(data, "terminal_condition.rules[0].sql")

    def test_rule_non_string_description(self):
        data = self._task_with_rule(description=5)
        assert _errors_for_field(data, "terminal_condition.rules[0].description")

    def test_second_rule_error_names_second_path(self):
        data = self._task_with_rule()
        data["terminal_condition"]["rules"].append({"sql": ""})
        names = {name for name, _ in validate_task_payload(data)}
        assert "terminal_condition.rules[1].sql" in names
        assert "terminal_condition.rules[0].sql" not in names

    def test_type_agnostic_rules_accepted(self):
        data = self._task_with_rule()
        data["category"] = "OLAP"
        data["task_type"] = "IS"
        assert validate_task_payload(data) == []


class TestParseRejects:
    def test_parse_raises_with_field_errors(self):
        data = _valid_task()
        data["max_steps"] = 0
        data["goal"] = ""
        data["category"] = "bogus"
        data["task_type"] = "bogus"
        with pytest.raises(TaskValidationError) as excinfo:
            parse_task_payload(data)
        fields = {name for name, _ in excinfo.value.errors}
        assert fields == {"max_steps", "goal", "category", "task_type"}
