class TaskValidationError(Exception):
    def __init__(self, errors: list[tuple[str, str]]):
        self.errors = errors
        super().__init__("task validation failed: " + "; ".join(f"{f}: {m}" for f, m in errors))


class TaskNotFoundError(Exception):
    pass


class DuplicateTaskError(Exception):
    pass
