from fastapi import FastAPI
from fastapi.testclient import TestClient
from gui.backend.app.main import create_app

from dbarena.actions.base import FakeAdapter, SqlResult
from dbarena.actions.registry import EngineRegistry
from tests.fakes import FakeEpisodeTransaction, FakeTaskStore
from tests.test_api import VALID_TASK, _client


def _app() -> FastAPI:
    from dbarena.catalog.validation import parse_task_payload

    catalog = FakeTaskStore([parse_task_payload(VALID_TASK)])
    registry = EngineRegistry()
    registry.register(
        "postgresql",
        FakeAdapter(result=SqlResult(columns=["id"], rows=[[1]], fetch_limited=False), schema=[]),
    )
    return create_app(
        catalog=catalog,
        registry=registry,
        admin_url=None,
        episode_transaction_factory=lambda engine, timeout: FakeEpisodeTransaction(),
    )


class TestCorsLockedDownByDefault:
    def test_no_cors_headers_without_configuration(self):
        client = TestClient(_app())
        response = client.options(
            "/api/tasks",
            headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
        )
        assert "access-control-allow-origin" not in response.headers

    def test_cors_only_with_explicit_env(self, monkeypatch):
        monkeypatch.setenv("DBARENA_CORS_ORIGINS", "http://localhost:5173")
        client = TestClient(_app())
        response = client.options(
            "/api/tasks",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
        )
        assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_configured_cors_rejects_other_origins(self, monkeypatch):
        monkeypatch.setenv("DBARENA_CORS_ORIGINS", "http://localhost:5173")
        client = TestClient(_app())
        response = client.options(
            "/api/tasks",
            headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
        )
        assert "access-control-allow-origin" not in response.headers


class TestSafeErrorResponses:
    def test_unhandled_exception_returns_structured_500_without_traceback(self, monkeypatch):
        app = _app()
        client = TestClient(app, raise_server_exceptions=False)

        def boom(session_id, tool, arguments):
            raise RuntimeError("secret stack details: /app/gui/backend/app/session_store.py")

        monkeypatch.setattr(app.state.session_store, "step", boom)
        session_id = client.post("/api/sessions", json={"task_id": "api-1"}).json()["session_id"]
        response = client.post(f"/api/sessions/{session_id}/step", json={"tool": "run_sql", "arguments": {}})
        assert response.status_code == 500
        body = response.json()
        assert body["error"]["code"] == "internal_error"
        assert "secret" not in response.text
        assert "RuntimeError" not in response.text

    def test_http_errors_are_json(self):
        client = _client()
        response = client.get("/api/tasks/ghost")
        assert response.status_code == 404
        assert response.json()["detail"] == "task not found"

    def test_validation_errors_never_leak_payloads(self):
        client = _client()
        response = client.post("/api/tasks", json=dict(VALID_TASK, task_id="leak", max_steps=-1))
        body = response.text
        assert response.status_code == 400
        assert "sqlalchemy_url" not in body
