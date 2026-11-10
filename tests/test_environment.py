from datetime import date
from decimal import Decimal

import pytest

from dbarena.actions.base import FakeAdapter, SqlResult
from dbarena.actions.errors import QueryError, QueryTimeoutError
from dbarena.actions.registry import EngineRegistry
from dbarena.catalog.records import TaskRecord
from dbarena.env import DBArenaEnv, EpisodeClosedError, ResetError
from dbarena.models.terminal import MatchingRules, StateCheck, TerminalCondition, VerificationRule


class FakeCatalog:
    def __init__(self, tasks: list[TaskRecord]):
        self._tasks = {t.task_id: t for t in tasks}

    def get(self, task_id):
        return self._tasks.get(task_id)


class ScriptedAdapter(FakeAdapter):
    def __init__(self, script: dict[str, SqlResult | Exception] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._script = script or {}

    def run_sql(self, engine, sql, row_limit, timeout_seconds, connection=None):
        self.calls.append(
            ("run_sql", {"sql": sql, "row_limit": row_limit, "timeout": timeout_seconds, "connection": connection})
        )
        outcome = self._script.get(sql)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            return outcome
        return self.result


class FakeEpisodeTransaction:
    def __init__(self):
        self.connection = object()
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True


def _fake_episode_factory(engine, timeout):
    return FakeEpisodeTransaction()


def _task(
    task_id="t-1",
    max_steps=5,
    expected=None,
    rules=None,
    url="postgresql+psycopg://u:p@h:5432/db",
    state_checks=(),
    verification_rules=(),
    task_type="IS",
    category="OLAP",
):
    return TaskRecord(
        task_id=task_id,
        goal="Answer the question.",
        submission='Call submit_final_answer with tuples [[1, "alpha"]].',
        category=category,
        task_type=task_type,
        max_steps=max_steps,
        sqlalchemy_url=url,
        terminal_condition=TerminalCondition(
            expected_tuples=[[1, "alpha"]] if expected is None else expected,
            matching=rules or MatchingRules(),
            state_checks=tuple(state_checks),
            rules=tuple(verification_rules),
        ),
        query_timeout=15,
        row_limit=None,
    )


def _adapter() -> FakeAdapter:
    return FakeAdapter(
        result=SqlResult(columns=["id", "name"], rows=[[1, "alpha"]], fetch_limited=False),
        schema=[],
    )


def _env(task=None, adapter=None, expected=None, max_steps=5) -> DBArenaEnv:
    task = task or _task(max_steps=max_steps, expected=expected)
    registry = EngineRegistry()
    registry.register("postgresql", adapter or _adapter())
    env = DBArenaEnv(FakeCatalog([task]), registry=registry, episode_transaction_factory=_fake_episode_factory)
    env.reset(options={"task_id": task.task_id})
    return env


class TestReset:
    def test_initial_observation_has_actions_and_schema(self):
        registry = EngineRegistry()
        adapter = _adapter()
        registry.register("postgresql", adapter)
        env = DBArenaEnv(FakeCatalog([_task()]), registry=registry, episode_transaction_factory=_fake_episode_factory)
        obs, info = env.reset(options={"task_id": "t-1"})
        assert obs["schema_version"] == "dbarena-observation/v1"
        assert obs["type"] == "schema_description"
        assert obs["action"] is None
        actions = obs["meta"]["available_actions"]
        assert [a["tool"] for a in actions] == [
            "run_sql",
            "describe_table",
            "describe_tables",
            "describe_schema",
            "submit_final_answer",
            "run_rag_query",
        ]
        assert all(a["summary"] and a["example"]["tool"] == a["tool"] for a in actions)
        submit_spec = next(a for a in actions if a["tool"] == "submit_final_answer")
        assert "confirmation" in submit_spec["summary"]
        assert "state checks" in submit_spec["summary"]
        assert info["available_actions"] == actions
        assert env.step_number == 0
        assert env.episode_over is False

    def test_initial_observation_exposes_the_observation_space(self):
        env = _env()
        obs, _ = env.reset(options={"task_id": "t-1"})
        space = obs["meta"]["observation_space"]
        assert [e["type"] for e in space] == [
            "query_result",
            "table_description",
            "schema_description",
            "episode_result",
            "error",
        ]
        assert all(e["description"] and e["example"]["schema_version"] == "dbarena-observation/v1" for e in space)
        assert obs["meta"]["last_action"] is None
        assert set(obs["meta"].keys()) == {"available_actions", "observation_space", "last_action"}
        assert obs["reward"] is None
        assert obs["terminated"] is False
        assert obs["truncated"] is False
        assert obs["step_number"] == 0
        assert obs["task"] == {
            "task_id": "t-1",
            "goal": "Answer the question.",
            "submission": 'Call submit_final_answer with tuples [[1, "alpha"]].',
            "category": "OLAP",
            "task_type": "IS",
        }

    def test_goal_and_submission_visible_on_every_step(self):
        env = _env(max_steps=3)
        obs0, _ = env.reset(options={"task_id": "t-1"})
        obs1, *_ = env.step({"tool": "run_sql", "arguments": {"sql": "SELECT 1"}})
        obs2, *_ = env.step({"tool": "describe_schema", "arguments": {}})
        expected = {
            "task_id": "t-1",
            "goal": "Answer the question.",
            "submission": 'Call submit_final_answer with tuples [[1, "alpha"]].',
            "category": "OLAP",
            "task_type": "IS",
        }
        assert obs0["task"] == expected
        assert obs1["task"] == expected
        assert obs2["task"] == expected

    def test_reset_is_repeatable(self):
        env = _env()
        first, _ = env.reset(options={"task_id": "t-1"})
        env.step({"tool": "run_sql", "arguments": {"sql": "SELECT 1"}})
        second, _ = env.reset(options={"task_id": "t-1"})
        assert first == second
        assert env.step_number == 0
        assert env.episode_over is False

    def test_reset_requires_task_id(self):
        env = _env()
        with pytest.raises(ResetError, match="task_id"):
            env.reset()

    def test_reset_unknown_task(self):
        env = _env()
        with pytest.raises(ResetError, match="unknown task"):
            env.reset(options={"task_id": "ghost"})

    def test_reset_unreachable_database_raises_with_reason(self):
        class BrokenAdapter(FakeAdapter):
            def describe_schema(self, engine):
                raise ConnectionError("connection refused")

        registry = EngineRegistry()
        registry.register("postgresql", BrokenAdapter())
        env = DBArenaEnv(FakeCatalog([_task()]), registry=registry, episode_transaction_factory=_fake_episode_factory)
        with pytest.raises(ResetError, match="connection refused"):
            env.reset(options={"task_id": "t-1"})

    def test_reset_creates_engine_with_bounded_pool(self, monkeypatch):
        captured = {}

        def fake_create_engine(url, **kwargs):
            captured["url"] = url
            captured["pool_size"] = kwargs.get("pool_size")
            captured["max_overflow"] = kwargs.get("max_overflow")
            return object()

        monkeypatch.setattr("dbarena.env.environment.create_engine", fake_create_engine)
        registry = EngineRegistry()
        registry.register("postgresql", _adapter())
        env = DBArenaEnv(FakeCatalog([_task()]), registry=registry, episode_transaction_factory=_fake_episode_factory)
        env.reset(options={"task_id": "t-1"})
        assert captured["url"] == "postgresql+psycopg://u:p@h:5432/db"
        assert captured["pool_size"] == 1
        assert captured["max_overflow"] == 1


class TestStep:
    def test_run_sql_returns_query_result(self):
        env = _env()
        obs, reward, terminated, truncated, info = env.step(
            {"tool": "run_sql", "arguments": {"sql": "SELECT id, name FROM t"}}
        )
        assert obs["type"] == "query_result"
        assert obs["payload"]["rows"] == [[1, "alpha"]]
        assert obs["payload"]["complete"] is True
        assert obs["meta"]["last_action"] == {"tool": "run_sql", "arguments": {"sql": "SELECT id, name FROM t"}}
        assert "available_actions" in obs["meta"] and "observation_space" in obs["meta"]
        assert obs["reward"] == 0.0
        assert obs["terminated"] is False
        assert obs["truncated"] is False
        assert obs["step_number"] == 1
        assert reward == 0.0
        assert terminated is False and truncated is False
        assert info["step_number"] == 1

    def test_describe_schema(self):
        env = _env()
        obs, *_ = env.step({"tool": "describe_schema", "arguments": {}})
        assert obs["type"] == "schema_description"

    def test_describe_table_found_and_missing(self):
        env = _env()
        obs, *_ = env.step({"tool": "describe_table", "arguments": {"table": "nope"}})
        assert obs["type"] == "error"
        assert obs["payload"]["code"] == "not_found"

    def test_describe_tables_mixed(self):
        adapter = FakeAdapter(tables=[])
        env = _env(adapter=adapter)
        obs, *_ = env.step({"tool": "describe_tables", "arguments": {"tables": ["a", "b"]}})
        assert obs["type"] == "table_description"
        assert [t["found"] for t in obs["payload"]["tables"]] == [False, False]

    def test_unknown_tool_is_error_step(self):
        env = _env()
        obs, reward, terminated, truncated, info = env.step({"tool": "drop_database", "arguments": {}})
        assert obs["type"] == "error"
        assert obs["payload"]["code"] == "invalid_action"
        assert obs["action"] == {"tool": "drop_database", "arguments": {}}
        assert obs["meta"]["last_action"] == {"tool": "drop_database", "arguments": {}}
        assert reward == 0.0
        assert terminated is False
        assert info["step_number"] == 1

    def test_malformed_action_is_error_step(self):
        env = _env()
        obs, *_ = env.step({"arguments": {}})
        assert obs["payload"]["code"] == "invalid_action"
        obs2, *_ = env.step("run_sql")
        assert obs2["payload"]["code"] == "invalid_action"

    def test_missing_arguments_are_error_steps(self):
        env = _env()
        obs, *_ = env.step({"tool": "run_sql", "arguments": {}})
        assert obs["payload"]["code"] == "invalid_arguments"
        obs2, *_ = env.step({"tool": "describe_table", "arguments": {}})
        assert obs2["payload"]["code"] == "invalid_arguments"


class TestSubmit:
    def test_correct_submit_terminates_with_success_reward(self):
        env = _env()
        obs, reward, terminated, truncated, _ = env.step(
            {"tool": "submit_final_answer", "arguments": {"tuples": [[1, "alpha"]]}}
        )
        assert obs["type"] == "episode_result"
        assert obs["payload"]["matched"] is True
        assert obs["reward"] == 1.0
        assert obs["terminated"] is True
        assert obs["step_number"] == 1
        assert reward == 1.0
        assert terminated is True
        assert truncated is False
        assert env.episode_over is True

    def test_multiset_submit_matches_out_of_order(self):
        env = _env(expected=[[1, "alpha"], [2, "beta"]])
        obs, reward, terminated, *_ = env.step(
            {"tool": "submit_final_answer", "arguments": {"tuples": [[2, "beta"], [1, "alpha"]]}}
        )
        assert obs["payload"]["matched"] is True
        assert reward == 1.0
        assert terminated is True

    def test_incorrect_submit_terminates_with_zero_reward(self):
        env = _env()
        obs, reward, terminated, *_ = env.step(
            {"tool": "submit_final_answer", "arguments": {"tuples": [[1, "wrong"]]}}
        )
        assert obs["payload"]["matched"] is False
        assert reward == 0.0
        assert terminated is True

    def test_submit_without_tuples_is_rejected(self):
        env = _env()
        obs, reward, terminated, truncated, info = env.step({"tool": "submit_final_answer", "arguments": {}})
        assert obs["type"] == "error"
        assert obs["payload"]["code"] == "invalid_arguments"
        assert reward == 0.0
        assert terminated is False
        assert info["step_number"] == 1
        assert env.episode_over is False

    def test_empty_submit_rejected_when_result_expected(self):
        env = _env(expected=[[1]])
        obs, *_ = env.step({"tool": "submit_final_answer", "arguments": {"tuples": []}})
        assert obs["payload"]["code"] == "invalid_arguments"
        assert env.episode_over is False

    def test_empty_submit_answers_empty_result_task(self):
        env = _env(expected=[])
        obs, reward, terminated, *_ = env.step(
            {"tool": "submit_final_answer", "arguments": {"tuples": []}}
        )
        assert obs["type"] == "episode_result"
        assert obs["payload"]["matched"] is True
        assert reward == 1.0
        assert terminated is True


class TestSubmitStateChecks:
    CHECK_ONE_SQL = "SELECT o_totalprice FROM orders WHERE o_orderkey = 7"
    CHECK_TWO_SQL = "SELECT count(*) FROM orders"
    RULE_SQL = (
        "SELECT o_totalprice = (SELECT sum(l_extendedprice) FROM lineitem WHERE l_orderkey = 7) "
        "FROM orders WHERE o_orderkey = 7"
    )

    @staticmethod
    def _passing_check_one() -> SqlResult:
        return SqlResult(columns=["o_totalprice"], rows=[[Decimal("34152.00")]], fetch_limited=False)

    def _cc_task(self, **overrides) -> TaskRecord:
        defaults = dict(
            task_id="t-cc",
            expected=[[7]],
            state_checks=(
                StateCheck(
                    sql=self.CHECK_ONE_SQL,
                    expected_tuples=[["34152.00"]],
                    matching=MatchingRules(numeric_tolerance=0.01),
                ),
                StateCheck(sql=self.CHECK_TWO_SQL, expected_tuples=[[25]]),
            ),
            task_type="CC",
            category="OLTP",
        )
        defaults.update(overrides)
        return _task(**defaults)

    def _cc_env(self, script: dict[str, SqlResult | Exception] | None = None, **task_overrides):
        adapter = ScriptedAdapter(script=script, result=SqlResult(columns=["id"], rows=[[1]], fetch_limited=False))
        registry = EngineRegistry()
        registry.register("postgresql", adapter)
        episode = FakeEpisodeTransaction()
        task = self._cc_task(**task_overrides)
        env = DBArenaEnv(
            FakeCatalog([task]),
            registry=registry,
            episode_transaction_factory=lambda engine, timeout: episode,
        )
        env.reset(options={"task_id": task.task_id})
        return env, adapter, episode

    def _submit(self, env, tuples):
        return env.step({"tool": "submit_final_answer", "arguments": {"tuples": tuples}})

    def test_successful_cc_submit(self):
        env, adapter, _ = self._cc_env(
            script={
                self.CHECK_ONE_SQL: self._passing_check_one(),
                self.CHECK_TWO_SQL: SqlResult(columns=["count"], rows=[[25]], fetch_limited=False),
            }
        )
        obs, reward, terminated, truncated, _ = self._submit(env, [[7]])
        assert obs["type"] == "episode_result"
        assert obs["payload"]["matched"] is True
        assert "Confirmation matched and 2 state check(s) passed." in obs["payload"]["message"]
        assert reward == 1.0
        assert terminated is True and truncated is False
        assert env.episode_over is True
        assert [c[1]["sql"] for c in adapter.calls if c[0] == "run_sql"] == [self.CHECK_ONE_SQL, self.CHECK_TWO_SQL]

    def test_confirmation_mismatch_names_confirmation_stage_and_skips_checks(self):
        env, adapter, _ = self._cc_env()
        obs, reward, terminated, *_ = self._submit(env, [[999]])
        assert obs["payload"]["matched"] is False
        assert obs["payload"]["message"].startswith("confirmation failed")
        assert reward == 0.0
        assert terminated is True
        assert adapter.calls == [] or all(c[0] != "run_sql" for c in adapter.calls)

    def test_passing_checks_with_wrong_confirmation_fail(self):
        env, _, _ = self._cc_env(
            script={
                self.CHECK_ONE_SQL: self._passing_check_one(),
                self.CHECK_TWO_SQL: SqlResult(columns=["count"], rows=[[25]], fetch_limited=False),
            }
        )
        obs, reward, terminated, *_ = self._submit(env, [[7], ["extra"]])
        assert obs["payload"]["matched"] is False
        assert obs["payload"]["message"].startswith("confirmation failed")
        assert reward == 0.0 and terminated is True

    def test_first_failing_check_named_by_position(self):
        env, _, _ = self._cc_env(
            script={
                self.CHECK_ONE_SQL: self._passing_check_one(),
                self.CHECK_TWO_SQL: SqlResult(columns=["count"], rows=[[26]], fetch_limited=False),
            }
        )
        obs, reward, terminated, *_ = self._submit(env, [[7]])
        assert obs["payload"]["matched"] is False
        assert obs["payload"]["message"].startswith("state check 2 failed")
        assert reward == 0.0 and terminated is True

    def test_check_error_degrades_to_failed_check(self):
        env, _, _ = self._cc_env(script={self.CHECK_ONE_SQL: QueryError('relation "orders" does not exist')})
        obs, reward, terminated, *_ = self._submit(env, [[7]])
        assert obs["payload"]["matched"] is False
        assert obs["payload"]["message"].startswith("state check 1 failed")
        assert reward == 0.0
        assert terminated is True

    def test_check_timeout_degrades_to_failed_check(self):
        env, _, _ = self._cc_env(
            script={
                self.CHECK_ONE_SQL: self._passing_check_one(),
                self.CHECK_TWO_SQL: QueryTimeoutError("statement timeout"),
            }
        )
        obs, reward, terminated, *_ = self._submit(env, [[7]])
        assert obs["payload"]["matched"] is False
        assert obs["payload"]["message"].startswith("state check 2 failed")
        assert reward == 0.0
        assert terminated is True

    def test_checks_run_with_evaluator_row_cap_and_task_timeout(self):
        env, adapter, _ = self._cc_env(
            script={
                self.CHECK_ONE_SQL: self._passing_check_one(),
                self.CHECK_TWO_SQL: SqlResult(columns=["count"], rows=[[25]], fetch_limited=False),
            }
        )
        self._submit(env, [[7]])
        check_calls = [c for c in adapter.calls if c[0] == "run_sql"]
        assert [c[1]["row_limit"] for c in check_calls] == [10_000, 10_000]
        assert [c[1]["timeout"] for c in check_calls] == [15, 15]

    def test_episode_connection_passed_and_rolled_back_on_submit(self):
        env, adapter, episode = self._cc_env(
            script={
                self.CHECK_ONE_SQL: self._passing_check_one(),
                self.CHECK_TWO_SQL: SqlResult(columns=["count"], rows=[[25]], fetch_limited=False),
            }
        )
        self._submit(env, [[7]])
        check_calls = [c for c in adapter.calls if c[0] == "run_sql"]
        assert all(c[1]["connection"] is episode.connection for c in check_calls)
        assert episode.rolled_back is True

    def test_successful_submit_disposes_the_engine(self):
        env, _, episode = self._cc_env(
            script={
                self.CHECK_ONE_SQL: self._passing_check_one(),
                self.CHECK_TWO_SQL: SqlResult(columns=["count"], rows=[[25]], fetch_limited=False),
            }
        )
        self._submit(env, [[7]])
        assert episode.rolled_back is True
        assert env._engine is None

    def test_failed_submit_disposes_the_engine(self):
        env, _, episode = self._cc_env()
        obs, reward, terminated, *_ = self._submit(env, [[999]])
        assert terminated is True and reward == 0.0
        assert episode.rolled_back is True
        assert env._engine is None

    def test_episode_rolled_back_on_truncation(self):
        env, _, episode = self._cc_env(max_steps=1)
        _, _, terminated, truncated, _ = _ = env.step({"tool": "describe_schema", "arguments": {}})
        assert terminated is False and truncated is True
        assert episode.rolled_back is True

    def test_truncation_disposes_the_engine(self):
        env, _, _ = self._cc_env(max_steps=1)
        env.step({"tool": "describe_schema", "arguments": {}})
        assert env._engine is None

    def test_check_rows_rendered_like_query_results(self):
        check_sql = "SELECT created FROM arena_item WHERE id = 1"
        task = self._cc_task(
            task_id="t-cc-render",
            expected=[[1]],
            state_checks=(StateCheck(sql=check_sql, expected_tuples=[["1995-09-01"]]),),
        )
        adapter = ScriptedAdapter(
            script={check_sql: SqlResult(columns=["created"], rows=[[date(1995, 9, 1)]], fetch_limited=False)}
        )
        registry = EngineRegistry()
        registry.register("postgresql", adapter)
        env = DBArenaEnv(
            FakeCatalog([task]),
            registry=registry,
            episode_transaction_factory=lambda engine, timeout: FakeEpisodeTransaction(),
        )
        env.reset(options={"task_id": task.task_id})
        obs, reward, terminated, *_ = self._submit(env, [[1]])
        assert obs["payload"]["matched"] is True
        assert reward == 1.0 and terminated is True

    def test_legacy_task_without_checks_unchanged(self):
        env, _, _ = self._cc_env(
            script={},
            task_id="t-legacy",
            expected=[[1, "alpha"]],
            state_checks=(),
            task_type="IS",
            category="OLAP",
        )
        obs, reward, terminated, *_ = self._submit(env, [[1, "alpha"]])
        assert obs["payload"]["matched"] is True
        assert obs["payload"]["message"] == "All expected tuples were matched."
        assert reward == 1.0 and terminated is True


RULE_SQL = (
    "SELECT o_totalprice = (SELECT sum(l_extendedprice) FROM lineitem WHERE l_orderkey = 7) "
    "FROM orders WHERE o_orderkey = 7"
)


class TestSubmitRules:
    def _rules_env(self, script, verification_rules, **task_overrides):
        adapter = ScriptedAdapter(script=script, result=SqlResult(columns=["id"], rows=[[1]], fetch_limited=False))
        registry = EngineRegistry()
        registry.register("postgresql", adapter)
        episode = FakeEpisodeTransaction()
        task = _task(
            task_id="t-rules",
            expected=[["ok"]],
            verification_rules=verification_rules,
            **task_overrides,
        )
        env = DBArenaEnv(
            FakeCatalog([task]),
            registry=registry,
            episode_transaction_factory=lambda engine, timeout: episode,
        )
        env.reset(options={"task_id": task.task_id})
        return env, adapter, episode

    def _submit(self, env, tuples="ok"):
        return env.step({"tool": "submit_final_answer", "arguments": {"tuples": [[tuples]]}})

    def test_successful_submit_with_passing_rules(self):
        env, adapter, episode = self._rules_env(
            script={RULE_SQL: SqlResult(columns=["?column?"], rows=[[True]], fetch_limited=False)},
            verification_rules=(VerificationRule(sql=RULE_SQL, description="total matches lines"),),
        )
        obs, reward, terminated, *_ = self._submit(env)
        assert obs["payload"]["matched"] is True
        assert obs["payload"]["message"] == "Confirmation matched and 1 rule(s) passed."
        assert reward == 1.0 and terminated is True
        assert all(c[1]["connection"] is episode.connection for c in adapter.calls if c[0] == "run_sql")
        assert episode.rolled_back is True

    def test_passing_rules_after_passing_checks(self):
        check_sql = "SELECT count(*) FROM orders"
        env, _, _ = self._rules_env(
            script={
                check_sql: SqlResult(columns=["count"], rows=[[25]], fetch_limited=False),
                RULE_SQL: SqlResult(columns=["?column?"], rows=[[True]], fetch_limited=False),
            },
            verification_rules=(VerificationRule(sql=RULE_SQL),),
            state_checks=(StateCheck(sql=check_sql, expected_tuples=[[25]]),),
        )
        obs, reward, terminated, *_ = self._submit(env)
        assert obs["payload"]["matched"] is True
        assert obs["payload"]["message"] == "Confirmation matched and 1 state check(s) and 1 rule(s) passed."
        assert reward == 1.0 and terminated is True

    def test_failing_rule_named_by_description(self):
        env, _, _ = self._rules_env(
            script={RULE_SQL: SqlResult(columns=["?column?"], rows=[[False]], fetch_limited=False)},
            verification_rules=(VerificationRule(sql=RULE_SQL, description="total matches lines"),),
        )
        obs, reward, terminated, *_ = self._submit(env)
        assert obs["payload"]["matched"] is False
        assert obs["payload"]["message"] == "rule 1 (total matches lines) failed: expected a single boolean-true result"
        assert reward == 0.0 and terminated is True

    def test_failing_rule_without_description_uses_position_only(self):
        env, _, _ = self._rules_env(
            script={RULE_SQL: SqlResult(columns=["?column?"], rows=[[False]], fetch_limited=False)},
            verification_rules=(VerificationRule(sql=RULE_SQL),),
        )
        obs, *_ = self._submit(env)
        assert obs["payload"]["message"] == "rule 1 failed: expected a single boolean-true result"

    def test_second_rule_failure_names_second_position(self):
        env, _, _ = self._rules_env(
            script={
                RULE_SQL: SqlResult(columns=["?column?"], rows=[[True]], fetch_limited=False),
                "SELECT false": SqlResult(columns=["?column?"], rows=[[False]], fetch_limited=False),
            },
            verification_rules=(
                VerificationRule(sql=RULE_SQL),
                VerificationRule(sql="SELECT false"),
            ),
        )
        obs, *_ = self._submit(env)
        assert obs["payload"]["message"] == "rule 2 failed: expected a single boolean-true result"

    def test_check_failure_reported_before_rule_failure(self):
        env, _, _ = self._rules_env(
            script={
                RULE_SQL: SqlResult(columns=["?column?"], rows=[[False]], fetch_limited=False),
            },
            verification_rules=(VerificationRule(sql=RULE_SQL),),
            state_checks=(StateCheck(sql="SELECT count(*) FROM orders", expected_tuples=[[999]]),),
        )
        obs, *_ = self._submit(env)
        assert obs["payload"]["message"].startswith("state check 1 failed")

    def test_rule_error_degrades_to_failed_rule(self):
        env, _, _ = self._rules_env(
            script={RULE_SQL: QueryError('relation "orders" does not exist')},
            verification_rules=(VerificationRule(sql=RULE_SQL),),
        )
        obs, reward, terminated, *_ = self._submit(env)
        assert obs["payload"]["matched"] is False
        assert obs["payload"]["message"].startswith("rule 1 failed")
        assert reward == 0.0 and terminated is True

    def test_rule_timeout_degrades_to_failed_rule(self):
        env, _, _ = self._rules_env(
            script={RULE_SQL: QueryTimeoutError("statement timeout")},
            verification_rules=(VerificationRule(sql=RULE_SQL),),
        )
        obs, reward, terminated, *_ = self._submit(env)
        assert obs["payload"]["message"].startswith("rule 1 failed")
        assert reward == 0.0 and terminated is True

    @pytest.mark.parametrize(
        "rows",
        [
            [[True], [True]],
            [[1]],
            [[True, False]],
            [],
        ],
        ids=["multi-row", "non-boolean", "two-columns", "empty"],
    )
    def test_non_boolean_true_results_fail(self, rows):
        env, _, _ = self._rules_env(
            script={RULE_SQL: SqlResult(columns=["x"], rows=rows, fetch_limited=False)},
            verification_rules=(VerificationRule(sql=RULE_SQL),),
        )
        obs, reward, terminated, *_ = self._submit(env)
        assert obs["payload"]["matched"] is False
        assert obs["payload"]["message"] == "rule 1 failed: expected a single boolean-true result"
        assert reward == 0.0 and terminated is True

    def test_tasks_without_rules_unchanged(self):
        env, _, _ = self._rules_env(script={}, verification_rules=())
        obs, reward, terminated, *_ = self._submit(env)
        assert obs["payload"]["matched"] is True
        assert obs["payload"]["message"] == "All expected tuples were matched."
        assert reward == 1.0 and terminated is True


class TestTruncation:
    def test_truncates_at_max_steps(self):
        env = _env(max_steps=2)
        _, _, t1, tr1, _ = env.step({"tool": "describe_schema", "arguments": {}})
        assert t1 is False and tr1 is False
        obs2, _, t2, tr2, _ = env.step({"tool": "describe_schema", "arguments": {}})
        assert t2 is False and tr2 is True
        assert obs2["truncated"] is True
        assert obs2["reward"] == 0.0
        assert obs2["step_number"] == 2
        assert env.episode_over is True

    def test_submit_on_final_step_terminates_not_truncates(self):
        env = _env(max_steps=1)
        _, _, terminated, truncated, _ = env.step(
            {"tool": "submit_final_answer", "arguments": {"tuples": [[1, "alpha"]]}}
        )
        assert terminated is True
        assert truncated is False

    def test_step_after_done_is_rejected(self):
        env = _env(max_steps=1)
        env.step({"tool": "describe_schema", "arguments": {}})
        with pytest.raises(EpisodeClosedError, match="reset"):
            env.step({"tool": "describe_schema", "arguments": {}})


class TestRewards:
    def test_only_successful_submit_rewards(self):
        env = _env()
        _, r1, *_ = env.step({"tool": "describe_schema", "arguments": {}})
        _, r2, *_ = env.step({"tool": "run_sql", "arguments": {"sql": "SELECT 1"}})
        _, r3, *_ = env.step({"tool": "submit_final_answer", "arguments": {"tuples": [[9]]}})
        assert (r1, r2, r3) == (0.0, 0.0, 0.0)


class TestSpaces:
    def test_action_and_observation_spaces_accept_envelopes(self):
        env = _env()
        assert env.action_space.contains({"tool": "run_sql", "arguments": {"sql": "SELECT 1"}})
        assert env.observation_space.contains({"schema_version": "x", "rows": [1, [2], {"a": None}]})


class TestEngineLifecycle:
    def test_error_steps_keep_the_engine(self):
        env = _env()
        obs, *_ = env.step({"tool": "drop_database", "arguments": {}})
        assert obs["type"] == "error"
        assert env._engine is not None
        assert env.episode_over is False

    def test_query_error_keeps_the_engine(self):
        adapter = ScriptedAdapter(script={"SELECT 1": QueryError("syntax error")})
        env = _env(adapter=adapter)
        obs, *_ = env.step({"tool": "run_sql", "arguments": {"sql": "SELECT 1"}})
        assert obs["type"] == "error"
        assert env._engine is not None
        assert env.episode_over is False

    def test_reset_after_episode_end_opens_a_fresh_episode(self):
        env = _env()
        env.step({"tool": "submit_final_answer", "arguments": {"tuples": [[9]]}})
        assert env._engine is None
        obs, _ = env.reset(options={"task_id": "t-1"})
        assert obs["type"] == "schema_description"
        assert env._engine is not None
        next_obs, *_ = env.step({"tool": "run_sql", "arguments": {"sql": "SELECT 1"}})
        assert next_obs["type"] == "query_result"


class TestClose:
    def test_close_is_idempotent(self):
        env = _env()
        env.close()
        env.close()
