import os
import time

import pytest
from sqlalchemy import create_engine, text

from dbarena.actions.registry import default_registry
from dbarena.catalog.records import TaskRecord
from dbarena.env import DBArenaEnv
from dbarena.models.terminal import MatchingRules, StateCheck, TerminalCondition, VerificationRule

pytestmark = pytest.mark.usefixtures("pg_engine")

PROBE_TABLE = "episode_probe"


@pytest.fixture()
def probe(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {PROBE_TABLE}"))
        conn.execute(text(f"CREATE TABLE {PROBE_TABLE} (id integer PRIMARY KEY)"))
    yield
    with pg_engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {PROBE_TABLE}"))


@pytest.fixture()
def fresh_engine(pg_url):
    engine = create_engine(pg_url)
    yield engine
    engine.dispose()


@pytest.fixture()
def envs():
    created = []
    yield created
    for env in created:
        env.close()


def _task(pg_url, task_id="t-iso", task_type="CC", category="OLTP", max_steps=5, with_checks=True, with_rules=False):
    checks = ()
    rules = ()
    if with_checks:
        checks = (
            StateCheck(
                sql=f"SELECT count(*) FROM {PROBE_TABLE}",
                expected_tuples=[["1"]],
                matching=MatchingRules(numeric_tolerance=None),
            ),
        )
    if with_rules:
        rules = (VerificationRule(sql=f"SELECT count(*) = 1 FROM {PROBE_TABLE}"),)
    return TaskRecord(
        task_id=task_id,
        goal="Make the state change the task describes.",
        submission="Call submit_final_answer with the confirmation tuple.",
        category=category,
        task_type=task_type,
        max_steps=max_steps,
        sqlalchemy_url=pg_url,
        terminal_condition=TerminalCondition(
            expected_tuples=[["ok"]] if (with_checks or with_rules) else [["done"]],
            matching=MatchingRules(),
            state_checks=checks,
            rules=rules,
        ),
        query_timeout=10,
        row_limit=None,
    )


def _env(task, envs):
    env = DBArenaEnv(_catalog(task), registry=default_registry())
    env.reset(options={"task_id": task.task_id})
    envs.append(env)
    return env


def _catalog(task):
    class Catalog:
        def get(self, task_id):
            return task if task.task_id == task_id else None

    return Catalog()


def _step(env, sql):
    return env.step({"tool": "run_sql", "arguments": {"sql": sql}})


def _submit(env, tuples):
    return env.step({"tool": "submit_final_answer", "arguments": {"tuples": tuples}})


def _count(fresh_engine, table=PROBE_TABLE):
    with fresh_engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()


def _connection_count(pg_url):
    counter = create_engine(pg_url)
    try:
        with counter.connect() as conn:
            return conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
            ).scalar()
    finally:
        counter.dispose()


class TestEpisodeIsolation:
    def test_writes_visible_mid_episode_and_evaluated_at_submit(self, pg_url, probe, fresh_engine, envs):
        env = _env(_task(pg_url), envs)
        obs, *_ = _step(env, f"INSERT INTO {PROBE_TABLE} VALUES (1)")
        assert obs["type"] == "query_result" and obs["payload"]["rows"] == []
        obs, *_ = _step(env, f"SELECT count(*) FROM {PROBE_TABLE}")
        assert obs["payload"]["rows"] == [[1]]
        obs, reward, terminated, *_ = _submit(env, [["ok"]])
        assert obs["payload"]["matched"] is True
        assert reward == 1.0 and terminated is True
        assert _count(fresh_engine) == 0

    def test_rollback_at_failed_submit(self, pg_url, probe, fresh_engine, envs):
        env = _env(_task(pg_url), envs)
        _step(env, f"INSERT INTO {PROBE_TABLE} VALUES (1)")
        obs, reward, terminated, *_ = _submit(env, [["wrong"]])
        assert obs["payload"]["matched"] is False and reward == 0.0 and terminated is True
        assert _count(fresh_engine) == 0

    def test_rollback_at_truncation(self, pg_url, probe, fresh_engine, envs):
        env = _env(_task(pg_url, max_steps=2), envs)
        _step(env, f"INSERT INTO {PROBE_TABLE} VALUES (1)")
        obs, _, terminated, truncated, _ = _step(env, f"SELECT count(*) FROM {PROBE_TABLE}")
        assert terminated is False and truncated is True
        assert _count(fresh_engine) == 0

    def test_fresh_episode_starts_pristine(self, pg_url, probe, fresh_engine, envs):
        task = _task(pg_url)
        env = _env(task, envs)
        _step(env, f"INSERT INTO {PROBE_TABLE} VALUES (1)")
        _submit(env, [["ok"]])
        assert _count(fresh_engine) == 0
        env2 = DBArenaEnv(_catalog(task), registry=default_registry())
        env2.reset(options={"task_id": task.task_id})
        envs.append(env2)
        obs, *_ = _step(env2, f"SELECT count(*) FROM {PROBE_TABLE}")
        assert obs["payload"]["rows"] == [[0]]

    def test_failed_statement_does_not_poison_the_episode(self, pg_url, probe, fresh_engine, envs):
        env = _env(_task(pg_url), envs)
        obs, *_ = _step(env, "SELECT boom")
        assert obs["type"] == "error" and obs["payload"]["code"] == "query_error"
        obs, *_ = _step(env, f"INSERT INTO {PROBE_TABLE} VALUES (1)")
        assert obs["type"] == "query_result"
        obs, reward, terminated, *_ = _submit(env, [["ok"]])
        assert obs["payload"]["matched"] is True and reward == 1.0 and terminated is True
        assert _count(fresh_engine) == 0

    def test_uncommitted_writes_invisible_to_other_connections(self, pg_url, probe, fresh_engine, envs):
        env = _env(_task(pg_url), envs)
        _step(env, f"INSERT INTO {PROBE_TABLE} VALUES (1)")
        assert _count(fresh_engine) == 0
        _submit(env, [["ok"]])


class TestConnectionRelease:
    def test_episode_end_releases_connections_and_describe_works_under_transaction(self, pg_url, probe, envs):
        task = _task(pg_url)
        baseline = _connection_count(pg_url)
        env = DBArenaEnv(_catalog(task), registry=default_registry())
        env.reset(options={"task_id": task.task_id})
        envs.append(env)
        # The reset describe connection parks in the pool and is reused by the
        # episode transaction: only one connection is held before the first step.
        assert _connection_count(pg_url) == baseline + 1
        obs, *_ = env.step({"tool": "describe_schema", "arguments": {}})
        assert obs["type"] == "schema_description"
        # Transaction connection held + the describe connection parked: capped at 2.
        assert _connection_count(pg_url) == baseline + 2
        _step(env, f"INSERT INTO {PROBE_TABLE} VALUES (1)")
        obs, reward, terminated, *_ = _submit(env, [["ok"]])
        assert obs["payload"]["matched"] is True and reward == 1.0 and terminated is True
        assert env._engine is None
        for _ in range(10):
            if _connection_count(pg_url) <= baseline:
                break
            time.sleep(0.2)
        assert _connection_count(pg_url) == baseline


class TestNoIsolationWithoutStateChecks:
    def test_writes_roll_back_after_episode_ends(self, pg_url, probe, fresh_engine, envs):
        task = _task(pg_url, task_id="t-iso-is", task_type="IS", category="OLAP", with_checks=False)
        env = _env(task, envs)
        _step(env, f"INSERT INTO {PROBE_TABLE} VALUES (1)")
        obs, reward, terminated, *_ = _submit(env, [["done"]])
        assert obs["payload"]["matched"] is True and reward == 1.0 and terminated is True
        assert _count(fresh_engine) == 0

    def test_stray_write_invisible_to_other_connections(self, pg_url, probe, fresh_engine, envs):
        task = _task(pg_url, task_id="t-iso-nav", task_type="NAV", category="OLTP", with_checks=False)
        env = _env(task, envs)
        _step(env, f"INSERT INTO {PROBE_TABLE} VALUES (1)")
        assert _count(fresh_engine) == 0
        obs, *_ = _step(env, f"SELECT count(*) FROM {PROBE_TABLE}")
        assert obs["payload"]["rows"] == [[1]]
        _submit(env, [["done"]])
        assert _count(fresh_engine) == 0


class TestRulesOnlyIsolation:
    def test_rules_only_task_is_write_isolated(self, pg_url, probe, fresh_engine, envs):
        task = _task(pg_url, task_id="t-iso-rules", with_checks=False, with_rules=True)
        env = _env(task, envs)
        _step(env, f"INSERT INTO {PROBE_TABLE} VALUES (1)")
        obs, reward, terminated, *_ = _submit(env, [["ok"]])
        assert obs["payload"]["matched"] is True
        assert "1 rule(s) passed" in obs["payload"]["message"]
        assert reward == 1.0 and terminated is True
        assert _count(fresh_engine) == 0


class TestDuckDBUniversalRollback:
    def test_no_check_duckdb_writes_roll_back(self, tmp_path, envs):
        path = os.path.join(str(tmp_path), "fixture.duckdb")
        url = f"duckdb:///{path}"
        setup = create_engine(url)
        try:
            with setup.begin() as conn:
                conn.execute(text("CREATE TABLE duck_probe (id integer PRIMARY KEY)"))
        finally:
            setup.dispose()
        fresh = create_engine(url)
        try:
            task = TaskRecord(
                task_id="t-iso-duck",
                goal="Read-only lookup.",
                submission="Call submit_final_answer with the confirmation tuple.",
                category="ANALYTICS",
                task_type="IS",
                max_steps=5,
                sqlalchemy_url=url,
                terminal_condition=TerminalCondition(
                    expected_tuples=[["done"]],
                    matching=MatchingRules(),
                ),
                query_timeout=10,
                row_limit=None,
            )
            env = _env(task, envs)
            obs, *_ = _step(env, "INSERT INTO duck_probe VALUES (1)")
            assert obs["type"] == "query_result"
            obs, *_ = _step(env, "SELECT count(*) FROM duck_probe")
            assert obs["payload"]["rows"] == [[1]]
            obs, reward, terminated, *_ = _submit(env, [["done"]])
            assert obs["payload"]["matched"] is True and reward == 1.0 and terminated is True
            with fresh.connect() as conn:
                assert conn.execute(text("SELECT count(*) FROM duck_probe")).scalar() == 0
        finally:
            fresh.dispose()
