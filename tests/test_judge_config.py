import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gui.backend.app.embeddings_router import build_judge_config_router
from sqlalchemy import create_engine, text

from dbarena.catalog.migrations import ensure_embedding_schema

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture()
def admin_engine(pg_url):
    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text((open("db/init/001-schema.sql", encoding="utf-8")).read()))
    ensure_embedding_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def client(admin_engine):
    app = FastAPI()
    app.include_router(build_judge_config_router(admin_engine))
    return TestClient(app)


class _StubLlm:
    """Local OpenAI-shaped stub: configurable status/body, captures the last request."""

    def __init__(self):
        self.status = 200
        self.body = {
            "choices": [{"message": {"role": "assistant", "content": " OK "}}]
        }
        self.last_path = ""
        self.last_auth = ""
        self.last_payload: dict = {}
        self._server = HTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_port}/v1"

    def _make_handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                stub.last_path = self.path
                stub.last_auth = self.headers.get("Authorization", "")
                stub.last_payload = json.loads(self.rfile.read(length) or b"{}")
                payload = json.dumps(stub.body).encode("utf-8")
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        return Handler

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def stub_llm():
    stub = _StubLlm()
    yield stub
    stub.stop()


def _save_config(client, base_url, model, api_key=""):
    response = client.put(
        "/api/judge/config",
        json={"base_url": base_url, "model": model, "api_key": api_key},
    )
    assert response.status_code == 200


def test_success_uses_typed_values_and_stored_key_fallback(client, stub_llm):
    _save_config(client, stub_llm.url, "stored-model", api_key="sk-stored")
    response = client.post(
        "/api/judge/test", json={"base_url": stub_llm.url, "model": "typed-model"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["base_url"] == stub_llm.url
    assert body["model"] == "typed-model"
    assert body["reply"] == "OK"
    assert body["latency_ms"] >= 0
    assert stub_llm.last_auth == "Bearer sk-stored"
    assert stub_llm.last_payload["model"] == "typed-model"
    assert stub_llm.last_payload["temperature"] == 0


def test_stored_fallback_resolves_everything(client, stub_llm):
    _save_config(client, stub_llm.url, "stored-model", api_key="sk-stored")
    response = client.post("/api/judge/test", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["model"] == "stored-model"
    assert stub_llm.last_auth == "Bearer sk-stored"


def test_base_url_without_v1_is_normalized(client, admin_engine, stub_llm):
    _save_config(client, f"http://127.0.0.1:{stub_llm._server.server_port}", "m")
    response = client.post("/api/judge/test", json={})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert stub_llm.last_path == "/v1/chat/completions"


def test_http_401_reports_failure(client, stub_llm):
    stub_llm.status = 401
    stub_llm.body = {"detail": "bad key"}
    response = client.post("/api/judge/test", json={"base_url": stub_llm.url, "model": "m"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "HTTP 401" in body["error"]


def test_non_openai_body_reports_failure(client, stub_llm):
    stub_llm.body = {"unexpected": "shape"}
    response = client.post("/api/judge/test", json={"base_url": stub_llm.url, "model": "m"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "unexpected body" in body["error"]


def test_connection_refused_reports_failure(client):
    response = client.post(
        "/api/judge/test", json={"base_url": "http://127.0.0.1:9", "model": "m"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "unreachable" in body["error"]


def test_nothing_to_test_is_refused(client, admin_engine):
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM judge_config"))
    response = client.post("/api/judge/test", json={})
    assert response.status_code == 400
    assert "nothing to test" in response.json()["detail"]


def test_empty_key_sends_no_auth_header(client, stub_llm):
    _save_config(client, stub_llm.url, "m")
    response = client.post("/api/judge/test", json={})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert stub_llm.last_auth == ""
