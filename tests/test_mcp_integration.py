"""Full MCP round trip against a real backend server and an ephemeral PostgreSQL."""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from mcp_server.client import BackendClient
from mcp_server.config import Settings
from mcp_server.server import build_mcp
from mcp_server.tools import TOOL_NAMES
from sqlalchemy import create_engine, text

from tests.mcp_harness import error_payload, json_payload, with_session

EXPECTED_ROWS = [[1, "alpha"], [2, "beta"], [3, "gamma"]]

TASK = {
    "task_id": "mcp-e2e-1",
    "goal": "Read every row of the seeded table.",
    "submission": "Call submit_final_answer with the tuples you produced.",
    "category": "OLAP",
    "task_type": "IS",
    "max_steps": 6,
    "sqlalchemy_url": None,
    "terminal_condition": {"expected_tuples": EXPECTED_ROWS, "matching": {}},
    "query_timeout": 10,
    "row_limit": None,
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(base: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            time.sleep(0.25)
    raise RuntimeError("backend did not become healthy in time")


@pytest.fixture()
def backend(pg_url):
    """Real backend server (uvicorn subprocess) against the ephemeral PostgreSQL."""
    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text(open("db/init/001-schema.sql", encoding="utf-8").read()))
    engine.dispose()

    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests._mcp_backend_runner:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=os.getcwd(),
        env={**os.environ, "ADMINDB_URL": pg_url},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(base)
        yield BackendClient(base), pg_url
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        engine = create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM episode_steps"))
            conn.execute(text("DELETE FROM episodes"))
            conn.execute(text("DELETE FROM tasks"))
        engine.dispose()


def _episode_task(pg_url: str) -> dict:
    return dict(TASK, sqlalchemy_url=pg_url)


def _seed_task(client: BackendClient, pg_url: str) -> dict:
    """Seed the episode task through the backend API (the MCP server no longer exposes task writes)."""
    response = client.post("/api/tasks", _episode_task(pg_url))
    assert response.ok, response.error
    return response.body


class TestMcpRoundTrip:
    def test_tools_list_seed_and_full_episode(self, backend):
        client, pg_url = backend
        server = build_mcp(Settings(), client)
        seeded = _seed_task(client, pg_url)

        async def scenario(session):
            listing = await session.list_tools()
            started = await session.call_tool("start_session", {"task_id": TASK["task_id"]})
            session_id = json_payload(started)["session_id"]
            schema_step = await session.call_tool("step_session", {"session_id": session_id, "tool": "describe_schema"})
            query_step = await session.call_tool(
                "step_session",
                {
                    "session_id": session_id,
                    "tool": "run_sql",
                    "arguments": {"sql": "SELECT id, name FROM arena_item ORDER BY id"},
                },
            )
            submitted = await session.call_tool(
                "step_session",
                {"session_id": session_id, "tool": "submit_final_answer", "arguments": {"tuples": EXPECTED_ROWS}},
            )
            history = await session.call_tool("get_session", {"session_id": session_id})
            return listing, started, schema_step, query_step, submitted, history, session_id

        listing, started, schema_step, query_step, submitted, history, session_id = with_session(
            server, scenario
        )

        assert sorted(t.name for t in listing.tools) == sorted(TOOL_NAMES)
        assert seeded["task_id"] == TASK["task_id"]

        started_body = json_payload(started)
        assert started_body["steps"][0]["observation"]["type"] == "schema_description"

        schema_body = json_payload(schema_step)
        assert schema_body["step_number"] == 1
        schema_tables = [t["name"] for t in schema_body["observation"]["payload"]["tables"]]
        assert "arena_item" in schema_tables

        query_body = json_payload(query_step)
        assert query_body["observation"]["type"] == "query_result"
        assert query_body["observation"]["payload"]["rows"] == EXPECTED_ROWS

        submitted_body = json_payload(submitted)
        assert submitted_body["terminated"] is True
        assert submitted_body["reward"] == 1.0
        assert submitted_body["observation"]["payload"]["matched"] is True

        assert len(json_payload(history)["steps"]) == 4

        engine = create_engine(pg_url)
        with engine.connect() as conn:
            episode = conn.execute(
                text("SELECT status, reward FROM episodes WHERE episode_id = CAST(:id AS uuid)"),
                {"id": session_id},
            ).one()
        engine.dispose()
        assert episode[0] == "submitted"
        assert float(episode[1]) == 1.0

    def test_stepping_after_submit_conflicts(self, backend):
        client, pg_url = backend
        server = build_mcp(Settings(), client)
        _seed_task(client, pg_url)

        async def scenario(session):
            started = await session.call_tool("start_session", {"task_id": TASK["task_id"]})
            session_id = json_payload(started)["session_id"]
            await session.call_tool(
                "step_session",
                {"session_id": session_id, "tool": "submit_final_answer", "arguments": {"tuples": EXPECTED_ROWS}},
            )
            late = await session.call_tool("step_session", {"session_id": session_id, "tool": "describe_schema"})
            return late

        late = with_session(server, scenario)
        assert late.isError is True
        assert error_payload(late)["code"] == "conflict"
