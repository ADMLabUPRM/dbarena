"""Thin synchronous HTTP client for the DBArena admin API.

The client never raises on HTTP or connection errors: every call returns a
:class:`BackendResponse` that tools turn into either a result or an MCP error
result, so a backend outage can never take the MCP server down.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 120.0

_CODE_BY_STATUS = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    500: "internal_error",
}


@dataclass(frozen=True)
class BackendResponse:
    """Outcome of one backend call: ``ok`` results carry ``body``; failures carry ``error``."""

    ok: bool
    status: int | None = None
    body: Any = None
    error: dict[str, Any] | None = None


class BackendClient:
    """Synchronous client for one backend base URL."""

    def __init__(self, base_url: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, transport=None):
        self._transport = transport
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    def request(self, method: str, path: str, json_body: Any = None) -> BackendResponse:
        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            detail = str(exc) or exc.__class__.__name__
            return BackendResponse(
                ok=False,
                error={"code": "backend_unreachable", "message": f"cannot reach the DBArena backend: {detail}"},
            )
        body = _decode(response)
        if 200 <= response.status_code < 300:
            return BackendResponse(ok=True, status=response.status_code, body=body)
        return BackendResponse(ok=False, status=response.status_code, body=body, error=_error_from(response, body))

    def get(self, path: str) -> BackendResponse:
        return self.request("GET", path)

    def post(self, path: str, json_body: Any = None) -> BackendResponse:
        return self.request("POST", path, json_body)

    def put(self, path: str, json_body: Any = None) -> BackendResponse:
        return self.request("PUT", path, json_body)

    def delete(self, path: str) -> BackendResponse:
        return self.request("DELETE", path)

    def close(self) -> None:
        if self._transport is not None:
            return
        self._client.close()


def _decode(response: httpx.Response) -> Any:
    if "json" not in response.headers.get("content-type", ""):
        text = response.text.strip()
        return text or None
    try:
        return response.json()
    except ValueError:
        return response.text


def _error_from(response: httpx.Response, body: Any) -> dict[str, Any]:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        error = dict(body["error"])
        error.setdefault("code", _code_for(response.status_code))
        error.setdefault("message", response.reason_phrase or "backend error")
        return error
    if isinstance(body, dict) and "detail" in body:
        return {"code": _code_for(response.status_code), "message": _detail_message(body["detail"])}
    return {
        "code": _code_for(response.status_code),
        "message": _detail_message(body if body is not None else (response.reason_phrase or "backend error")),
    }


def _detail_message(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    import json

    try:
        return json.dumps(detail)
    except TypeError:
        return str(detail)


def _code_for(status: int) -> str:
    return _CODE_BY_STATUS.get(status, "backend_error")
