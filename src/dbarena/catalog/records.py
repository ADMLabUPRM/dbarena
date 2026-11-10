from dataclasses import dataclass

from dbarena.models.terminal import TerminalCondition


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    goal: str
    submission: str
    category: str
    task_type: str
    max_steps: int
    sqlalchemy_url: str
    terminal_condition: TerminalCondition
    query_timeout: int
    row_limit: int | None = None
