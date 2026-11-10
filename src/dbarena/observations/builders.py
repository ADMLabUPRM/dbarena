from typing import Any

from dbarena.models.actions import ActionCall
from dbarena.models.observations import (
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
from dbarena.observations.values import jsonify_row


def query_result_observation(
    action: ActionCall,
    columns: list[str],
    rows: list[list[Any]],
    row_limit: int,
    duration_ms: float | None = None,
) -> tuple[Observation, str | None]:
    truncated = len(rows) > row_limit
    kept_rows = rows[:row_limit] if truncated else rows
    converted: list[list[Any]] = []
    encoding_note: str | None = None
    for row in kept_rows:
        values, note = jsonify_row(list(row))
        converted.append(values)
        if note is not None:
            encoding_note = note
    payload = QueryResultPayload(
        columns=[str(c) for c in columns],
        rows=converted,
        row_count=len(converted),
        complete=not truncated,
        row_limit=row_limit,
        total_row_count=None if truncated else len(converted),
    )
    meta: dict[str, Any] = {}
    if duration_ms is not None:
        meta["duration_ms"] = duration_ms
    if truncated:
        meta["truncated"] = True
    if encoding_note is not None:
        meta["value_encoding"] = encoding_note
    return Observation(type=ObservationType.QUERY_RESULT, payload=payload, action=action, meta=meta), encoding_note


def table_description_observation(
    action: ActionCall,
    tables: list[TableDescription],
    duration_ms: float | None = None,
) -> Observation:
    payload = TableDescriptionPayload(tables=list(tables))
    meta = {"duration_ms": duration_ms} if duration_ms is not None else {}
    return Observation(type=ObservationType.TABLE_DESCRIPTION, payload=payload, action=action, meta=meta)


def schema_description_observation(
    action: ActionCall,
    tables: list[TableSummary],
    duration_ms: float | None = None,
) -> Observation:
    payload = SchemaDescriptionPayload(tables=list(tables))
    meta = {"duration_ms": duration_ms} if duration_ms is not None else {}
    return Observation(type=ObservationType.SCHEMA_DESCRIPTION, payload=payload, action=action, meta=meta)


def error_observation(
    action: ActionCall | None,
    code: ErrorCode,
    message: str,
    duration_ms: float | None = None,
) -> Observation:
    payload = ErrorPayload(code=code, message=message)
    meta = {"duration_ms": duration_ms} if duration_ms is not None else {}
    return Observation(type=ObservationType.ERROR, payload=payload, action=action, meta=meta)


def episode_result_observation(
    action: ActionCall,
    matched: bool,
    message: str,
    reward: float,
) -> Observation:
    payload = EpisodeResultPayload(matched=matched, message=message, reward=reward)
    return Observation(type=ObservationType.EPISODE_RESULT, payload=payload, action=action)
