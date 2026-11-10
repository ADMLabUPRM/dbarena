import json
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from dbarena.embeddings import EmbeddingConfig
from gui.backend.app.embedding_store import EmbeddingService, EmbeddingServiceError

JUDGE_TEST_TIMEOUT_SECONDS = 20.0


class EmbeddingConfigRequest(BaseModel):
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    dim: int = Field(gt=0)
    api_key: str = ""
    max_model_tokens: int = Field(default=512, gt=0)
    chunk_chars: int = Field(default=800, gt=0)
    chunk_overlap: int = Field(default=0, ge=0)


class JudgeConfigRequest(BaseModel):
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: str = ""


class JudgeTestRequest(BaseModel):
    base_url: str = ""
    model: str = ""
    api_key: str = ""


def build_embeddings_router(service: EmbeddingService | None) -> APIRouter:
    router = APIRouter(prefix="/api/embeddings", tags=["embeddings"])

    @router.get("/status")
    def status():
        if service is None:
            return {
                "state": "unconfigured",
                "states": ["unconfigured", "missing", "stale", "ready"],
                "config": None,
                "embedded": 0,
                "total": 0,
                "job": None,
                "message": "embedding service unavailable (no admin database)",
            }
        return service.status()

    @router.put("/config")
    def save_config(payload: EmbeddingConfigRequest):
        if service is None:
            raise HTTPException(status_code=503, detail="embedding service unavailable")
        if payload.chunk_overlap >= payload.chunk_chars:
            raise HTTPException(
                status_code=400, detail="chunk_overlap must be smaller than chunk_chars"
            )
        try:
            config = service.save_config(
                EmbeddingConfig(
                    base_url=payload.base_url.strip(),
                    model=payload.model.strip(),
                    dim=payload.dim,
                    api_key=payload.api_key,
                    max_model_tokens=payload.max_model_tokens,
                    chunk_chars=payload.chunk_chars,
                    chunk_overlap=payload.chunk_overlap,
                )
            )
        except EmbeddingServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"config": config.public_dict()}

    @router.post("/reembed")
    def reembed():
        if service is None:
            raise HTTPException(status_code=503, detail="embedding service unavailable")
        try:
            return service.start_reembed()
        except EmbeddingServiceError as exc:
            message = str(exc)
            code = 409 if "already running" in message else 400
            raise HTTPException(status_code=code, detail=message) from None

    return router


def build_judge_config_router(admin_engine) -> APIRouter:
    router = APIRouter(prefix="/api/judge", tags=["judge"])

    @router.get("/config")
    def get_config():
        try:
            with admin_engine.connect() as conn:
                row = conn.execute(
                    text("SELECT base_url, model, api_key FROM judge_config WHERE id")
                ).mappings().first()
        except Exception:
            row = None
        if row is None:
            return {"config": None}
        return {
            "config": {
                "base_url": row["base_url"],
                "model": row["model"],
                "api_key_set": bool(row["api_key"]),
            }
        }

    @router.put("/config")
    def save_config(payload: JudgeConfigRequest):
        existing_key = ""
        try:
            with admin_engine.connect() as conn:
                row = conn.execute(
                    text("SELECT api_key FROM judge_config WHERE id")
                ).mappings().first()
            if row is not None:
                existing_key = row["api_key"] or ""
        except Exception:
            existing_key = ""
        api_key = payload.api_key if payload.api_key else existing_key
        try:
            with admin_engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO judge_config (id, base_url, model, api_key) "
                        "VALUES (true, :base_url, :model, :api_key) "
                        "ON CONFLICT (id) DO UPDATE SET base_url = :base_url, model = :model, "
                        "api_key = :api_key, updated_at = now()"
                    ),
                    {
                        "base_url": payload.base_url.strip(),
                        "model": payload.model.strip(),
                        "api_key": api_key,
                    },
                )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"config": {"base_url": payload.base_url.strip(), "model": payload.model.strip()}}

    @router.post("/test")
    def test_config(payload: JudgeTestRequest):
        stored_base = stored_model = stored_key = ""
        try:
            with admin_engine.connect() as conn:
                row = conn.execute(
                    text("SELECT base_url, model, api_key FROM judge_config WHERE id")
                ).mappings().first()
            if row is not None:
                stored_base = (row["base_url"] or "").strip()
                stored_model = (row["model"] or "").strip()
                stored_key = row["api_key"] or ""
        except Exception:
            stored_base = stored_model = stored_key = ""
        base_url = payload.base_url.strip() or stored_base
        model = payload.model.strip() or stored_model
        api_key = payload.api_key or stored_key
        if not base_url or not model:
            raise HTTPException(
                status_code=400,
                detail="nothing to test: provide a base URL and model or save a judge configuration first",
            )
        endpoint = f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"
        body = json.dumps(
            {
                "model": model,
                "temperature": 0,
                "messages": [{"role": "user", "content": "Reply with the single word OK."}],
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=JUDGE_TEST_TIMEOUT_SECONDS) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            return {"ok": False, "error": f"endpoint returned HTTP {exc.code}: {detail}"}
        except (urllib.error.URLError, OSError) as exc:
            return {"ok": False, "error": f"endpoint unreachable: {exc}"}
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"endpoint returned a non-JSON body: {exc}"}
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            reply = str(parsed["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            return {"ok": False, "error": f"endpoint returned an unexpected body: {exc}"}
        return {
            "ok": True,
            "base_url": base_url,
            "model": model,
            "latency_ms": latency_ms,
            "reply": reply[:80],
        }

    return router
