import os
import socket
import subprocess
import sys
import time

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp_server.__main__ import main
from mcp_server.client import BackendClient
from mcp_server.config import Settings
from mcp_server.server import build_mcp

EXPECTED_TOOL_COUNT = 9


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"MCP server did not start listening on port {port}")


class TestStdioTransport:
    def test_initialization_handshake_and_tool_list(self):
        async def scenario():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "mcp_server", "--transport", "stdio"],
                cwd=os.getcwd(),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    listing = await session.list_tools()
                    return init, listing

        init, listing = anyio.run(scenario)
        assert init.serverInfo.name == "dbarena"
        assert len(listing.tools) == EXPECTED_TOOL_COUNT


class TestStreamableHttpTransport:
    def test_serves_mcp_on_configured_port(self):
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mcp_server",
                "--transport",
                "streamable-http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=os.getcwd(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_port(port)

            async def scenario():
                async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _get_session_id):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await session.list_tools()

            listing = anyio.run(scenario)
            assert len(listing.tools) == EXPECTED_TOOL_COUNT
        finally:
            proc.terminate()
            proc.wait(timeout=15)


class TestEntryPointResolution:
    def _capture_run(self, monkeypatch) -> list:
        captured: list = []
        monkeypatch.setattr("mcp_server.__main__.run_server", lambda settings: captured.append(settings))
        return captured

    def test_idle_timeout_flag_overrides_env(self, monkeypatch):
        captured = self._capture_run(monkeypatch)
        monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "60")
        code = main(["--transport", "streamable-http", "--idle-timeout", "0"])
        assert code == 0
        assert captured[0].session_idle_timeout is None

    def test_idle_timeout_env_applies_without_flag(self, monkeypatch):
        captured = self._capture_run(monkeypatch)
        monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "90")
        assert main([]) == 0
        assert captured[0].session_idle_timeout == 90.0

    def test_flags_override_env_and_defaults(self, monkeypatch):
        captured = self._capture_run(monkeypatch)
        monkeypatch.setenv("MCP_TRANSPORT", "stdio")
        monkeypatch.setenv("MCP_PORT", "1111")
        code = main(
            [
                "--transport",
                "streamable-http",
                "--port",
                "2222",
                "--host",
                "0.0.0.0",
                "--api-url",
                "http://flag:9000",
                "--timeout",
                "30",
            ]
        )
        assert code == 0
        settings = captured[0]
        assert settings.transport == "streamable-http"
        assert settings.port == 2222
        assert settings.http_host == "0.0.0.0"
        assert settings.api_url == "http://flag:9000"
        assert settings.timeout_seconds == 30.0

    def test_env_overrides_apply_without_flags(self, monkeypatch):
        captured = self._capture_run(monkeypatch)
        monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
        monkeypatch.setenv("MCP_PORT", "3333")
        monkeypatch.setenv("DBARENA_API_URL", "http://env:8000")
        assert main([]) == 0
        settings = captured[0]
        assert settings.transport == "streamable-http"
        assert settings.port == 3333
        assert settings.api_url == "http://env:8000"
        assert settings.http_host == "127.0.0.1"

    def test_defaults_when_neither_flags_nor_env(self, monkeypatch):
        captured = self._capture_run(monkeypatch)
        for name in (
            "MCP_TRANSPORT",
            "MCP_PORT",
            "MCP_HTTP_HOST",
            "DBARENA_API_URL",
            "DBARENA_TIMEOUT_SECONDS",
            "MCP_SESSION_IDLE_TIMEOUT",
        ):
            monkeypatch.delenv(name, raising=False)
        assert main([]) == 0
        settings = captured[0]
        assert settings.transport == "stdio"
        assert settings.port == 8090
        assert settings.http_host == "127.0.0.1"
        assert settings.api_url == "http://127.0.0.1:8000"
        assert settings.timeout_seconds == 120.0
        assert settings.session_idle_timeout is None

    def test_invalid_transport_rejected_with_clear_message(self, monkeypatch, capsys):
        self._capture_run(monkeypatch)
        code = main(["--transport", "websocket"])
        assert code == 2
        stderr = capsys.readouterr().err
        assert "invalid transport" in stderr
        assert "websocket" in stderr
        assert "stdio" in stderr and "streamable-http" in stderr
        assert not capsys.readouterr().out

    def test_invalid_port_rejected(self, monkeypatch, capsys):
        self._capture_run(monkeypatch)
        code = main(["--port", "not-a-number"])
        assert code == 2
        stderr = capsys.readouterr().err
        assert "invalid port" in stderr
        assert "not-a-number" in stderr


class TestBuildMcpWiring:
    def test_session_idle_timeout_is_forwarded_to_fastmcp(self):
        client = BackendClient("http://127.0.0.1:1")
        try:
            settings = Settings(transport="streamable-http", session_idle_timeout=120.0)
            mcp = build_mcp(settings, client)
            assert mcp.settings.session_idle_timeout == 120.0
        finally:
            client.close()

    def test_disabled_session_idle_timeout_is_forwarded(self):
        client = BackendClient("http://127.0.0.1:1")
        try:
            settings = Settings(transport="streamable-http", session_idle_timeout=None)
            mcp = build_mcp(settings, client)
            assert mcp.settings.session_idle_timeout is None
        finally:
            client.close()
