"""Server construction and transport selection."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcp_server.client import BackendClient
from mcp_server.config import Settings
from mcp_server.tools import SERVER_INSTRUCTIONS, register_tools

SERVER_NAME = "dbarena"


def build_mcp(settings: Settings, client: BackendClient) -> FastMCP:
    """Build the FastMCP server with all tools registered against ``client``."""
    mcp = FastMCP(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        host=settings.http_host,
        port=settings.port,
        session_idle_timeout=settings.session_idle_timeout,
    )
    register_tools(mcp, client)
    return mcp


def run_server(settings: Settings) -> None:
    """Run the MCP server on the configured transport; blocks until the transport exits."""
    client = BackendClient(settings.api_url, timeout_seconds=settings.timeout_seconds)
    try:
        mcp = build_mcp(settings, client)
        mcp.run(transport=settings.transport)  # type: ignore[arg-type]
    finally:
        client.close()
