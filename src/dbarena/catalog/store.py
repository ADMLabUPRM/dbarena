import json

from sqlalchemy import Engine, text

from dbarena.catalog.errors import DuplicateTaskError, TaskNotFoundError
from dbarena.catalog.records import TaskRecord
from dbarena.catalog.validation import validate_task_record
from dbarena.models.terminal import TerminalCondition

__all__ = ["TaskRecord", "TaskCatalogStore"]


def _row_to_record(row) -> TaskRecord:
    return TaskRecord(
        task_id=row.task_id,
        goal=row.goal,
        submission=row.submission or "",
        category=row.category or "OLAP",
        task_type=row.task_type or "IS",
        max_steps=row.max_steps,
        sqlalchemy_url=row.sqlalchemy_url,
        terminal_condition=TerminalCondition.from_dict(row.terminal_condition),
        query_timeout=row.query_timeout,
        row_limit=row.row_limit,
    )


class TaskCatalogStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, task: TaskRecord) -> TaskRecord:
        _raise_if_invalid(task)
        payload = task.terminal_condition.to_dict()
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                           max_steps, sqlalchemy_url,
                                           terminal_condition, query_timeout, row_limit)
                        VALUES (:task_id, :goal, :submission, :category, :task_type,
                                :max_steps, :sqlalchemy_url,
                                CAST(:terminal_condition AS jsonb), :query_timeout, :row_limit)
                        """
                    ),
                    {
                        "task_id": task.task_id,
                        "goal": task.goal,
                        "submission": task.submission,
                        "category": task.category,
                        "task_type": task.task_type,
                        "max_steps": task.max_steps,
                        "sqlalchemy_url": task.sqlalchemy_url,
                        "terminal_condition": _json(payload),
                        "query_timeout": task.query_timeout,
                        "row_limit": task.row_limit,
                    },
                )
        except Exception as exc:
            if "tasks_pkey" in str(exc):
                raise DuplicateTaskError(task.task_id) from exc
            raise
        return task

    def get(self, task_id: str) -> TaskRecord | None:
        query = text("SELECT * FROM tasks WHERE task_id = :task_id")
        with self.engine.connect() as conn:
            row = conn.execute(query, {"task_id": task_id}).mappings().first()
        return _row_to_record(row) if row else None

    def list(self, category: str | None = None, task_type: str | None = None) -> list[TaskRecord]:
        query = "SELECT * FROM tasks"
        conditions = []
        params: dict[str, str] = {}
        if category is not None:
            conditions.append("category = :category")
            params["category"] = category
        if task_type is not None:
            conditions.append("task_type = :task_type")
            params["task_type"] = task_type
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY task_id"
        with self.engine.connect() as conn:
            rows = conn.execute(text(query), params).mappings().all()
        return [_row_to_record(row) for row in rows]

    def update(self, task: TaskRecord) -> TaskRecord:
        _raise_if_invalid(task)
        payload = task.terminal_condition.to_dict()
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE tasks
                    SET goal = :goal,
                        submission = :submission,
                        category = :category,
                        task_type = :task_type,
                        max_steps = :max_steps,
                        sqlalchemy_url = :sqlalchemy_url,
                        terminal_condition = CAST(:terminal_condition AS jsonb),
                        query_timeout = :query_timeout,
                        row_limit = :row_limit,
                        updated_at = now()
                    WHERE task_id = :task_id
                    """
                ),
                {
                    "task_id": task.task_id,
                    "goal": task.goal,
                    "submission": task.submission,
                    "category": task.category,
                    "task_type": task.task_type,
                    "max_steps": task.max_steps,
                    "sqlalchemy_url": task.sqlalchemy_url,
                    "terminal_condition": _json(payload),
                    "query_timeout": task.query_timeout,
                    "row_limit": task.row_limit,
                },
            )
            if result.rowcount == 0:
                raise TaskNotFoundError(task.task_id)
        return task

    def delete(self, task_id: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(text("DELETE FROM tasks WHERE task_id = :task_id"), {"task_id": task_id})
            return result.rowcount > 0


def _json(value) -> str:
    return json.dumps(value)


def _raise_if_invalid(task: TaskRecord) -> None:
    from dbarena.catalog.errors import TaskValidationError

    errors = validate_task_record(task)
    if errors:
        raise TaskValidationError(errors)
