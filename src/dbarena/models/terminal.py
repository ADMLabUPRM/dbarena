from dataclasses import dataclass, field
from typing import Any

NULL_HANDLING_EXACT = "exact"


@dataclass(frozen=True)
class MatchingRules:
    order_sensitive: bool = False
    numeric_tolerance: float | None = None
    null_handling: str = NULL_HANDLING_EXACT

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_sensitive": self.order_sensitive,
            "numeric_tolerance": self.numeric_tolerance,
            "null_handling": self.null_handling,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MatchingRules":
        data = data or {}
        null_handling = data.get("null_handling", NULL_HANDLING_EXACT)
        if null_handling != NULL_HANDLING_EXACT:
            raise ValueError(f"unsupported null_handling: {null_handling!r}")
        tolerance = data.get("numeric_tolerance")
        return cls(
            order_sensitive=bool(data.get("order_sensitive", False)),
            numeric_tolerance=float(tolerance) if tolerance is not None else None,
            null_handling=str(null_handling),
        )


@dataclass(frozen=True)
class StateCheck:
    sql: str
    expected_tuples: list[list[Any]]
    matching: MatchingRules = field(default_factory=MatchingRules)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "expected_tuples": [list(t) for t in self.expected_tuples],
            "matching": self.matching.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateCheck":
        if not isinstance(data, dict) or "sql" not in data or "expected_tuples" not in data:
            raise ValueError("state check requires 'sql' and 'expected_tuples'")
        return cls(
            sql=str(data["sql"]),
            expected_tuples=[list(t) for t in data["expected_tuples"]],
            matching=MatchingRules.from_dict(data.get("matching")),
        )


@dataclass(frozen=True)
class VerificationRule:
    sql: str
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"sql": self.sql}
        if self.description is not None:
            data["description"] = self.description
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VerificationRule":
        if not isinstance(data, dict) or "sql" not in data:
            raise ValueError("verification rule requires 'sql'")
        description = data.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError("verification rule 'description' must be a string")
        return cls(sql=str(data["sql"]), description=description)


@dataclass(frozen=True)
class JudgeCondition:
    """LLM-judged verification: the authored expected result the judge checks against.

    ``key_points`` enables dense rewards (graded by conveyed fraction);
    ``reward_mode`` selects ``sparse`` (binary, default) or ``dense``.
    """

    expected: str
    key_points: tuple[str, ...] = ()
    reward_mode: str = "sparse"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"expected": self.expected}
        if self.key_points:
            data["key_points"] = list(self.key_points)
        if self.reward_mode != "sparse":
            data["reward_mode"] = self.reward_mode
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "JudgeCondition":
        if not isinstance(data, dict) or not isinstance(data.get("expected"), str) or not data["expected"].strip():
            raise ValueError("judge condition requires a non-empty 'expected' string")
        reward_mode = data.get("reward_mode") or "sparse"
        if reward_mode not in ("sparse", "dense"):
            raise ValueError(f"unsupported judge reward_mode: {reward_mode!r}")
        raw_key_points = data.get("key_points")
        if raw_key_points is None:
            raw_key_points = []
        if not isinstance(raw_key_points, list):
            raise ValueError("judge condition 'key_points' must be an array of strings")
        key_points = []
        for key_point in raw_key_points:
            if not isinstance(key_point, str) or not key_point.strip():
                raise ValueError("judge condition 'key_points' entries must be non-empty strings")
            key_points.append(key_point)
        if reward_mode == "dense" and not key_points:
            raise ValueError("dense reward_mode requires 'key_points'")
        return cls(expected=str(data["expected"]), key_points=tuple(key_points), reward_mode=reward_mode)


@dataclass(frozen=True)
class TerminalCondition:
    expected_tuples: list[list[Any]] = field(default_factory=list)
    matching: MatchingRules = field(default_factory=MatchingRules)
    state_checks: tuple[StateCheck, ...] = ()
    rules: tuple[VerificationRule, ...] = ()
    judge: JudgeCondition | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.judge is not None:
            data["judge"] = self.judge.to_dict()
        if self.expected_tuples:
            data["expected_tuples"] = [list(t) for t in self.expected_tuples]
            data["matching"] = self.matching.to_dict()
        if self.state_checks:
            data["state_checks"] = [check.to_dict() for check in self.state_checks]
        if self.rules:
            data["rules"] = [rule.to_dict() for rule in self.rules]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TerminalCondition":
        if not isinstance(data, dict) or ("expected_tuples" not in data and "judge" not in data):
            raise ValueError("terminal condition requires 'expected_tuples' or a 'judge' block")
        raw_judge = data.get("judge")
        judge = JudgeCondition.from_dict(raw_judge) if raw_judge is not None else None
        raw_checks = data.get("state_checks")
        checks: tuple[StateCheck, ...] = ()
        if raw_checks:
            if not isinstance(raw_checks, list):
                raise ValueError("terminal condition 'state_checks' must be an array of state checks")
            checks = tuple(StateCheck.from_dict(check) for check in raw_checks)
        raw_rules = data.get("rules")
        rules: tuple[VerificationRule, ...] = ()
        if raw_rules:
            if not isinstance(raw_rules, list):
                raise ValueError("terminal condition 'rules' must be an array of verification rules")
            rules = tuple(VerificationRule.from_dict(rule) for rule in raw_rules)
        return cls(
            expected_tuples=[list(t) for t in data.get("expected_tuples", [])],
            matching=MatchingRules.from_dict(data.get("matching")),
            state_checks=checks,
            rules=rules,
            judge=judge,
        )
