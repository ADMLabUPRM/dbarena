from typing import Any

from dbarena.models.observations import (
    EpisodeResultPayload,
    ErrorPayload,
    Observation,
    ObservationType,
    QueryResultPayload,
    SchemaDescriptionPayload,
    TableDescriptionPayload,
)


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    return str(value).replace("|", "\\|").replace("\r\n", " ").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    return "\n".join(lines)


def _query_result_markdown(payload: QueryResultPayload) -> str:
    lines = ["## Query result", "", _table(payload.columns, payload.rows), ""]
    if payload.complete:
        lines.append(f"Result is complete: all {payload.row_count} rows are shown.")
    else:
        lines.append(
            f"Result is truncated: showing the first {payload.row_count} rows (row limit {payload.row_limit}); "
            "this is not the entire result."
        )
    return "\n".join(lines)


def _table_description_markdown(payload: TableDescriptionPayload) -> str:
    sections = ["## Table description", ""]
    for table in payload.tables:
        if not table.found:
            sections.append(f"### {table.name}")
            sections.append("")
            sections.append(f"Table `{table.name}` was not found.")
            sections.append("")
            continue
        sections.append(f"### {table.name}")
        sections.append("")
        rows = [
            [
                c.name,
                c.type,
                "yes" if c.nullable else "no",
                c.default if c.default is not None else "-",
                "PK" if c.primary_key else "-",
            ]
            for c in table.columns
        ]
        sections.append(_table(["Column", "Type", "Nullable", "Default", "Key"], rows))
        sections.append("")
    return "\n".join(sections).rstrip() + "\n"


def _schema_description_markdown(payload: SchemaDescriptionPayload) -> str:
    rows = [[t.name, t.column_count, ", ".join(t.columns)] for t in payload.tables]
    return "## Schema description\n\n" + _table(["Table", "Columns", "Column summary"], rows) + "\n"


def _error_markdown(payload: ErrorPayload) -> str:
    return f"**Error** (`{payload.code.value}`): {payload.message}"


def _episode_result_markdown(payload: EpisodeResultPayload) -> str:
    verdict = "correct" if payload.matched else "incorrect"
    return f"Episode ended. The submitted answer is **{verdict}**. Reward: {payload.reward}.\n\n{payload.message}"


_RENDERERS = {
    ObservationType.QUERY_RESULT: _query_result_markdown,
    ObservationType.TABLE_DESCRIPTION: _table_description_markdown,
    ObservationType.SCHEMA_DESCRIPTION: _schema_description_markdown,
    ObservationType.ERROR: _error_markdown,
    ObservationType.EPISODE_RESULT: _episode_result_markdown,
}


def _step_signals(obs: Observation) -> str:
    if obs.step_number is None or obs.type == ObservationType.EPISODE_RESULT:
        return ""
    reward_text = "-" if obs.reward is None else str(obs.reward)
    if obs.terminated:
        status = "terminated"
    elif obs.truncated:
        status = "truncated"
    else:
        status = "episode continues"
    return f"\n\nStep {obs.step_number} | reward {reward_text} | {status}"


def to_markdown(obs: Observation) -> str:
    renderer = _RENDERERS[obs.type]
    return renderer(obs.payload) + _step_signals(obs)
