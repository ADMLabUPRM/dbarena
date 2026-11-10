import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text

from dbarena import __version__
from dbarena.actions.registry import EngineRegistry
from dbarena.catalog.migrations import ensure_schema
from dbarena.catalog.store import TaskCatalogStore
from dbarena.judge import build_judge_resolver
from gui.backend.app.embedding_store import EmbeddingService
from gui.backend.app.embeddings_router import build_embeddings_router, build_judge_config_router
from gui.backend.app.interface_router import build_interface_router
from gui.backend.app.session_store import EpisodeSessionStore, mark_orphaned_episodes
from gui.backend.app.sessions_router import build_sessions_router
from gui.backend.app.tasks_router import build_classification_router, build_tasks_router


def create_app(
    catalog: Any = None,
    registry: EngineRegistry | None = None,
    admin_url: str | None = None,
    episode_transaction_factory: Any = None,
) -> FastAPI:
    admin_url = admin_url if admin_url is not None else os.environ.get("ADMINDB_URL")
    admin_engine = create_engine(admin_url) if admin_url else None
    if catalog is None:
        if admin_engine is None:
            raise RuntimeError("ADMINDB_URL is required when no catalog is provided")
        catalog = TaskCatalogStore(admin_engine)
    if admin_engine is not None:
        ensure_schema(admin_engine)
        mark_orphaned_episodes(admin_engine)
    embed_service: EmbeddingService | None = None
    if admin_engine is not None:
        embed_service = EmbeddingService(
            admin_engine,
            ragdb_url=os.environ.get("RAGDB_URL"),
            catalog=catalog,
            env=dict(os.environ),
        )
        embed_service.ensure_tables()
        embed_service.bootstrap_from_env()
    session_store = EpisodeSessionStore(
        catalog,
        registry=registry,
        admin_engine=admin_engine,
        embed_resolver=embed_service.resolver() if embed_service is not None else None,
        embed_gate=embed_service.gate() if embed_service is not None else None,
        judge_resolver=build_judge_resolver(os.environ.get("JUDGE_URL")),
        episode_transaction_factory=episode_transaction_factory,
    )

    app = FastAPI(title="DBArena Admin API", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    cors_origins = os.environ.get("DBARENA_CORS_ORIGINS", "")
    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in cors_origins.split(",") if o.strip()],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "unexpected server error"}},
        )

    @app.get("/api/health")
    def health():
        database = False
        if admin_engine is not None:
            try:
                with admin_engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                database = True
            except Exception:
                database = False
        return {"status": "ok", "version": __version__, "database": database}

    app.include_router(build_interface_router())
    app.include_router(build_classification_router())
    app.include_router(build_tasks_router(catalog))
    app.include_router(build_sessions_router(session_store))
    app.include_router(build_embeddings_router(embed_service))
    if admin_engine is not None:
        app.include_router(build_judge_config_router(admin_engine))
    app.state.session_store = session_store
    app.state.admin_engine = admin_engine
    app.state.embed_service = embed_service
    return app
