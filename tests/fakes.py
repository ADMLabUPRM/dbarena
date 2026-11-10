from dbarena.catalog.errors import DuplicateTaskError, TaskNotFoundError
from dbarena.catalog.records import TaskRecord


class FakeEpisodeTransaction:
    def __init__(self):
        self.connection = object()
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True


def fake_episode_transaction_factory(engine, timeout_seconds):
    return FakeEpisodeTransaction()


class FakeTaskStore:
    def __init__(self, tasks: list[TaskRecord] | None = None):
        self._tasks: dict[str, TaskRecord] = {t.task_id: t for t in (tasks or [])}

    def create(self, task: TaskRecord) -> TaskRecord:
        if task.task_id in self._tasks:
            raise DuplicateTaskError(task.task_id)
        self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def list(self, category: str | None = None, task_type: str | None = None) -> list[TaskRecord]:
        tasks = sorted(self._tasks.values(), key=lambda t: t.task_id)
        if category is not None:
            tasks = [t for t in tasks if t.category == category]
        if task_type is not None:
            tasks = [t for t in tasks if t.task_type == task_type]
        return tasks

    def update(self, task: TaskRecord) -> TaskRecord:
        if task.task_id not in self._tasks:
            raise TaskNotFoundError(task.task_id)
        self._tasks[task.task_id] = task
        return task

    def delete(self, task_id: str) -> bool:
        return self._tasks.pop(task_id, None) is not None
