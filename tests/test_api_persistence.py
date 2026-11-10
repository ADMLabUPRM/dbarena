import time

import pytest
from fastapi.testclient import TestClient
from gui.backend.app.main import create_app
from gui.backend.app.session_store import EpisodeSessionStore, SessionNotActiveError
from sqlalchemy import create_engine, text

from dbarena.actions.base import FakeAdapter, SqlResult
from dbarena.actions.registry import EngineRegistry, default_registry
from tests.fakes import FakeTaskStore, fake_episode_transaction_factory
from tests.test_api import VALID_TASK

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture()
def admin_engine(pg_url):
    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text((open("db/init/001-schema.sql", encoding="utf-8")).read()))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM episode_steps"))
        conn.execute(text("DELETE FROM episodes"))
        conn.execute(text("DELETE FROM tasks"))
    engine.dispose()


def _registry() -> EngineRegistry:
    registry = EngineRegistry()
    registry.register(
        "postgresql",
        FakeAdapter(result=SqlResult(columns=["id"], rows=[[1]], fetch_limited=False), schema=[]),
    )
    return registry


def _catalog() -> FakeTaskStore:
    from dbarena.catalog.validation import parse_task_payload

    return FakeTaskStore([parse_task_payload(dict(VALID_TASK, task_id="persist-1"))])


class TestEpisodePersistence:
    def test_steps_persist_as_they_happen(self, admin_engine):
        store = EpisodeSessionStore(_catalog(), registry=_registry(), admin_engine=admin_engine, episode_transaction_factory=fake_episode_transaction_factory)
        state = store.create("persist-1")
        store.step(state.session_id, "run_sql", {"sql": "SELECT id"})
        store.step(state.session_id, "submit_final_answer", {"tuples": [[1, "alpha"]]})

        with admin_engine.connect() as conn:
            episode = conn.execute(
                text("SELECT task_id, status, reward, ended_at FROM episodes")
            ).mappings().one()
            step_rows = conn.execute(
                text("SELECT step_number, action, observation FROM episode_steps ORDER BY step_number")
            ).all()
        assert episode["task_id"] == "persist-1"
        assert episode["status"] == "submitted"
        assert float(episode["reward"]) == 1.0
        assert episode["ended_at"] is not None
        assert [r[0] for r in step_rows] == [0, 1, 2]
        assert step_rows[2][2]["type"] == "episode_result"

    def test_history_readable_after_restart(self, admin_engine):
        store = EpisodeSessionStore(_catalog(), registry=_registry(), admin_engine=admin_engine, episode_transaction_factory=fake_episode_transaction_factory)
        state = store.create("persist-1")
        session_id = state.session_id
        store.step(session_id, "run_sql", {"sql": "SELECT id"})
        store.step(session_id, "submit_final_answer", {"tuples": [[1, "alpha"]]})

        restarted = EpisodeSessionStore(_catalog(), registry=_registry(), admin_engine=admin_engine)
        restored = restarted.get(session_id)
        assert restored.read_only is True
        assert [s.step_number for s in restored.steps] == [0, 1, 2]
        assert restored.steps[1].observation["type"] == "query_result"
        listed = restarted.list()
        assert any(s["session_id"] == session_id and s["status"] == "submitted" for s in listed)
        with pytest.raises(SessionNotActiveError):
            restarted.step(session_id, "describe_schema", {})

    def test_history_readable_via_api_after_backend_restart(self, pg_url, admin_engine):
        app = create_app(catalog=_catalog(), registry=_registry(), admin_url=pg_url, episode_transaction_factory=fake_episode_transaction_factory)
        client = TestClient(app)
        session_id = client.post("/api/sessions", json={"task_id": "persist-1"}).json()["session_id"]
        client.post(f"/api/sessions/{session_id}/step", json={"tool": "run_sql", "arguments": {"sql": "SELECT id"}})
        client.post(
            f"/api/sessions/{session_id}/step",
            json={"tool": "submit_final_answer", "arguments": {"tuples": [[1, "alpha"]]}},
        )

        restarted_app = create_app(catalog=_catalog(), registry=_registry(), admin_url=pg_url, episode_transaction_factory=fake_episode_transaction_factory)
        restarted_client = TestClient(restarted_app)
        response = restarted_client.get(f"/api/sessions/{session_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["read_only"] is True
        assert len(body["steps"]) == 3
        assert body["steps"][2]["observation"]["payload"]["matched"] is True
        listing = restarted_client.get("/api/sessions")
        assert any(s["session_id"] == session_id for s in listing.json())
        conflict = restarted_client.post(
            f"/api/sessions/{session_id}/step", json={"tool": "describe_schema", "arguments": {}}
        )
        assert conflict.status_code == 409


class TestAbandonedStartupMarking:
    def test_stale_running_episodes_marked_abandoned_at_startup(self, pg_url, admin_engine):
        with admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO episodes (task_id, task_snapshot, status) "
                    "VALUES ('persist-1', '{}'::jsonb, 'running')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO episodes (task_id, task_snapshot, status, reward, ended_at) "
                    "VALUES ('persist-1', '{}'::jsonb, 'submitted', 1.0, now())"
                )
            )

        create_app(catalog=_catalog(), registry=_registry(), admin_url=pg_url, episode_transaction_factory=fake_episode_transaction_factory)

        with admin_engine.begin() as conn:
            abandoned = conn.execute(
                text("SELECT reward, ended_at FROM episodes WHERE status = 'abandoned'")
            ).all()
            untouched = conn.execute(
                text("SELECT reward, ended_at FROM episodes WHERE status = 'submitted'")
            ).all()
            total = conn.execute(text("SELECT count(*) FROM episodes")).scalar()
        assert total == 2
        assert len(abandoned) == 1
        assert float(abandoned[0][0]) == 0.0
        assert abandoned[0][1] is not None
        assert len(untouched) == 1
        assert float(untouched[0][0]) == 1.0
        assert untouched[0][1] is not None

    def test_sessions_listing_surfaces_abandoned_status(self, pg_url, admin_engine):
        with admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO episodes (task_id, task_snapshot, status) "
                    "VALUES ('persist-1', '{}'::jsonb, 'abandoned')"
                )
            )

        app = create_app(catalog=_catalog(), registry=_registry(), admin_url=pg_url, episode_transaction_factory=fake_episode_transaction_factory)
        client = TestClient(app)
        listing = client.get("/api/sessions").json()
        assert any(s["status"] == "abandoned" for s in listing)
        episode_id = next(s["session_id"] for s in listing if s["status"] == "abandoned")
        response = client.get(f"/api/sessions/{episode_id}")
        assert response.status_code == 200
        assert response.json()["read_only"] is True


class TestSequentialSessionsReleaseConnections:
    def test_five_sequential_sessions_do_not_accumulate_connections(self, pg_url, admin_engine):
        from dbarena.catalog.validation import parse_task_payload

        pg_task = parse_task_payload(
            dict(
                VALID_TASK,
                task_id="persist-1",
                sqlalchemy_url=pg_url,
                terminal_condition={"expected_tuples": [[3]], "matching": {"numeric_tolerance": 0.01}},
            )
        )
        store = EpisodeSessionStore(
            FakeTaskStore([pg_task]), registry=default_registry(), admin_engine=admin_engine
        )

        def connection_count():
            counter = create_engine(pg_url)
            try:
                with counter.connect() as conn:
                    return conn.execute(
                        text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
                    ).scalar()
            finally:
                counter.dispose()

        baseline = connection_count()
        for _ in range(5):
            state = store.create("persist-1")
            store.step(state.session_id, "run_sql", {"sql": "SELECT count(*) FROM arena_item"})
            store.step(state.session_id, "submit_final_answer", {"tuples": [[3]]})
            assert store.get(state.session_id).steps[-1].observation["type"] == "episode_result"
        for _ in range(10):
            if connection_count() <= baseline:
                break
            time.sleep(0.2)
        assert connection_count() == baseline
