import pytest
from mcp_server.config import Settings
from mcp_server.server import build_mcp
from mcp_server.tools import TOOL_NAMES

from tests.mcp_harness import FakeBackendClient, error_payload, failure, json_payload, json_text, ok, with_session

TASK = {
    "task_id": "t1",
    "goal": "Answer the question.",
    "submission": "Call submit_final_answer with tuples [[1]].",
    "category": "OLAP",
    "task_type": "IS",
    "max_steps": 6,
    "sqlalchemy_url": "postgresql+psycopg://u:p@h:5432/db",
    "terminal_condition": {
        "expected_tuples": [[1]],
        "matching": {"numeric_tolerance": 0.01},
        "state_checks": [
            {"sql": "SELECT count(*) FROM orders", "expected_tuples": [[25]], "matching": {}}
        ],
        "rules": [{"sql": "SELECT o_totalprice FROM orders", "description": "price rule"}],
    },
    "query_timeout": 10,
    "row_limit": None,
}

REDACTED_TASK = {k: v for k, v in TASK.items() if k != "sqlalchemy_url"}
REDACTED_TASK["terminal_condition"] = {"state_checks": 1, "rules": 1}

TASK2 = dict(TASK, task_id="t2")
REDACTED_TASK2 = dict(REDACTED_TASK, task_id="t2")

CLASSIFICATIONS = {"categories": ["OLTP", "OLAP", "ANALYTICS", "RAG"], "task_types": ["NAV", "IS", "CC", "MIX"]}

STEP = {
    "step_number": 1,
    "action": {"tool": "describe_schema", "arguments": {}},
    "observation": {"schema_version": "dbarena-observation/v1", "type": "schema_description", "payload": {}},
    "reward": 0.0,
    "terminated": False,
    "truncated": False,
}

SESSION = {
    "session_id": "s1",
    "task_id": "t1",
    "read_only": False,
    "steps": [
        {
            "step_number": 0,
            "action": {"tool": "reset", "arguments": {"task_id": "t1"}},
            "observation": {"type": "schema_description"},
            "reward": 0.0,
            "terminated": False,
            "truncated": False,
        }
    ],
}


def _server(client):
    return build_mcp(Settings(), client)


async def _call(session, name, arguments):
    return await session.call_tool(name, arguments)


class TestTaskCatalogTools:
    def test_list_tasks(self):
        client = FakeBackendClient(
            lambda m, p, b: (
                ok([TASK, TASK2]) if p == "/api/tasks" else failure(500, {"code": "x", "message": "no"})
            )
        )
        result = with_session(_server(client), lambda s: _call(s, "list_tasks", {}))
        assert json_text(result) == {"tasks": [REDACTED_TASK, REDACTED_TASK2]}
        assert client.calls == [("GET", "/api/tasks", None)]

    def test_list_tasks_redacts_tuples_only_tasks(self):
        bare = dict(TASK, task_id="t-bare", terminal_condition={"expected_tuples": [[1]], "matching": {}})
        client = FakeBackendClient(lambda m, p, b: ok([bare]))
        result = with_session(_server(client), lambda s: _call(s, "list_tasks", {}))
        listing = json_payload(result)
        assert listing["tasks"][0]["terminal_condition"] == {"state_checks": 0, "rules": 0}
        assert listing["tasks"][0]["task_id"] == "t-bare"

    def test_list_tasks_forwards_classification_filters(self):
        client = FakeBackendClient(
            lambda m, p, b: ok([TASK]) if "category=OLTP" in p and "task_type=CC" in p else failure(500, {})
        )
        result = with_session(
            _server(client), lambda s: _call(s, "list_tasks", {"category": "OLTP", "task_type": "CC"})
        )
        assert json_text(result) == {"tasks": [REDACTED_TASK]}
        assert client.calls == [("GET", "/api/tasks?category=OLTP&task_type=CC", None)]

    def test_list_tasks_without_filters_sends_no_query(self):
        client = FakeBackendClient(lambda m, p, b: ok([]))
        with_session(_server(client), lambda s: _call(s, "list_tasks", {}))
        assert client.calls == [("GET", "/api/tasks", None)]

    def test_list_tasks_invalid_filter_surfaces_backend_error(self):
        client = FakeBackendClient(
            lambda m, p, b: failure(
                400,
                {
                    "code": "validation_error",
                    "message": "task validation failed",
                    "fields": {"category": ["must be one of: OLTP, OLAP, ANALYTICS, RAG"]},
                },
            )
        )
        result = with_session(_server(client), lambda s: _call(s, "list_tasks", {"category": "oltp"}))
        assert result.isError is True
        error = error_payload(result)
        assert error["fields"]["category"] == ["must be one of: OLTP, OLAP, ANALYTICS, RAG"]

    def test_list_task_classifications(self):
        client = FakeBackendClient(
            lambda m, p, b: ok(CLASSIFICATIONS) if p == "/api/task-classifications" else failure(500, {})
        )
        result = with_session(_server(client), lambda s: _call(s, "list_task_classifications", {}))
        assert json_payload(result) == CLASSIFICATIONS
        assert client.calls == [("GET", "/api/task-classifications", None)]

    def test_tool_surface_includes_classifications(self):
        assert "list_task_classifications" in TOOL_NAMES

    def test_get_task_quotes_the_id(self):
        client = FakeBackendClient(lambda m, p, b: ok(TASK))
        result = with_session(_server(client), lambda s: _call(s, "get_task", {"task_id": "a/b"}))
        assert json_payload(result) == REDACTED_TASK
        assert client.calls == [("GET", "/api/tasks/a%2Fb", None)]

    def test_task_bearing_results_never_carry_grading_details(self):
        calls = []

        def handler(m, p, b):
            calls.append((m, p, b))
            if m == "GET" and p == "/api/tasks":
                return ok([TASK])
            return ok(TASK)

        client = FakeBackendClient(handler)

        async def scenario(session):
            return [
                await session.call_tool("list_tasks", {}),
                await session.call_tool("get_task", {"task_id": "t1"}),
            ]

        results = with_session(_server(client), scenario)
        for result in results:
            assert result.isError is False
            payload = json_payload(result)
            tasks = payload["tasks"] if "tasks" in payload else [payload]
            for task in tasks:
                assert set(task["terminal_condition"]) == {"state_checks", "rules"}
                assert task["terminal_condition"] == {"state_checks": 1, "rules": 1}
                assert "sqlalchemy_url" not in task
                assert task["goal"] == TASK["goal"]
                assert task["submission"] == TASK["submission"]
        assert calls[0] == ("GET", "/api/tasks", None)
        assert calls[1] == ("GET", "/api/tasks/t1", None)


class TestSessionTools:
    def test_start_session_returns_id_and_initial_observation(self):
        client = FakeBackendClient(lambda m, p, b: ok(SESSION, status=201))
        result = with_session(_server(client), lambda s: _call(s, "start_session", {"task_id": "t1"}))
        payload = json_payload(result)
        assert payload["session_id"] == "s1"
        assert payload["steps"][0]["observation"]["type"] == "schema_description"
        assert client.calls == [("POST", "/api/sessions", {"task_id": "t1"})]

    def test_step_session_forwards_the_action(self):
        client = FakeBackendClient(lambda m, p, b: ok(STEP))
        arguments = {"tool": "run_sql", "arguments": {"sql": "SELECT 1"}}
        result = with_session(_server(client), lambda s: _call(s, "step_session", dict(arguments, session_id="s1")))
        payload = json_payload(result)
        assert payload["step_number"] == 1
        assert payload["observation"]["type"] == "schema_description"
        expected_call = ("POST", "/api/sessions/s1/step", {"tool": "run_sql", "arguments": {"sql": "SELECT 1"}})
        assert client.calls == [expected_call]

    def test_step_session_defaults_arguments_to_empty(self):
        client = FakeBackendClient(lambda m, p, b: ok(STEP))
        arguments = {"session_id": "s1", "tool": "describe_schema"}
        with_session(_server(client), lambda s: _call(s, "step_session", arguments))
        assert client.calls == [("POST", "/api/sessions/s1/step", {"tool": "describe_schema", "arguments": {}})]

    def test_stepping_a_closed_session_is_a_conflict_error(self):
        client = FakeBackendClient(
            lambda m, p, b: failure(409, {"code": "conflict", "message": "session is not active"})
        )
        arguments = {"session_id": "s1", "tool": "describe_schema"}
        result = with_session(_server(client), lambda s: _call(s, "step_session", arguments))
        error = error_payload(result)
        assert error["code"] == "conflict"
        assert "not active" in error["message"]

    def test_unknown_session_is_not_found(self):
        client = FakeBackendClient(lambda m, p, b: failure(404, {"code": "not_found", "message": "session not found"}))
        result = with_session(_server(client), lambda s: _call(s, "get_session", {"session_id": "ghost"}))
        assert error_payload(result)["code"] == "not_found"

    def test_reset_session(self):
        client = FakeBackendClient(lambda m, p, b: ok(SESSION))
        result = with_session(_server(client), lambda s: _call(s, "reset_session", {"session_id": "s1"}))
        assert json_payload(result)["task_id"] == "t1"
        assert client.calls == [("POST", "/api/sessions/s1/reset", None)]

    def test_get_session_returns_history(self):
        client = FakeBackendClient(lambda m, p, b: ok(SESSION))
        result = with_session(_server(client), lambda s: _call(s, "get_session", {"session_id": "s1"}))
        assert len(json_payload(result)["steps"]) == 1


class TestHealthAndInterface:
    def test_health(self):
        body = {"status": "ok", "version": "0.1.0", "database": True}
        client = FakeBackendClient(lambda m, p, b: ok(body))
        result = with_session(_server(client), lambda s: _call(s, "dbarena_health", {}))
        assert json_payload(result) == body
        assert client.calls == [("GET", "/api/health", None)]

    def test_get_interface_sourced_from_backend(self):
        body = {
            "actions": [{"tool": "run_sql", "summary": "s", "arguments": {"sql": "required string"}, "example": {}}],
            "observations": [{"type": "query_result", "description": "d", "example": {}}],
        }
        client = FakeBackendClient(lambda m, p, b: ok(body))
        result = with_session(_server(client), lambda s: _call(s, "get_interface", {}))
        assert json_payload(result) == body
        assert client.calls == [("GET", "/api/interface", None)]


class TestToolSurface:
    def test_exactly_the_specified_tools_are_served_with_details(self):
        client = FakeBackendClient()

        async def scenario(session):
            return await session.list_tools()

        listing = with_session(_server(client), scenario)
        names = [t.name for t in listing.tools]
        assert sorted(names) == sorted(TOOL_NAMES)
        by_name = {t.name: t for t in listing.tools}
        for name in TOOL_NAMES:
            tool = by_name[name]
            assert tool.description, f"tool {name} has no description"
            assert tool.inputSchema.get("type") == "object", f"tool {name} has no object schema"
        step_schema = by_name["step_session"].inputSchema
        assert set(step_schema.get("properties", {})) == {"session_id", "tool", "arguments"}


class TestOutageResilience:
    def test_backend_outage_then_recovery(self):
        state = {"calls": 0}

        def handler(m, p, b):
            state["calls"] += 1
            if state["calls"] == 1:
                error = {
                    "code": "backend_unreachable",
                    "message": "cannot reach the DBArena backend: connection refused",
                }
                return failure(None, error)
            return ok({"status": "ok"})

        client = FakeBackendClient(handler)

        async def scenario(session):
            first = await session.call_tool("dbarena_health", {})
            second = await session.call_tool("dbarena_health", {})
            return first, second

        first, second = with_session(_server(client), scenario)
        assert first.isError is True
        assert error_payload(first)["code"] == "backend_unreachable"
        assert second.isError is False
        assert json_payload(second) == {"status": "ok"}

    @pytest.mark.parametrize(
        "tool,arguments",
        [("dbarena_health", {}), ("list_tasks", {}), ("start_session", {"task_id": "t1"})],
    )
    def test_outage_never_raises_into_the_protocol(self, tool, arguments):
        client = FakeBackendClient(lambda m, p, b: failure(None, {"code": "backend_unreachable", "message": "down"}))
        result = with_session(_server(client), lambda s: _call(s, tool, arguments))
        assert result.isError is True
        assert error_payload(result)["code"] == "backend_unreachable"
