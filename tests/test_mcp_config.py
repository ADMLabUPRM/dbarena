import pytest
from mcp_server.config import Settings, load_settings


class TestDefaults:
    def test_empty_env_yields_defaults(self):
        settings = load_settings({})
        assert settings == Settings(
            api_url="http://127.0.0.1:8000",
            transport="stdio",
            port=8090,
            http_host="127.0.0.1",
            timeout_seconds=120.0,
            session_idle_timeout=None,
        )


class TestEnvOverrides:
    def test_every_knob_is_overridable(self):
        settings = load_settings(
            {
                "DBARENA_API_URL": "http://backend:8000",
                "MCP_TRANSPORT": "streamable-http",
                "MCP_PORT": "9999",
                "MCP_HTTP_HOST": "0.0.0.0",
                "DBARENA_TIMEOUT_SECONDS": "45.5",
                "MCP_SESSION_IDLE_TIMEOUT": "1800",
            }
        )
        assert settings.api_url == "http://backend:8000"
        assert settings.transport == "streamable-http"
        assert settings.port == 9999
        assert settings.http_host == "0.0.0.0"
        assert settings.timeout_seconds == 45.5
        assert settings.session_idle_timeout == 1800.0

    def test_transport_is_case_insensitive_and_trimmed(self):
        assert load_settings({"MCP_TRANSPORT": "  Streamable-HTTP "}).transport == "streamable-http"

    def test_empty_env_values_are_ignored(self):
        assert load_settings({"MCP_PORT": ""}) == load_settings({})

    def test_zero_idle_timeout_disables_expiry(self):
        assert load_settings({"MCP_SESSION_IDLE_TIMEOUT": "0"}).session_idle_timeout is None

    def test_negative_idle_timeout_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            load_settings({"MCP_SESSION_IDLE_TIMEOUT": "-5"})
        assert "MCP_SESSION_IDLE_TIMEOUT" in str(excinfo.value)

    def test_non_numeric_idle_timeout_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            load_settings({"MCP_SESSION_IDLE_TIMEOUT": "soon"})
        assert "MCP_SESSION_IDLE_TIMEOUT" in str(excinfo.value)


class TestFlagPrecedence:
    def test_flags_override_env(self):
        settings = load_settings(
            {"MCP_PORT": "1111", "MCP_TRANSPORT": "stdio", "DBARENA_API_URL": "http://env:1"},
            {"port": 2222, "transport": "streamable-http"},
        )
        assert settings.port == 2222
        assert settings.transport == "streamable-http"
        assert settings.api_url == "http://env:1"

    def test_idle_timeout_flag_overrides_env(self):
        settings = load_settings({"MCP_SESSION_IDLE_TIMEOUT": "60"}, {"session_idle_timeout": 0})
        assert settings.session_idle_timeout is None

    def test_none_overrides_do_not_mask_env(self):
        settings = load_settings({"MCP_PORT": "1111"}, {"port": None})
        assert settings.port == 1111


class TestValidation:
    def test_invalid_transport_names_the_valid_options(self):
        with pytest.raises(ValueError) as excinfo:
            load_settings({"MCP_TRANSPORT": "websocket"})
        message = str(excinfo.value)
        assert "websocket" in message
        assert "stdio" in message and "streamable-http" in message

    def test_invalid_port_names_the_variable(self):
        with pytest.raises(ValueError) as excinfo:
            load_settings({"MCP_PORT": "not-a-number"})
        assert "MCP_PORT" in str(excinfo.value)

    def test_port_out_of_range(self):
        with pytest.raises(ValueError):
            load_settings({"MCP_PORT": "0"})
        with pytest.raises(ValueError):
            load_settings({"MCP_PORT": "70000"})

    def test_non_positive_timeout_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            load_settings({"DBARENA_TIMEOUT_SECONDS": "0"})
        assert "timeout" in str(excinfo.value)

    def test_empty_api_url_rejected(self):
        with pytest.raises(ValueError):
            load_settings({"DBARENA_API_URL": "  "})
