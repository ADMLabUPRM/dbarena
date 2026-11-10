import httpx
from mcp_server.client import BackendClient


def _client(handler) -> BackendClient:
    return BackendClient("http://backend.test", transport=httpx.MockTransport(handler))


class TestSuccessMapping:
    def test_json_body_is_decoded(self):
        client = _client(lambda request: httpx.Response(200, json={"status": "ok"}))
        response = client.get("/api/health")
        assert response.ok is True
        assert response.status == 200
        assert response.body == {"status": "ok"}
        assert response.error is None

    def test_post_and_put_bodies_are_sent(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["json"] = json_of(request)
            return httpx.Response(201, json={"created": True})

        client = _client(handler)
        response = client.post("/api/tasks", {"task_id": "t1"})
        assert response.ok is True
        assert seen == {"method": "POST", "path": "/api/tasks", "json": {"task_id": "t1"}}

        client.request("PUT", "/api/tasks/t1", {"max_steps": 3})
        assert seen["method"] == "PUT"

    def test_empty_204_body_decodes_to_none(self):
        client = _client(lambda request: httpx.Response(204))
        response = client.delete("/api/tasks/t1")
        assert response.ok is True
        assert response.body is None


def json_of(request: httpx.Request):
    import json

    return json.loads(request.content) if request.content else None


class TestErrorMapping:
    def test_structured_error_body_is_preserved(self):
        body = {
            "error": {
                "code": "validation_error",
                "message": "task validation failed",
                "fields": {"max_steps": ["must be a positive integer"]},
            }
        }
        client = _client(lambda request: httpx.Response(400, json=body))
        response = client.post("/api/tasks", {})
        assert response.ok is False
        assert response.status == 400
        assert response.error["code"] == "validation_error"
        assert response.error["message"] == "task validation failed"
        assert response.error["fields"] == {"max_steps": ["must be a positive integer"]}

    def test_detail_body_gets_status_derived_code(self):
        client = _client(lambda request: httpx.Response(404, json={"detail": "task not found"}))
        response = client.get("/api/tasks/ghost")
        assert response.error == {"code": "not_found", "message": "task not found"}

    def test_conflict_detail_maps_to_conflict(self):
        client = _client(lambda request: httpx.Response(409, json={"detail": "session is not active"}))
        response = client.post("/api/sessions/s1/step", {"tool": "describe_schema", "arguments": {}})
        assert response.error == {"code": "conflict", "message": "session is not active"}

    def test_unstructured_error_body_falls_back_to_backend_error(self):
        client = _client(lambda request: httpx.Response(500, text="boom"))
        response = client.get("/api/tasks")
        assert response.ok is False
        assert response.error["code"] == "internal_error"
        assert "boom" in response.error["message"]


class TestConnectionFailures:
    def test_connect_error_becomes_backend_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _client(handler)
        response = client.get("/api/health")
        assert response.ok is False
        assert response.status is None
        assert response.error["code"] == "backend_unreachable"
        assert "connection refused" in response.error["message"]

    def test_timeout_becomes_backend_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        client = _client(handler)
        response = client.get("/api/tasks")
        assert response.error["code"] == "backend_unreachable"
