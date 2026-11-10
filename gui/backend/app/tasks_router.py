from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from dbarena.catalog.errors import DuplicateTaskError, TaskNotFoundError
from dbarena.catalog.records import TaskRecord
from dbarena.catalog.validation import CATEGORIES, TASK_TYPES, parse_task_payload, validate_task_payload


def _task_to_dict(task: TaskRecord) -> dict:
    return {
        "task_id": task.task_id,
        "goal": task.goal,
        "submission": task.submission,
        "category": task.category,
        "task_type": task.task_type,
        "max_steps": task.max_steps,
        "sqlalchemy_url": task.sqlalchemy_url,
        "terminal_condition": task.terminal_condition.to_dict(),
        "query_timeout": task.query_timeout,
        "row_limit": task.row_limit,
    }


def _validation_response(errors: list[tuple[str, str]]) -> JSONResponse:
    fields: dict[str, list[str]] = {}
    for name, message in errors:
        fields.setdefault(name, []).append(message)
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "validation_error", "message": "task validation failed", "fields": fields}},
    )


def build_classification_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["tasks"])

    @router.get("/task-classifications")
    def task_classifications():
        return {"categories": list(CATEGORIES), "task_types": list(TASK_TYPES)}

    return router


def build_tasks_router(catalog) -> APIRouter:
    router = APIRouter(prefix="/api/tasks", tags=["tasks"])

    @router.get("")
    def list_tasks(
        category: str | None = Query(default=None),
        task_type: str | None = Query(default=None),
    ):
        errors: list[tuple[str, str]] = []
        if category is not None and category not in CATEGORIES:
            errors.append(("category", "must be one of: " + ", ".join(CATEGORIES)))
        if task_type is not None and task_type not in TASK_TYPES:
            errors.append(("task_type", "must be one of: " + ", ".join(TASK_TYPES)))
        if errors:
            return _validation_response(errors)
        return [_task_to_dict(t) for t in catalog.list(category=category, task_type=task_type)]

    @router.post("", status_code=201)
    def create_task(payload: dict):
        errors = validate_task_payload(payload)
        if errors:
            return _validation_response(errors)
        try:
            record = parse_task_payload(payload)
            catalog.create(record)
        except DuplicateTaskError:
            raise HTTPException(status_code=409, detail="task already exists") from None
        return _task_to_dict(record)

    @router.get("/{task_id}")
    def get_task(task_id: str):
        task = catalog.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found") from None
        return _task_to_dict(task)

    @router.put("/{task_id}")
    def update_task(task_id: str, payload: dict):
        errors = validate_task_payload(payload)
        if errors:
            return _validation_response(errors)
        record = parse_task_payload(payload)
        if record.task_id != task_id:
            return _validation_response([("task_id", "payload task_id must match the URL task_id")])
        try:
            updated = catalog.update(record)
        except TaskNotFoundError:
            raise HTTPException(status_code=404, detail="task not found") from None
        return _task_to_dict(updated)

    @router.delete("/{task_id}", status_code=204)
    def delete_task(task_id: str):
        if not catalog.delete(task_id):
            raise HTTPException(status_code=404, detail="task not found") from None

    @router.post("/{task_id}/validate")
    def validate_task(task_id: str, payload: dict):
        errors = validate_task_payload(payload)
        if errors:
            return _validation_response(errors)
        return {"valid": True, "task_id": task_id}

    return router
