from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from gui.backend.app.session_store import EmbeddingGateError, SessionNotActiveError, SessionNotFoundError


class StepRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=128)
    arguments: dict = Field(default_factory=dict)


def build_sessions_router(store) -> APIRouter:
    router = APIRouter(prefix="/api/sessions", tags=["sessions"])

    @router.post("", status_code=201)
    def create_session(payload: dict):
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise HTTPException(status_code=400, detail="task_id is required")
        try:
            state = store.create(task_id)
        except EmbeddingGateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except Exception as exc:
            cause = str(getattr(exc, "__cause__", None) or exc)
            raise HTTPException(status_code=400, detail=f"cannot start session: {cause}") from exc
        return state.to_dict()

    @router.get("")
    def list_sessions():
        return store.list()

    @router.get("/{session_id}")
    def get_session(session_id: str):
        try:
            return store.get(session_id).to_dict()
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail="session not found") from None

    @router.post("/{session_id}/step")
    def step_session(session_id: str, request: StepRequest):
        try:
            record = store.step(session_id, request.tool, request.arguments)
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail="session not found") from None
        except SessionNotActiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {
            "step_number": record.step_number,
            "action": record.action,
            "observation": record.observation,
            "reward": record.reward,
            "terminated": record.terminated,
            "truncated": record.truncated,
        }

    @router.post("/{session_id}/reset")
    def reset_session(session_id: str):
        try:
            state = store.reset_session(session_id)
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail="session not found") from None
        except EmbeddingGateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except SessionNotActiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return state.to_dict()

    return router
