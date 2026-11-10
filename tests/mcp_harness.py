"""Shared helpers for MCP tests: an in-memory client session harness and a fake backend client."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anyio
from mcp.shared.memory import create_connected_server_and_client_session
from mcp_server.client import BackendResponse

Handler = Callable[[str, str, Any], BackendResponse]


def with_session(server, scenario: Callable[[Any], Any]) -> Any:
    """Run ``scenario(session)`` against ``server`` over an in-memory MCP session (synchronous)."""

    async def runner():
        async with create_connected_server_and_client_session(server) as session:
            return await scenario(session)

    return anyio.run(runner)


def json_payload(result) -> Any:
    """Decode the JSON body of a successful tool result.

    FastMCP (mcp 1.30) wraps bare list returns in ``structuredContent`` as ``{"result": [...]}``
    and flattens them into one text block per item; dict returns carry the JSON text directly.
    """
    assert result.isError is False, f"expected success, got error: {_texts(result)}"
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return json.loads(_texts(result)[0])


def json_text(result) -> Any:
    """Decode the tool result's text content as JSON, asserting single-document framing.

    Listing tools must return exactly one text block holding one valid JSON document
    (see the wrap-mcp-list-results spec delta); more than one block means a list return
    was flattened by the SDK.
    """
    assert result.isError is False, f"expected success, got error: {_texts(result)}"
    texts = _texts(result)
    assert len(texts) == 1, f"expected exactly one text block, got {len(texts)}"
    return json.loads(texts[0])


def error_payload(result) -> dict:
    """Decode the error dict carried by an isError tool result.

    FastMCP prefixes tool errors with ``Error executing tool <name>: `` before the message.
    """
    assert result.isError is True, f"expected error, got: {_texts(result)}"
    text = _texts(result)[0]
    prefix = "Error executing tool "
    if text.startswith(prefix):
        _, _, rest = text.partition(": ")
        return json.loads(rest)
    return json.loads(text)


def _texts(result) -> list[str]:
    return [c.text for c in result.content if getattr(c, "type", "") == "text"]


class FakeBackendClient:
    """Records every proxied call and answers through a per-test handler."""

    def __init__(self, handler: Handler | None = None):
        self.calls: list[tuple[str, str, Any]] = []
        self.handler: Handler = handler if handler is not None else _default_handler

    def request(self, method: str, path: str, json_body: Any = None) -> BackendResponse:
        self.calls.append((method, path, json_body))
        return self.handler(method, path, json_body)

    def get(self, path: str) -> BackendResponse:
        return self.request("GET", path)

    def post(self, path: str, json_body: Any = None) -> BackendResponse:
        return self.request("POST", path, json_body)

    def put(self, path: str, json_body: Any = None) -> BackendResponse:
        return self.request("PUT", path, json_body)

    def delete(self, path: str) -> BackendResponse:
        return self.request("DELETE", path)

    def close(self) -> None:  # pragma: no cover - symmetry with BackendClient
        pass


def ok(body: Any, status: int = 200) -> BackendResponse:
    return BackendResponse(ok=True, status=status, body=body)


def failure(status: int | None, error: dict) -> BackendResponse:
    return BackendResponse(ok=False, status=status, error=error)


def _default_handler(method: str, path: str, body: Any) -> BackendResponse:
    return ok({})
