"""Command-line entry point: ``python -m mcp_server``."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from mcp_server.config import load_settings
from mcp_server.server import run_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mcp_server",
        description="DBArena MCP server: exposes the DBArena admin backend as MCP tools.",
    )
    parser.add_argument(
        "--transport",
        help="MCP transport: stdio or streamable-http (default: MCP_TRANSPORT or stdio)",
    )
    parser.add_argument(
        "--api-url",
        dest="api_url",
        help="DBArena backend base URL (default: DBARENA_API_URL or http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--host",
        help="bind host for the streamable-http transport (default: MCP_HTTP_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        help="port for the streamable-http transport (default: MCP_PORT or 8090)",
    )
    parser.add_argument(
        "--timeout",
        help="backend request timeout in seconds (default: DBARENA_TIMEOUT_SECONDS or 120)",
    )
    parser.add_argument(
        "--idle-timeout",
        dest="session_idle_timeout",
        help="streamable-http session idle timeout in seconds; 0 disables expiry "
        "(default: MCP_SESSION_IDLE_TIMEOUT or disabled)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    overrides: dict[str, Any] = {
        "transport": args.transport,
        "api_url": args.api_url,
        "http_host": args.host,
        "port": args.port,
        "timeout_seconds": args.timeout,
        "session_idle_timeout": args.session_idle_timeout,
    }
    try:
        settings = load_settings(os.environ, {k: v for k, v in overrides.items() if v is not None})
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    run_server(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
