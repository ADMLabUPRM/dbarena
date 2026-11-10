from typing import Any

from sqlalchemy.engine import make_url

from dbarena.catalog.errors import TaskValidationError
from dbarena.catalog.records import TaskRecord
from dbarena.models.terminal import (
    NULL_HANDLING_EXACT,
    TerminalCondition,
)

MAX_ID_LENGTH = 256

CATEGORIES = ("OLTP", "OLAP", "ANALYTICS", "RAG")
TASK_TYPES = ("NAV", "IS", "CC", "MIX")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_integral(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_task_payload(data: Any) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    if not isinstance(data, dict):
        return [("payload", "task payload must be a JSON object")]

    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        errors.append(("task_id", "must be a non-empty string"))
    elif len(task_id) > MAX_ID_LENGTH:
        errors.append(("task_id", f"must be at most {MAX_ID_LENGTH} characters"))

    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        errors.append(("goal", "must be a non-empty string"))

    submission = data.get("submission")
    if not isinstance(submission, str) or not submission.strip():
        errors.append(("submission", "must be a non-empty string describing how to submit the answer"))

    category = data.get("category")
    if not isinstance(category, str) or category not in CATEGORIES:
        allowed = ", ".join(CATEGORIES)
        errors.append(("category", f"must be one of: {allowed}"))

    task_type = data.get("task_type")
    if not isinstance(task_type, str) or task_type not in TASK_TYPES:
        allowed = ", ".join(TASK_TYPES)
        errors.append(("task_type", f"must be one of: {allowed}"))

    max_steps = data.get("max_steps")
    if not _is_integral(max_steps) or max_steps <= 0:
        errors.append(("max_steps", "must be a positive integer"))

    url = data.get("sqlalchemy_url")
    if not isinstance(url, str) or not url.strip():
        errors.append(("sqlalchemy_url", "must be a non-empty string"))
    else:
        try:
            make_url(url)
        except Exception:
            errors.append(("sqlalchemy_url", "must be a valid SQLAlchemy database URL"))

    timeout = data.get("query_timeout")
    if not (_is_integral(timeout) or _is_number(timeout)) or timeout <= 0:
        errors.append(("query_timeout", "must be a positive number of seconds"))
    elif isinstance(timeout, float) and not timeout.is_integer():
        errors.append(("query_timeout", "must be a whole number of seconds"))

    row_limit = data.get("row_limit")
    if row_limit is not None and not (_is_integral(row_limit) and row_limit > 0):
        errors.append(("row_limit", "must be a positive integer when present"))

    errors.extend(_validate_terminal_condition(data.get("terminal_condition")))
    return errors


def _validate_terminal_condition(condition: Any) -> list[tuple[str, str]]:
    if not isinstance(condition, dict):
        return [("terminal_condition", "must be an object with expected_tuples/judge and matching rules")]
    errors: list[tuple[str, str]] = []
    has_judge = "judge" in condition
    if has_judge:
        judge = condition["judge"]
        if not isinstance(judge, dict):
            errors.append(("terminal_condition.judge", "must be an object with an 'expected' string"))
        else:
            if not isinstance(judge.get("expected"), str) or not judge["expected"].strip():
                errors.append(("terminal_condition.judge.expected", "must be a non-empty string"))
            key_points = judge.get("key_points")
            if key_points is not None:
                if not isinstance(key_points, list) or any(
                    not isinstance(point, str) or not point.strip() for point in key_points
                ):
                    errors.append(
                        ("terminal_condition.judge.key_points", "must be an array of non-empty strings")
                    )
                elif judge.get("reward_mode") == "dense" and not key_points:
                    errors.append(("terminal_condition.judge.key_points", "dense reward_mode requires key points"))
            reward_mode = judge.get("reward_mode")
            if reward_mode is not None and reward_mode not in ("sparse", "dense"):
                errors.append(("terminal_condition.judge.reward_mode", "must be 'sparse' or 'dense'"))
    if "expected_tuples" not in condition and not has_judge:
        errors.append(("terminal_condition.expected_tuples", "is required unless a judge block is declared"))
    if "expected_tuples" in condition:
        errors.extend(_validate_expected_tuples(condition["expected_tuples"], "terminal_condition.expected_tuples"))
    errors.extend(_validate_matching_rules(condition.get("matching", {}), "terminal_condition.matching"))
    errors.extend(_validate_state_checks(condition.get("state_checks")))
    errors.extend(_validate_rules(condition.get("rules")))
    return errors


def _validate_expected_tuples(expected: Any, prefix: str) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    if not isinstance(expected, list):
        errors.append((prefix, "must be an array of tuples"))
    else:
        for index, row in enumerate(expected):
            if not isinstance(row, list):
                errors.append((f"{prefix}[{index}]", "each tuple must be an array"))
    return errors


def _validate_matching_rules(matching: Any, prefix: str) -> list[tuple[str, str]]:
    if not isinstance(matching, dict):
        return [(prefix, "must be an object")]
    errors: list[tuple[str, str]] = []
    tolerance = matching.get("numeric_tolerance")
    if tolerance is not None and not _is_number(tolerance):
        errors.append((f"{prefix}.numeric_tolerance", "must be a number"))
    if tolerance is not None and tolerance < 0:
        errors.append((f"{prefix}.numeric_tolerance", "must not be negative"))
    if "null_handling" in matching and matching["null_handling"] != NULL_HANDLING_EXACT:
        errors.append((f"{prefix}.null_handling", "only 'exact' is supported"))
    if "order_sensitive" in matching and not isinstance(matching["order_sensitive"], bool):
        errors.append((f"{prefix}.order_sensitive", "must be a boolean"))
    return errors


def _validate_rules(rules: Any) -> list[tuple[str, str]]:
    if rules is None:
        return []
    if not isinstance(rules, list):
        return [("terminal_condition.rules", "must be an array of verification rules")]
    errors: list[tuple[str, str]] = []
    for index, rule in enumerate(rules):
        prefix = f"terminal_condition.rules[{index}]"
        if not isinstance(rule, dict):
            errors.append((prefix, "each verification rule must be an object"))
            continue
        sql = rule.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            errors.append((f"{prefix}.sql", "must be a non-empty string"))
        description = rule.get("description")
        if description is not None and not isinstance(description, str):
            errors.append((f"{prefix}.description", "must be a string"))
    return errors


def _validate_state_checks(checks: Any) -> list[tuple[str, str]]:
    if checks is None:
        return []
    if not isinstance(checks, list):
        return [("terminal_condition.state_checks", "must be an array of state checks")]
    errors: list[tuple[str, str]] = []
    for index, check in enumerate(checks):
        prefix = f"terminal_condition.state_checks[{index}]"
        if not isinstance(check, dict):
            errors.append((prefix, "each state check must be an object"))
            continue
        sql = check.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            errors.append((f"{prefix}.sql", "must be a non-empty string"))
        errors.extend(_validate_expected_tuples(check.get("expected_tuples"), f"{prefix}.expected_tuples"))
        errors.extend(_validate_matching_rules(check.get("matching", {}), f"{prefix}.matching"))
    return errors


def validate_task_record(task: TaskRecord) -> list[tuple[str, str]]:
    payload: dict[str, Any] = {
        "task_id": task.task_id,
        "goal": task.goal,
        "submission": task.submission,
        "category": task.category,
        "task_type": task.task_type,
        "max_steps": task.max_steps,
        "sqlalchemy_url": task.sqlalchemy_url,
        "query_timeout": task.query_timeout,
        "row_limit": task.row_limit,
        "terminal_condition": task.terminal_condition.to_dict(),
    }
    return validate_task_payload(payload)


def parse_task_payload(data: dict[str, Any]) -> TaskRecord:
    errors = validate_task_payload(data)
    if errors:
        raise TaskValidationError(errors)
    return TaskRecord(
        task_id=data["task_id"],
        goal=data["goal"],
        submission=data["submission"],
        category=data["category"],
        task_type=data["task_type"],
        max_steps=data["max_steps"],
        sqlalchemy_url=data["sqlalchemy_url"],
        terminal_condition=TerminalCondition.from_dict(data["terminal_condition"]),
        query_timeout=data["query_timeout"],
        row_limit=data.get("row_limit"),
    )
