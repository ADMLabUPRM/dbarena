from dataclasses import replace
from typing import Any

import gymnasium
from sqlalchemy import Engine, create_engine

from dbarena.actions.base import UnsupportedEngineError
from dbarena.actions.errors import QueryError, QueryTimeoutError
from dbarena.actions.registry import (
    DESCRIBE_SCHEMA,
    DESCRIBE_TABLE,
    DESCRIBE_TABLES,
    RUN_RAG_QUERY,
    RUN_SQL,
    SUBMIT_FINAL_ANSWER,
    EngineRegistry,
    default_registry,
)
from dbarena.catalog.matching import match_tuples
from dbarena.catalog.records import TaskRecord
from dbarena.embeddings import EmbeddingError, EmbeddingResult
from dbarena.env.episode import EpisodeTransaction
from dbarena.env.errors import EpisodeClosedError, ResetError
from dbarena.env.interface import action_specs, observation_space_catalog
from dbarena.env.spaces import JSONSpace
from dbarena.judge import JudgeError
from dbarena.models.actions import ActionCall
from dbarena.models.observations import ErrorCode, Observation
from dbarena.models.terminal import StateCheck, VerificationRule
from dbarena.observations.builders import (
    episode_result_observation,
    error_observation,
    query_result_observation,
    schema_description_observation,
    table_description_observation,
)
from dbarena.observations.values import jsonify_row

DEFAULT_ROW_LIMIT = 50
STATE_CHECK_ROW_LIMIT = 10_000

_ACTION_TUPLES = (RUN_SQL, DESCRIBE_TABLE, DESCRIBE_TABLES, DESCRIBE_SCHEMA, SUBMIT_FINAL_ANSWER, RUN_RAG_QUERY)

QUERY_VECTOR_PLACEHOLDER = ":query_vector"


def _require_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _require_str_list(arguments: dict[str, Any], key: str) -> list[str]:
    value = arguments.get(key)
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"'{key}' is required and must be a non-empty array of strings")
    return value


class DBArenaEnv(gymnasium.Env):
    metadata: dict[str, Any] = {"render_modes": []}

    action_space: JSONSpace
    observation_space: JSONSpace

    def __init__(
        self,
        catalog: Any,
        registry: EngineRegistry | None = None,
        default_row_limit: int = DEFAULT_ROW_LIMIT,
        episode_transaction_factory: Any = None,
        embed_resolver: Any = None,
        judge_resolver: Any = None,
    ):
        super().__init__()
        self.catalog = catalog
        self.registry = registry if registry is not None else default_registry()
        self.default_row_limit = default_row_limit
        self._episode_transaction_factory = episode_transaction_factory or EpisodeTransaction
        self._embed_resolver = embed_resolver
        self._judge_resolver = judge_resolver
        self.action_space = JSONSpace()
        self.observation_space = JSONSpace()
        self._task: TaskRecord | None = None
        self._engine: Engine | None = None
        self._adapter = None
        self._episode: Any | None = None
        self._steps = 0
        self._done = False

    @property
    def task(self) -> TaskRecord | None:
        return self._task

    @property
    def available_actions(self) -> tuple[str, ...]:
        return _ACTION_TUPLES

    @property
    def step_number(self) -> int:
        return self._steps

    @property
    def episode_over(self) -> bool:
        return self._done

    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None):
        super().reset(seed=seed)
        task_id = (options or {}).get("task_id")
        if not task_id or not isinstance(task_id, str):
            raise ResetError("reset requires options={'task_id': <task id>}")
        task = self.catalog.get(task_id)
        if task is None:
            raise ResetError(f"unknown task {task_id!r}")
        self._close_engine()
        engine = create_engine(
            task.sqlalchemy_url, pool_pre_ping=True, pool_size=1, max_overflow=1
        )
        adapter = self.registry.adapter_for(task.sqlalchemy_url)
        try:
            tables = adapter.describe_schema(engine)
        except Exception as exc:
            engine.dispose()
            raise ResetError(f"cannot reach target database for task {task_id!r}: {exc}") from exc
        self._task = task
        self._engine = engine
        self._adapter = adapter
        self._episode = None
        try:
            self._episode = self._episode_transaction_factory(engine, task.query_timeout)
        except Exception as exc:
            engine.dispose()
            raise ResetError(
                f"cannot open the episode transaction for task {task_id!r}: {exc}"
            ) from exc
        self._steps = 0
        self._done = False
        self._task_block = {
            "task_id": task.task_id,
            "goal": task.goal,
            "submission": task.submission,
            "category": task.category,
            "task_type": task.task_type,
        }
        self._interface_meta = {
            "available_actions": action_specs(),
            "observation_space": observation_space_catalog(),
        }
        observation = replace(
            schema_description_observation(None, tables),
            meta={**self._interface_meta, "last_action": None},
            reward=None,
            terminated=False,
            truncated=False,
            step_number=0,
            task=dict(self._task_block),
        )
        return observation.to_dict(), {"task_id": task_id, "available_actions": action_specs()}

    def step(self, action: dict[str, Any]):
        if self._done:
            raise EpisodeClosedError("episode is over; call reset() to start a new episode")
        call, call_error, attempted = self._parse_action(action)
        self._steps += 1
        if call is None:
            observation = error_observation(
                attempted, ErrorCode.INVALID_ACTION, call_error or "malformed action"
            )
            return self._finish(observation, 0.0, terminated=False, last_action=attempted)
        observation, reward, terminated = self._dispatch(call)
        return self._finish(observation, reward, terminated, last_action=call)

    def _parse_action(self, action: Any) -> tuple[ActionCall | None, str | None, ActionCall | None]:
        if not isinstance(action, dict) or "tool" not in action:
            return None, "action must be an object with a 'tool' field", None
        try:
            call = ActionCall.from_dict(action)
        except ValueError as exc:
            return None, str(exc), None
        if call.tool not in _ACTION_TUPLES:
            return None, f"unknown tool {call.tool!r}", call
        return call, None, call

    def _dispatch(self, call: ActionCall) -> tuple[Observation, float, bool]:
        if call.tool == SUBMIT_FINAL_ANSWER:
            return self._submit(call)
        try:
            if call.tool == RUN_SQL:
                return self._run_sql(call)
            if call.tool == DESCRIBE_TABLE:
                return self._describe_table(call)
            if call.tool == DESCRIBE_TABLES:
                return self._describe_tables(call)
            if call.tool == DESCRIBE_SCHEMA:
                return self._describe_schema(call)
            if call.tool == RUN_RAG_QUERY:
                return self._run_rag_query(call)
            return error_observation(call, ErrorCode.INVALID_ACTION, f"unknown tool {call.tool!r}"), 0.0, False
        except UnsupportedEngineError as exc:
            return error_observation(call, ErrorCode.UNSUPPORTED_ENGINE, str(exc)), 0.0, False
        except QueryTimeoutError as exc:
            return error_observation(call, ErrorCode.TIMEOUT, str(exc)), 0.0, False
        except QueryError as exc:
            return error_observation(call, ErrorCode.QUERY_ERROR, str(exc)), 0.0, False
        except EmbeddingError as exc:
            return error_observation(call, ErrorCode.EMBEDDING_ERROR, str(exc)), 0.0, False
        except JudgeError as exc:
            return error_observation(call, ErrorCode.JUDGE_ERROR, str(exc)), 0.0, False
        except ValueError as exc:
            return error_observation(call, ErrorCode.INVALID_ARGUMENTS, str(exc)), 0.0, False

    def _run_rag_query(self, call: ActionCall) -> tuple[Observation, float, bool]:
        sql = _require_str(call.arguments, "sql")
        embed_text = _require_str(call.arguments, "text")
        if QUERY_VECTOR_PLACEHOLDER not in sql:
            return (
                error_observation(
                    call,
                    ErrorCode.INVALID_ARGUMENTS,
                    f"sql must contain the {QUERY_VECTOR_PLACEHOLDER!r} placeholder where the "
                    "query embedding should be inserted",
                ),
                0.0,
                False,
            )
        if self._embed_resolver is None:
            return (
                error_observation(
                    call,
                    ErrorCode.EMBEDDING_ERROR,
                    "embedding model is not configured; configure it in the admin GUI and re-embed",
                ),
                0.0,
                False,
            )
        result = self._embed_resolver(embed_text)
        if not isinstance(result, EmbeddingResult):
            result = EmbeddingResult(vector=list(result[0]), model=str(result[1]), dim=int(result[2]))
        resolved_sql = sql.replace(
            QUERY_VECTOR_PLACEHOLDER, f"CAST('{result.vector_literal}' AS vector)"
        )
        return self._run_sql(replace(call, tool=RUN_SQL, arguments={"sql": resolved_sql}))

    def _run_sql(self, call: ActionCall) -> tuple[Observation, float, bool]:
        sql = _require_str(call.arguments, "sql")
        row_limit = self._row_limit()
        result = self._adapter.run_sql(
            self._engine,
            sql,
            row_limit=row_limit,
            timeout_seconds=self._task.query_timeout,
            connection=self._episode.connection if self._episode is not None else None,
        )
        observation, _ = query_result_observation(call, result.columns, result.rows, row_limit=row_limit)
        return observation, 0.0, False

    def _describe_table(self, call: ActionCall) -> tuple[Observation, float, bool]:
        table = _require_str(call.arguments, "table")
        description = self._adapter.describe_table(self._engine, table)
        if not description.found:
            return (
                error_observation(call, ErrorCode.NOT_FOUND, f"table {table!r} does not exist"),
                0.0,
                False,
            )
        return table_description_observation(call, [description]), 0.0, False

    def _describe_tables(self, call: ActionCall) -> tuple[Observation, float, bool]:
        tables = _require_str_list(call.arguments, "tables")
        descriptions = self._adapter.describe_tables(self._engine, tables)
        return table_description_observation(call, descriptions), 0.0, False

    def _describe_schema(self, call: ActionCall) -> tuple[Observation, float, bool]:
        tables = self._adapter.describe_schema(self._engine)
        return schema_description_observation(call, tables), 0.0, False

    def _submit(self, call: ActionCall) -> tuple[Observation, float, bool]:
        condition = self._task.terminal_condition
        if condition.judge is not None:
            return self._submit_judged(call, condition)
        tuples = call.arguments.get("tuples")
        if not isinstance(tuples, list) or any(not isinstance(row, list) for row in tuples):
            return (
                error_observation(
                    call,
                    ErrorCode.INVALID_ARGUMENTS,
                    "submit_final_answer requires 'tuples': an array of tuples",
                ),
                0.0,
                False,
            )
        expected = condition.expected_tuples
        if len(tuples) == 0 and expected:
            return (
                error_observation(
                    call,
                    ErrorCode.INVALID_ARGUMENTS,
                    "submitted answer must contain at least one tuple",
                ),
                0.0,
                False,
            )
        matched, message = match_tuples(tuples, expected, condition.matching)
        if not matched:
            return (
                episode_result_observation(call, False, f"confirmation failed: {message}", 0.0),
                0.0,
                True,
            )
        if condition.state_checks:
            checks_ok, check_message = self._evaluate_state_checks(condition.state_checks)
            if not checks_ok:
                return (
                    episode_result_observation(call, False, check_message, 0.0),
                    0.0,
                    True,
                )
        if condition.rules:
            rules_ok, rule_message = self._evaluate_rules(condition.rules)
            if not rules_ok:
                return (
                    episode_result_observation(call, False, rule_message, 0.0),
                    0.0,
                    True,
                )
        stages = []
        if condition.state_checks:
            stages.append(f"{len(condition.state_checks)} state check(s)")
        if condition.rules:
            stages.append(f"{len(condition.rules)} rule(s)")
        if stages:
            message = "Confirmation matched and " + " and ".join(stages) + " passed."
        reward = 1.0
        return episode_result_observation(call, True, message, reward), reward, True

    def _submit_judged(self, call: ActionCall, condition: Any) -> tuple[Observation, float, bool]:
        answer = call.arguments.get("answer")
        if "tuples" in call.arguments:
            return (
                error_observation(
                    call,
                    ErrorCode.INVALID_ARGUMENTS,
                    "this task is judge-verified: submit the natural-language 'answer' instead of tuples",
                ),
                0.0,
                False,
            )
        if not isinstance(answer, str) or not answer.strip():
            return (
                error_observation(
                    call,
                    ErrorCode.INVALID_ARGUMENTS,
                    "submit_final_answer requires 'answer': a non-empty natural-language response",
                ),
                0.0,
                False,
            )
        if self._judge_resolver is None:
            return (
                error_observation(
                    call,
                    ErrorCode.JUDGE_ERROR,
                    "judge service is not configured; set JUDGE_URL for the backend",
                ),
                0.0,
                False,
            )
        try:
            outcome = self._judge_resolver(
                self._task.goal,
                condition.judge.expected,
                answer,
                condition.judge.key_points,
                condition.judge.reward_mode,
            )
        except JudgeError as exc:
            return error_observation(call, ErrorCode.JUDGE_ERROR, str(exc)), 0.0, False
        if condition.judge.reward_mode == "dense":
            message = "judge verdict: pass" if outcome.passed else "judge verdict: fail"
            return (
                episode_result_observation(call, outcome.passed, message, outcome.score),
                outcome.score,
                True,
            )
        if outcome.passed:
            return (
                episode_result_observation(call, True, "judge verdict: pass", 1.0),
                1.0,
                True,
            )
        return (
            episode_result_observation(call, False, "judge verdict: fail", 0.0),
            0.0,
            True,
        )

    def _evaluate_rules(self, rules: tuple[VerificationRule, ...]) -> tuple[bool, str]:
        for position, rule in enumerate(rules, start=1):
            label = f"rule {position} ({rule.description})" if rule.description else f"rule {position}"
            try:
                result = self._adapter.run_sql(
                    self._engine,
                    rule.sql,
                    row_limit=STATE_CHECK_ROW_LIMIT,
                    timeout_seconds=self._task.query_timeout,
                    connection=self._episode.connection if self._episode is not None else None,
                )
            except (QueryError, QueryTimeoutError, UnsupportedEngineError) as exc:
                return False, f"{label} failed: {exc}"
            rows = [jsonify_row(row)[0] for row in result.rows]
            if len(rows) != 1 or len(rows[0]) != 1 or rows[0][0] is not True:
                return False, f"{label} failed: expected a single boolean-true result"
        return True, ""

    def _evaluate_state_checks(self, checks: tuple[StateCheck, ...]) -> tuple[bool, str]:
        for position, check in enumerate(checks, start=1):
            try:
                result = self._adapter.run_sql(
                    self._engine,
                    check.sql,
                    row_limit=STATE_CHECK_ROW_LIMIT,
                    timeout_seconds=self._task.query_timeout,
                    connection=self._episode.connection if self._episode is not None else None,
                )
            except (QueryError, QueryTimeoutError, UnsupportedEngineError) as exc:
                return False, f"state check {position} failed: {exc}"
            rows = [jsonify_row(row)[0] for row in result.rows]
            matched, message = match_tuples(rows, check.expected_tuples, check.matching)
            if not matched:
                return False, f"state check {position} failed: {message}"
        return True, ""

    def _finish(
        self,
        observation: Observation,
        reward: float,
        terminated: bool,
        last_action: ActionCall | None,
    ) -> tuple:
        truncated = (not terminated) and self._steps >= self._task.max_steps
        observation = replace(
            observation,
            meta={
                **self._interface_meta,
                **observation.meta,
                "last_action": last_action.to_dict() if last_action is not None else None,
            },
            reward=reward,
            terminated=terminated,
            truncated=truncated,
            step_number=self._steps,
            task=dict(self._task_block),
        )
        if terminated or truncated:
            self._done = True
            self._close_engine()
        return observation.to_dict(), reward, terminated, truncated, {"step_number": self._steps}

    def _row_limit(self) -> int:
        return self._task.row_limit if self._task.row_limit is not None else self.default_row_limit

    def _close_engine(self) -> None:
        if self._episode is not None:
            self._episode.rollback()
            self._episode = None
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def close(self):
        self._close_engine()
