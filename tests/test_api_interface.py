from fastapi.testclient import TestClient
from gui.backend.app.main import create_app

from tests.fakes import FakeTaskStore

ACTION_NAMES = [
    "run_sql",
    "describe_table",
    "describe_tables",
    "describe_schema",
    "submit_final_answer",
    "run_rag_query",
]
OBSERVATION_TYPES = ["query_result", "table_description", "schema_description", "episode_result", "error"]


def _client() -> TestClient:
    return TestClient(create_app(catalog=FakeTaskStore([]), registry=None, admin_url=None))


class TestInterfaceEndpoint:
    def test_actions_are_served_without_a_session(self):
        response = _client().get("/api/interface")
        assert response.status_code == 200
        actions = response.json()["actions"]
        assert [a["tool"] for a in actions] == ACTION_NAMES
        for action in actions:
            assert action["summary"]
            assert isinstance(action["arguments"], dict)
            assert action["example"]["tool"] == action["tool"]
        run_sql = actions[0]
        assert "required string" in run_sql["arguments"]["sql"]

    def test_observation_catalog_is_served(self):
        response = _client().get("/api/interface")
        observations = response.json()["observations"]
        assert [o["type"] for o in observations] == OBSERVATION_TYPES
        for entry in observations:
            assert entry["description"]
            assert entry["example"]["schema_version"] == "dbarena-observation/v1"
            assert entry["example"]["type"] == entry["type"]

    def test_endpoint_is_stable_across_calls(self):
        client = _client()
        first = client.get("/api/interface").json()
        second = client.get("/api/interface").json()
        assert first == second

    def test_endpoint_is_documented_and_read_only(self):
        client = _client()
        openapi = client.get("/api/openapi.json").json()
        assert "/api/interface" in openapi["paths"]
        assert set(openapi["paths"]["/api/interface"].keys()) == {"get"}
        assert client.get("/api/interface").status_code == 200
