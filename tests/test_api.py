import pytest
from fastapi.testclient import TestClient
from gui.backend.app.main import create_app

from dbarena.actions.base import FakeAdapter, SqlResult
from dbarena.actions.registry import EngineRegistry
from tests.fakes import FakeEpisodeTransaction, FakeTaskStore

VALID_TASK = {
    "task_id": "api-1",
    "goal": "Answer the question.",
    "submission": "Call submit_final_answer with tuples [[1, \"alpha\"]].",
    "category": "OLAP",
    "task_type": "IS",
    "max_steps": 6,
    "sqlalchemy_url": "postgresql+psycopg://u:p@h:5432/db",
    "terminal_condition": {"expected_tuples": [[1, "alpha"]], "matching": {"numeric_tolerance": 0.01}},
    "query_timeout": 20,
    "row_limit": 50,
}


def _registry() -> EngineRegistry:
    registry = EngineRegistry()
    registry.register(
        "postgresql",
        FakeAdapter(
            result=SqlResult(columns=["id", "name"], rows=[[1, "alpha"]], fetch_limited=False),
            schema=[],
        ),
    )
    return registry


def _client(catalog=None) -> TestClient:
    if catalog is None:
        from dbarena.catalog.validation import parse_task_payload

        catalog = FakeTaskStore([parse_task_payload(VALID_TASK)])
    app = create_app(
        catalog=catalog,
        registry=_registry(),
        admin_url=None,
        episode_transaction_factory=lambda engine, timeout: FakeEpisodeTransaction(),
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return _client()


class TestHealth:
    def test_health(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] is False


class TestTaskCrud:
    def test_create_and_get_round_trip(self, client):
        payload = dict(VALID_TASK, task_id="api-create")
        response = client.post("/api/tasks", json=payload)
        assert response.status_code == 201
        created = response.json()
        assert created["task_id"] == "api-create"
        fetched = client.get("/api/tasks/api-create")
        assert fetched.status_code == 200
        assert fetched.json() == created

    def test_create_invalid_returns_field_errors(self, client):
        bad = dict(VALID_TASK, task_id="bad", max_steps=0, goal=" ")
        response = client.post("/api/tasks", json=bad)
        assert response.status_code == 400
        fields = response.json()["error"]["fields"]
        assert set(fields) == {"max_steps", "goal"}

    def test_create_duplicate_conflict(self, client):
        response = client.post("/api/tasks", json=dict(VALID_TASK, task_id="api-1"))
        assert response.status_code == 409

    def test_list(self, client):
        response = client.get("/api/tasks")
        assert response.status_code == 200
        assert any(t["task_id"] == "api-1" for t in response.json())

    def test_get_missing_404(self, client):
        assert client.get("/api/tasks/ghost").status_code == 404

    def test_update(self, client):
        response = client.put("/api/tasks/api-1", json=dict(VALID_TASK, max_steps=9))
        assert response.status_code == 200
        assert client.get("/api/tasks/api-1").json()["max_steps"] == 9

    def test_update_id_mismatch_is_field_error(self, client):
        response = client.put("/api/tasks/api-1", json=dict(VALID_TASK, task_id="other"))
        assert response.status_code == 400
        assert "task_id" in response.json()["error"]["fields"]

    def test_update_missing_404(self, client):
        fresh = _client()
        response = fresh.put("/api/tasks/ghost", json=dict(VALID_TASK, task_id="ghost"))
        assert response.status_code == 404

    def test_delete(self, client):
        client.post("/api/tasks", json=dict(VALID_TASK, task_id="doomed"))
        assert client.delete("/api/tasks/doomed").status_code == 204
        assert client.delete("/api/tasks/doomed").status_code == 404

    def test_validate_endpoint(self, client):
        ok = client.post("/api/tasks/api-1/validate", json=VALID_TASK)
        assert ok.status_code == 200
        assert ok.json()["valid"] is True
        bad = client.post("/api/tasks/api-1/validate", json=dict(VALID_TASK, query_timeout=0))
        assert bad.status_code == 400


class TestTaskClassifications:
    def test_classifications_listing(self):
        client = _client()
        response = client.get("/api/task-classifications")
        assert response.status_code == 200
        assert response.json() == {
            "categories": ["OLTP", "OLAP", "ANALYTICS", "RAG"],
            "task_types": ["NAV", "IS", "CC", "MIX"],
        }

    def test_create_persists_classification(self, client):
        response = client.post(
            "/api/tasks",
            json=dict(VALID_TASK, task_id="api-classified", category="OLTP", task_type="CC"),
        )
        assert response.status_code == 201
        fetched = client.get("/api/tasks/api-classified").json()
        assert fetched["category"] == "OLTP"
        assert fetched["task_type"] == "CC"

    def test_create_rejects_invalid_classification(self, client):
        response = client.post("/api/tasks", json=dict(VALID_TASK, task_id="api-bad-class", category="oltp"))
        assert response.status_code == 400
        fields = response.json()["error"]["fields"]
        assert fields["category"] == ["must be one of: OLTP, OLAP, ANALYTICS, RAG"]

        response = client.post("/api/tasks", json=dict(VALID_TASK, task_id="api-bad-class", task_type="READ"))
        assert response.status_code == 400
        fields = response.json()["error"]["fields"]
        assert fields["task_type"] == ["must be one of: NAV, IS, CC, MIX"]

    def test_list_filters_by_classification(self):
        store = FakeTaskStore()
        from dbarena.catalog.validation import parse_task_payload

        store.create(parse_task_payload(dict(VALID_TASK, task_id="api-1", category="OLAP", task_type="IS")))
        store.create(parse_task_payload(dict(VALID_TASK, task_id="api-2", category="OLTP", task_type="CC")))
        client = _client(catalog=store)

        olap = client.get("/api/tasks", params={"category": "OLAP"}).json()
        assert [t["task_id"] for t in olap] == ["api-1"]
        both = client.get("/api/tasks", params={"category": "OLTP", "task_type": "CC"}).json()
        assert [t["task_id"] for t in both] == ["api-2"]
        everyone = client.get("/api/tasks").json()
        assert len(everyone) == 2

    def test_list_rejects_invalid_filters(self):
        client = _client()
        response = client.get("/api/tasks", params={"category": "oltp"})
        assert response.status_code == 400
        fields = response.json()["error"]["fields"]
        assert fields["category"] == ["must be one of: OLTP, OLAP, ANALYTICS, RAG"]

        response = client.get("/api/tasks", params={"task_type": "nope"})
        assert response.status_code == 400
        fields = response.json()["error"]["fields"]
        assert fields["task_type"] == ["must be one of: NAV, IS, CC, MIX"]


class TestSessions:
    def test_full_fake_session_round_trip(self):
        client = _client()
        created = client.post("/api/sessions", json={"task_id": "api-1"})
        assert created.status_code == 201
        body = created.json()
        session_id = body["session_id"]
        assert body["steps"][0]["observation"]["type"] == "schema_description"

        stepped = client.post(f"/api/sessions/{session_id}/step", json={"tool": "describe_schema", "arguments": {}})
        assert stepped.status_code == 200
        assert stepped.json()["step_number"] == 1
        assert stepped.json()["observation"]["type"] == "schema_description"

        query = client.post(
            f"/api/sessions/{session_id}/step",
            json={"tool": "run_sql", "arguments": {"sql": "SELECT id, name FROM t"}},
        )
        assert query.json()["observation"]["type"] == "query_result"
        assert query.json()["observation"]["payload"]["rows"] == [[1, "alpha"]]

        submitted = client.post(
            f"/api/sessions/{session_id}/step",
            json={"tool": "submit_final_answer", "arguments": {"tuples": [[1, "alpha"]]}},
        )
        assert submitted.json()["terminated"] is True
        assert submitted.json()["reward"] == 1.0
        assert submitted.json()["observation"]["payload"]["matched"] is True

        after = client.post(f"/api/sessions/{session_id}/step", json={"tool": "describe_schema", "arguments": {}})
        assert after.status_code == 409

    def test_invalid_tool_counts_as_error_step(self):
        client = _client()
        session_id = client.post("/api/sessions", json={"task_id": "api-1"}).json()["session_id"]
        stepped = client.post(f"/api/sessions/{session_id}/step", json={"tool": "nope", "arguments": {}})
        assert stepped.status_code == 200
        assert stepped.json()["observation"]["payload"]["code"] == "invalid_action"
        assert stepped.json()["step_number"] == 1

    def test_unknown_session_404(self):
        client = _client()
        assert client.get("/api/sessions/missing").status_code == 404
        assert (
            client.post("/api/sessions/missing/step", json={"tool": "describe_schema", "arguments": {}}).status_code
            == 404
        )

    def test_create_session_unknown_task_400(self):
        client = _client()
        response = client.post("/api/sessions", json={"task_id": "ghost"})
        assert response.status_code == 400
        assert "cannot start session" in response.json()["detail"]

    def test_reset_session(self):
        client = _client()
        session_id = client.post("/api/sessions", json={"task_id": "api-1"}).json()["session_id"]
        client.post(f"/api/sessions/{session_id}/step", json={"tool": "describe_schema", "arguments": {}})
        reset = client.post(f"/api/sessions/{session_id}/reset")
        assert reset.status_code == 200
        assert reset.json()["steps"][0]["observation"]["type"] == "schema_description"
        assert len(reset.json()["steps"]) == 1

    def test_create_session_requires_task_id(self):
        client = _client()
        assert client.post("/api/sessions", json={}).status_code == 400
