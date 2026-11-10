"""Configuration for the DBArena MCP server: env vars with CLI-flag precedence."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_TRANSPORT = "stdio"
DEFAULT_PORT = 8090
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_SESSION_IDLE_TIMEOUT: float | None = None
VALID_TRANSPORTS = ("stdio", "streamable-http")

ENV_API_URL = "DBARENA_API_URL"
ENV_TRANSPORT = "MCP_TRANSPORT"
ENV_PORT = "MCP_PORT"
ENV_HTTP_HOST = "MCP_HTTP_HOST"
ENV_TIMEOUT = "DBARENA_TIMEOUT_SECONDS"
ENV_SESSION_IDLE_TIMEOUT = "MCP_SESSION_IDLE_TIMEOUT"


@dataclasses.dataclass(frozen=True)
class Settings:
    """Resolved server settings.

    ``http_host``/``port`` and ``session_idle_timeout`` only apply to the streamable-HTTP transport.
    ``session_idle_timeout`` is the seconds of inactivity after which a stateful session is reaped;
    ``None`` disables expiry so sessions live until the client deletes them or the server stops.
    """

    api_url: str = DEFAULT_API_URL
    transport: str = DEFAULT_TRANSPORT
    port: int = DEFAULT_PORT
    http_host: str = DEFAULT_HTTP_HOST
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    session_idle_timeout: float | None = DEFAULT_SESSION_IDLE_TIMEOUT


def load_settings(env: Mapping[str, str], overrides: Mapping[str, Any] | None = None) -> Settings:
    """Build settings from ``env`` with ``overrides`` (explicit CLI flags) taking precedence.

    Only non-None override values are applied, so callers can pass a full mapping of parsed flags.
    Raises ``ValueError`` with a clear message for invalid values.
    """
    values: dict[str, Any] = {}

    def take(name: str, env_name: str, cast, default: Any, label: str) -> None:
        raw = env.get(env_name)
        if raw is not None and raw != "":
            try:
                values[name] = cast(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid {label} in environment variable {env_name}: {exc}") from None
        override = (overrides or {}).get(name)
        if override is not None:
            try:
                values[name] = cast(override)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid {label} from command-line flag: {exc}") from None
        if name not in values:
            values[name] = default

    take("api_url", ENV_API_URL, str, DEFAULT_API_URL, "backend URL")
    take("transport", ENV_TRANSPORT, _clean_transport, DEFAULT_TRANSPORT, "transport")
    take("http_host", ENV_HTTP_HOST, str, DEFAULT_HTTP_HOST, "HTTP bind host")
    take("port", ENV_PORT, int, DEFAULT_PORT, "port")
    take("timeout_seconds", ENV_TIMEOUT, float, DEFAULT_TIMEOUT_SECONDS, "timeout")
    take(
        "session_idle_timeout",
        ENV_SESSION_IDLE_TIMEOUT,
        _clean_idle_timeout,
        DEFAULT_SESSION_IDLE_TIMEOUT,
        "idle timeout",
    )

    settings = Settings(**values)
    if settings.transport not in VALID_TRANSPORTS:
        expected = " or ".join(repr(t) for t in VALID_TRANSPORTS)
        raise ValueError(f"invalid transport {settings.transport!r}: expected {expected}")
    if settings.port < 1 or settings.port > 65535:
        raise ValueError(f"invalid port {settings.port}: must be an integer between 1 and 65535")
    if settings.timeout_seconds <= 0:
        raise ValueError(f"invalid timeout {settings.timeout_seconds}: must be a positive number of seconds")
    if not settings.api_url.strip():
        raise ValueError("invalid backend URL: must not be empty")
    return settings


def _clean_transport(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("must be a string")
    return raw.strip().lower()


def _clean_idle_timeout(raw: Any) -> float | None:
    value = float(raw)
    if value < 0:
        raise ValueError("must be zero (disabled) or a positive number of seconds")
    return None if value == 0 else value
