from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from dbarena.models.actions import ActionCall

OBSERVATION_SCHEMA_VERSION = "dbarena-observation/v1"


class ObservationType(StrEnum):
    QUERY_RESULT = "query_result"
    TABLE_DESCRIPTION = "table_description"
    SCHEMA_DESCRIPTION = "schema_description"
    EPISODE_RESULT = "episode_result"
    ERROR = "error"


class ErrorCode(StrEnum):
    INVALID_ACTION = "invalid_action"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNSUPPORTED_ENGINE = "unsupported_engine"
    NOT_FOUND = "not_found"
    QUERY_ERROR = "query_error"
    TIMEOUT = "timeout"
    EMBEDDING_ERROR = "embedding_error"
    JUDGE_ERROR = "judge_error"
    INTERNAL = "internal"


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    nullable: bool = True
    default: str | None = None
    primary_key: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
            "default": self.default,
            "primary_key": self.primary_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ColumnInfo":
        return cls(
            name=str(data["name"]),
            type=str(data["type"]),
            nullable=bool(data.get("nullable", True)),
            default=data.get("default"),
            primary_key=bool(data.get("primary_key", False)),
        )


@dataclass(frozen=True)
class QueryResultPayload:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    complete: bool
    row_limit: int | None = None
    total_row_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "rows": [list(row) for row in self.rows],
            "row_count": self.row_count,
            "complete": self.complete,
            "row_limit": self.row_limit,
            "total_row_count": self.total_row_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueryResultPayload":
        return cls(
            columns=[str(c) for c in data["columns"]],
            rows=[list(row) for row in data["rows"]],
            row_count=int(data["row_count"]),
            complete=bool(data["complete"]),
            row_limit=data.get("row_limit"),
            total_row_count=data.get("total_row_count"),
        )


@dataclass(frozen=True)
class TableDescription:
    name: str
    columns: list[ColumnInfo]
    found: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": [c.to_dict() for c in self.columns],
            "found": self.found,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TableDescription":
        return cls(
            name=str(data["name"]),
            columns=[ColumnInfo.from_dict(c) for c in data["columns"]],
            found=bool(data.get("found", True)),
        )


@dataclass(frozen=True)
class TableDescriptionPayload:
    tables: list[TableDescription]

    def to_dict(self) -> dict[str, Any]:
        return {"tables": [t.to_dict() for t in self.tables]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TableDescriptionPayload":
        return cls(tables=[TableDescription.from_dict(t) for t in data["tables"]])


@dataclass(frozen=True)
class TableSummary:
    name: str
    column_count: int
    columns: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "column_count": self.column_count, "columns": list(self.columns)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TableSummary":
        return cls(
            name=str(data["name"]),
            column_count=int(data["column_count"]),
            columns=[str(c) for c in data["columns"]],
        )


@dataclass(frozen=True)
class SchemaDescriptionPayload:
    tables: list[TableSummary]

    def to_dict(self) -> dict[str, Any]:
        return {"tables": [t.to_dict() for t in self.tables]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SchemaDescriptionPayload":
        return cls(tables=[TableSummary.from_dict(t) for t in data["tables"]])


@dataclass(frozen=True)
class ErrorPayload:
    code: ErrorCode
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ErrorPayload":
        return cls(code=ErrorCode(data["code"]), message=str(data["message"]))


@dataclass(frozen=True)
class EpisodeResultPayload:
    matched: bool
    message: str
    reward: float

    def to_dict(self) -> dict[str, Any]:
        return {"matched": self.matched, "message": self.message, "reward": self.reward}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EpisodeResultPayload":
        return cls(
            matched=bool(data["matched"]),
            message=str(data["message"]),
            reward=float(data["reward"]),
        )


_PAYLOAD_TYPES: dict[ObservationType, type] = {
    ObservationType.QUERY_RESULT: QueryResultPayload,
    ObservationType.TABLE_DESCRIPTION: TableDescriptionPayload,
    ObservationType.SCHEMA_DESCRIPTION: SchemaDescriptionPayload,
    ObservationType.EPISODE_RESULT: EpisodeResultPayload,
    ObservationType.ERROR: ErrorPayload,
}


@dataclass(frozen=True)
class Observation:
    type: ObservationType
    payload: Any
    action: ActionCall | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    reward: float | None = None
    terminated: bool = False
    truncated: bool = False
    step_number: int | None = None
    task: dict[str, Any] | None = None
    schema_version: str = OBSERVATION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "type": self.type.value,
            "task": dict(self.task) if self.task is not None else None,
            "action": self.action.to_dict() if self.action is not None else None,
            "payload": self.payload.to_dict(),
            "reward": self.reward,
            "terminated": self.terminated,
            "truncated": self.truncated,
            "step_number": self.step_number,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Observation":
        if not isinstance(data, dict):
            raise ValueError("observation must be a JSON object")
        if data.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
            raise ValueError(f"unsupported observation schema version: {data.get('schema_version')!r}")
        try:
            obs_type = ObservationType(data["type"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unknown observation type: {data.get('type')!r}") from exc
        payload_type = _PAYLOAD_TYPES[obs_type]
        payload = payload_type.from_dict(data["payload"])
        action = ActionCall.from_dict(data["action"]) if data.get("action") is not None else None
        reward = data.get("reward")
        task = data.get("task")
        if task is not None and not (
            isinstance(task, dict)
            and {"task_id", "goal", "submission", "category", "task_type"} <= set(task)
        ):
            raise ValueError("task block requires task_id, goal, submission, category, and task_type")
        return cls(
            type=obs_type,
            payload=payload,
            action=action,
            meta=dict(data.get("meta", {})),
            reward=float(reward) if reward is not None else None,
            terminated=bool(data.get("terminated", False)),
            truncated=bool(data.get("truncated", False)),
            step_number=int(data["step_number"]) if data.get("step_number") is not None else None,
            task=dict(task) if task is not None else None,
            schema_version=str(data["schema_version"]),
        )
