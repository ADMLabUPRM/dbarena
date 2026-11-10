
from dbarena.actions.base import FakeAdapter
from dbarena.actions.errors import QueryTimeoutError
from dbarena.actions.registry import EngineRegistry
from dbarena.env import DBArenaEnv
from tests.test_environment import FakeCatalog, _fake_episode_factory, _task


def _adapter_raising_timeout() -> FakeAdapter:
    class TimeoutAdapter(FakeAdapter):
        def run_sql(self, engine, sql, row_limit, timeout_seconds, connection=None):
            self.calls.append(
                ("run_sql", {"sql": sql, "row_limit": row_limit, "timeout": timeout_seconds, "connection": connection})
            )
            raise QueryTimeoutError("canceling statement due to statement timeout")

    return TimeoutAdapter()


class TestTimeoutWiring:
    def test_task_query_timeout_reaches_adapter(self):
        adapter = FakeAdapter()
        task = _task()
        from dataclasses import replace as dc_replace

        task = dc_replace(task, query_timeout=42)
        registry = EngineRegistry()
        registry.register("postgresql", adapter)
        env = DBArenaEnv(FakeCatalog([task]), registry=registry, episode_transaction_factory=_fake_episode_factory)
        env.reset(options={"task_id": "t-1"})
        env.step({"tool": "run_sql", "arguments": {"sql": "SELECT 1"}})
        assert adapter.calls[-1] == (
            "run_sql",
            {"sql": "SELECT 1", "row_limit": 50, "timeout": 42, "connection": env._episode.connection},
        )

    def test_timeout_returns_error_observation_and_episode_continues(self):
        adapter = _adapter_raising_timeout()
        env = _env = DBArenaEnv(
            FakeCatalog([_task()]), registry=_registry(adapter), episode_transaction_factory=_fake_episode_factory
        )
        env.reset(options={"task_id": "t-1"})
        obs, reward, terminated, truncated, info = env.step(
            {"tool": "run_sql", "arguments": {"sql": "SELECT pg_sleep(100)"}}
        )
        assert obs["type"] == "error"
        assert obs["payload"]["code"] == "timeout"
        assert "statement timeout" in obs["payload"]["message"]
        assert obs["action"]["tool"] == "run_sql"
        assert reward == 0.0
        assert terminated is False and truncated is False
        assert info["step_number"] == 1
        assert env.episode_over is False
        obs2, *_ = env.step({"tool": "describe_schema", "arguments": {}})
        assert info["step_number"] == 1
        assert obs2["type"] == "schema_description"


def _registry(adapter):
    registry = EngineRegistry()
    registry.register("postgresql", adapter)
    return registry
