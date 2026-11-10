"""The DBArena MCP tool surface: nine read/run tools, each mapping to one admin API operation.

Tool names, descriptions, and argument schemas are defined statically here so the
MCP tool list is stable for clients; dynamic action/observation documentation is
fetched at call time from the backend via ``get_interface``. Task catalog writes
(create, update, validate, delete) are deliberately not exposed; they belong to
the admin GUI.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mcp_server.client import BackendClient

TOOL_NAMES = (
    "dbarena_health",
    "list_task_classifications",
    "list_tasks",
    "get_task",
    "start_session",
    "step_session",
    "reset_session",
    "get_session",
    "get_interface",
)

SERVER_INSTRUCTIONS = (
    "Tools for the DBArena database-task benchmark: inspect the task catalog, run benchmark episodes "
    "against SQL databases one action at a time, and inspect the environment's action interface. "
    "Start with get_interface to learn the available episode actions, start_session to begin an "
    "episode for a task, then step_session one action at a time."
)



def register_tools(mcp: FastMCP, client: BackendClient) -> None:
    """Register all nine tools on ``mcp``, proxying every call through ``client``."""

    @mcp.tool()
    def dbarena_health() -> dict:
        """Check the DBArena backend: overall status, API version, and admin-database reachability."""
        return _call(client, "GET", "/api/health")

    @mcp.tool()
    def list_task_classifications() -> dict:
        """List the allowed task classifications: application-domain categories and intent task types."""
        return _call(client, "GET", "/api/task-classifications")

    @mcp.tool()
    def list_tasks(category: str | None = None, task_type: str | None = None) -> dict:
        """List catalog tasks, optionally filtered by classification.

        Returns ``{"tasks": [...]}``: one JSON object wrapping the backend's task array,
        so the result is a single JSON document rather than one text block per task.
        Each task's ``terminal_condition`` is returned as a counts-only summary
        (``{"state_checks": N, "rules": M}``) and ``sqlalchemy_url`` is omitted;
        the expected answer and the target database URL are not exposed.

        Args:
            category: optional filter by application-domain category (OLTP, OLAP, ANALYTICS, or RAG)
            task_type: optional filter by intent type (NAV, IS, CC, or MIX)
        """
        path = "/api/tasks" + _classification_query(category, task_type)
        return {"tasks": [_redact_task(task) for task in _call(client, "GET", path)]}

    @mcp.tool()
    def get_task(task_id: str) -> dict:
        """Fetch one task by id, including goal, limits, and terminal condition.

        The ``terminal_condition`` is returned as a counts-only summary
        (``{"state_checks": N, "rules": M}``) and ``sqlalchemy_url`` is omitted;
        the expected answer and the target database URL are not exposed.

        Args:
            task_id: the task identifier, e.g. "DBA-A06-001"
        """
        return _redact_task(_call(client, "GET", f"/api/tasks/{_quote(task_id)}"))

    @mcp.tool()
    def start_session(task_id: str) -> dict:
        """Start a benchmark episode for a task. Returns the session id, the task, and the initial
        observation (schema description with available actions and the observation catalog).

        Args:
            task_id: identifier of the task to run an episode for
        """
        return _call(client, "POST", "/api/sessions", {"task_id": task_id})

    @mcp.tool()
    def step_session(session_id: str, tool: str, arguments: dict[str, Any] | None = None) -> dict:
        """Apply one action to a running episode and receive the observation, reward, and episode state.

        Args:
            session_id: session id returned by start_session
            tool: one of the episode actions: run_sql, describe_table, describe_tables, describe_schema,
                submit_final_answer (see get_interface for argument documentation)
            arguments: action arguments, e.g. {"sql": "..."} for run_sql or {"tuples": [...]} for
                submit_final_answer

        Note: run_sql rejects transaction control statements (COMMIT, END, BEGIN, START TRANSACTION,
        SAVEPOINT, RELEASE, ROLLBACK, ABORT, PREPARE TRANSACTION); episode writes are isolated and
        rolled back automatically when the episode ends.
        """
        body = {"tool": tool, "arguments": arguments if arguments is not None else {}}
        return _call(client, "POST", f"/api/sessions/{_quote(session_id)}/step", body)

    @mcp.tool()
    def reset_session(session_id: str) -> dict:
        """Reset a session to a fresh episode of the same task, clearing its step history.

        Args:
            session_id: session id returned by start_session
        """
        return _call(client, "POST", f"/api/sessions/{_quote(session_id)}/reset")

    @mcp.tool()
    def get_session(session_id: str) -> dict:
        """Fetch one session with its full step history (actions, observations, rewards).

        Args:
            session_id: session id returned by start_session
        """
        return _call(client, "GET", f"/api/sessions/{_quote(session_id)}")

    @mcp.tool()
    def get_interface() -> dict:
        """Fetch the environment's self-describing interface: every episode action (name, summary,
        argument documentation, example) and the observation-type catalog. Call this before issuing
        step_session actions."""
        return _call(client, "GET", "/api/interface")


def _call(client: BackendClient, method: str, path: str, json_body: Any = None) -> Any:
    response = client.request(method, path, json_body)
    if not response.ok:
        raise ToolError(json.dumps(response.error))
    return response.body


def _redact_task(task: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a backend task with grading details and the target database URL removed.

    ``terminal_condition`` is replaced by a counts-only summary
    (``{"state_checks": N, "rules": M}``) and ``sqlalchemy_url`` is dropped, so
    expected answers and connection strings never appear in a tool result.
    """
    condition = task.get("terminal_condition")
    if not isinstance(condition, dict):
        condition = {}
    checks = condition.get("state_checks")
    rules = condition.get("rules")
    redacted = dict(task)
    redacted.pop("sqlalchemy_url", None)
    redacted["terminal_condition"] = {
        "state_checks": len(checks) if isinstance(checks, list) else 0,
        "rules": len(rules) if isinstance(rules, list) else 0,
    }
    return redacted


def _classification_query(category: str | None, task_type: str | None) -> str:
    from urllib.parse import urlencode

    params: dict[str, str] = {}
    if category is not None:
        params["category"] = category
    if task_type is not None:
        params["task_type"] = task_type
    if not params:
        return ""
    return "?" + urlencode(params)


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
