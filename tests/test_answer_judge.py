import json
import pathlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from dbarena.catalog.records import TaskRecord
from dbarena.catalog.validation import parse_task_payload, validate_task_payload
from dbarena.env.environment import DBArenaEnv
from dbarena.judge import (
    JudgeError as JudgeClientError,
)
from dbarena.judge import (
    JudgeOutcome,
    build_judge_resolver,
    post_verdict,
)
from dbarena.models.terminal import JudgeCondition, TerminalCondition

R03_TASK_ID = "DBA-R03-001"

EXPECTED_ANSWER = (
    "Pools exhausted on 4.2.0 because timed-out connections were returned to the pool unreset "
    "(INC-1047) and failover triggered retry storms that pinned the pool (INC-1058); sustained "
    "load exposed both until the wait queue overflowed (INC-1041, INC-1052). Fixed in 4.2.1 with "
    "eager connection release and jittered failover backoff."
)


@pytest.fixture
def tasks_table(pg_engine):
    """Apply the real admindb DDL (with the relaxed terminal-condition constraint)."""
    ddl = (pathlib.Path(__file__).resolve().parents[1] / "db" / "init" / "001-schema.sql").read_text(
        encoding="utf-8"
    )
    with pg_engine.begin() as conn:
        conn.execute(text(ddl))
    yield pg_engine


@pytest.fixture
def judge_client():
    from judge.app import app

    with patch("judge.app._chat_completion") as mock:
        yield TestClient(app), mock


# ---------------------------------------------------------------- judge service


def test_judge_passes_supportive_answer(judge_client):
    client, mock = judge_client
    mock.return_value = "PASS"
    response = client.post("/judge", json={"goal": "g", "expected": "e", "submitted": "s"})
    assert response.status_code == 200 and response.json() == {"pass": True}


def test_judge_fails_insufficient_answer(judge_client):
    client, mock = judge_client
    mock.return_value = "FAIL"
    response = client.post("/judge", json={"goal": "g", "expected": "e", "submitted": "s"})
    assert response.status_code == 200 and response.json() == {"pass": False}


def test_judge_unparsable_reply_is_502(judge_client):
    client, mock = judge_client
    mock.return_value = "I cannot decide, sorry"
    response = client.post("/judge", json={"goal": "g", "expected": "e", "submitted": "s"})
    assert response.status_code == 502


def test_judge_validation_requires_fields(judge_client):
    client, _mock = judge_client
    assert client.post("/judge", json={"goal": "g"}).status_code == 422


def test_post_verdict_parses_service_reply():
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"pass": True}).encode()

    with patch("dbarena.judge.urllib.request.urlopen", return_value=FakeResponse()):
        assert post_verdict("http://judge:9100", "g", "e", "s") == JudgeOutcome(passed=True)


def test_post_verdict_dense_parses_graded_outcome():
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"pass": False, "score": 0.5, "satisfied": 2, "total": 4}).encode()

    with patch("dbarena.judge.urllib.request.urlopen", return_value=FakeResponse()):
        outcome = post_verdict(
            "http://judge:9100", "g", "e", "s", key_points=["a", "b", "c", "d"], reward_mode="dense"
        )
    assert outcome.passed is False
    assert outcome.score == 0.5 and outcome.satisfied == 2 and outcome.total == 4


def test_post_verdict_dense_malformed_graded_body_raises():
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"pass": False}).encode()

    with patch("dbarena.judge.urllib.request.urlopen", return_value=FakeResponse()):
        with pytest.raises(JudgeClientError):
            post_verdict("http://judge:9100", "g", "e", "s", key_points=["a"], reward_mode="dense")


def test_post_verdict_malformed_body_raises():
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"verdict": "maybe"}).encode()

    with patch("dbarena.judge.urllib.request.urlopen", return_value=FakeResponse()):
        with pytest.raises(JudgeClientError):
            post_verdict("http://judge:9100", "g", "e", "s")


def test_build_judge_resolver_returns_none_when_unconfigured():
    assert build_judge_resolver(None) is None
    assert build_judge_resolver("") is None


# ---------------------------------------------------------------- catalog shape


def _judged_payload():
    return {
        "task_id": R03_TASK_ID,
        "goal": "explain and cite",
        "submission": "submit answer",
        "category": "RAG",
        "task_type": "IS",
        "max_steps": 8,
        "query_timeout": 30,
        "sqlalchemy_url": "postgresql+psycopg://x",
        "terminal_condition": {"judge": {"expected": EXPECTED_ANSWER}},
    }


def test_validation_accepts_judge_shaped_condition():
    assert validate_task_payload(_judged_payload()) == []


def test_validation_rejects_empty_judge_expected():
    payload = _judged_payload()
    payload["terminal_condition"] = {"judge": {"expected": "   "}}
    errors = validate_task_payload(payload)
    assert any(field.endswith("judge.expected") for field, _ in errors)


def test_validation_still_requires_tuples_without_judge():
    payload = _judged_payload()
    payload["task_id"] = "DBA-X-001"
    payload["terminal_condition"] = {"matching": {"order_sensitive": False}}
    errors = validate_task_payload(payload)
    assert any(field.endswith("expected_tuples") for field, _ in errors)


def test_judged_record_stores_and_round_trips(tasks_table):
    payload = _judged_payload()
    payload["sqlalchemy_url"] = "postgresql+psycopg://x"
    record = parse_task_payload(payload)
    engine = tasks_table
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, goal, submission, category, task_type, max_steps, "
                "query_timeout, sqlalchemy_url, terminal_condition) "
                "VALUES (:task_id, :goal, :submission, :category, :task_type, :max_steps, "
                ":query_timeout, :sqlalchemy_url, CAST(:tc AS jsonb))"
            ),
            {
                "task_id": record.task_id,
                "goal": record.goal,
                "submission": record.submission,
                "category": record.category,
                "task_type": record.task_type,
                "max_steps": record.max_steps,
                "query_timeout": record.query_timeout,
                "sqlalchemy_url": record.sqlalchemy_url,
                "tc": json.dumps(record.terminal_condition.to_dict()),
            },
        )
    stored = engine.connect().execute(
        text("SELECT terminal_condition -> 'judge' ->> 'expected' AS expected FROM tasks WHERE task_id = :id"),
        {"id": R03_TASK_ID},
    ).scalar()
    assert stored == EXPECTED_ANSWER
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tasks WHERE task_id = :id"), {"id": R03_TASK_ID})


# ---------------------------------------------------------------- env routing


def _judged_task(pg_url, judge=None):
    return TaskRecord(
        task_id=R03_TASK_ID,
        goal="Explain why pools exhaust and cite the incidents.",
        submission="Call submit_final_answer with the 'answer' argument.",
        category="RAG",
        task_type="IS",
        max_steps=8,
        query_timeout=10,
        sqlalchemy_url=pg_url,
        terminal_condition=TerminalCondition(
            judge=JudgeCondition(
                expected=EXPECTED_ANSWER,
                key_points=tuple(judge["key_points"]) if judge else (),
                reward_mode=judge["reward_mode"] if judge else "sparse",
            ),
        ),
        row_limit=None,
    )


class _SingleTaskCatalog:
    def __init__(self, record):
        self._record = record

    def get(self, task_id):
        return self._record if task_id == self._record.task_id else None


def test_judged_submission_pass(pg_url):
    env = DBArenaEnv(
        _SingleTaskCatalog(_judged_task(pg_url)),
        judge_resolver=lambda g, e, s, kp=None, rm="sparse": JudgeOutcome(passed=True),
    )
    env.reset(options={"task_id": R03_TASK_ID})
    obs, reward, terminated, _truncated, _info = env.step(
        {"tool": "submit_final_answer", "arguments": {"answer": EXPECTED_ANSWER}}
    )
    assert obs["type"] == "episode_result"
    assert reward == 1.0 and terminated is True
    assert "pass" in obs["payload"]["message"].lower()
    assert EXPECTED_ANSWER not in obs["payload"]["message"]


def test_judged_submission_fail_has_no_feedback(pg_url):
    env = DBArenaEnv(
        _SingleTaskCatalog(_judged_task(pg_url)),
        judge_resolver=lambda g, e, s, kp=None, rm="sparse": JudgeOutcome(passed=False),
    )
    env.reset(options={"task_id": R03_TASK_ID})
    obs, reward, terminated, _truncated, _info = env.step(
        {"tool": "submit_final_answer", "arguments": {"answer": "a wrong answer"}}
    )
    assert obs["type"] == "episode_result"
    assert reward == 0.0 and terminated is True
    assert "fail" in obs["payload"]["message"].lower()
    assert EXPECTED_ANSWER not in obs["payload"]["message"]
    assert "wrong" not in obs["payload"]["message"].lower()


def test_judged_submission_unreachable_keeps_episode_open(pg_url):
    def failing(goal, expected, submitted, key_points=None, reward_mode="sparse"):
        raise JudgeClientError("judge service unreachable: connection refused")

    env = DBArenaEnv(_SingleTaskCatalog(_judged_task(pg_url)), judge_resolver=failing)
    env.reset(options={"task_id": R03_TASK_ID})
    obs, reward, terminated, _truncated, _info = env.step(
        {"tool": "submit_final_answer", "arguments": {"answer": EXPECTED_ANSWER}}
    )
    assert obs["type"] == "error"
    assert obs["payload"]["code"] == "judge_error"
    assert reward == 0.0 and terminated is False
    assert env._engine is not None
    assert env.episode_over is False


def test_judged_task_rejects_tuples(pg_url):
    env = DBArenaEnv(
        _SingleTaskCatalog(_judged_task(pg_url)),
        judge_resolver=lambda g, e, s, kp=None, rm="sparse": JudgeOutcome(passed=True),
    )
    env.reset(options={"task_id": R03_TASK_ID})
    obs, _reward, _terminated, _truncated, _info = env.step(
        {"tool": "submit_final_answer", "arguments": {"tuples": [["x"]]}}
    )
    assert obs["type"] == "error"
    assert obs["payload"]["code"] == "invalid_arguments"
    assert "answer" in obs["payload"]["message"]


def test_judged_task_without_resolver_is_a_structured_error(pg_url):
    env = DBArenaEnv(_SingleTaskCatalog(_judged_task(pg_url)))
    env.reset(options={"task_id": R03_TASK_ID})
    obs, _reward, _terminated, _truncated, _info = env.step(
        {"tool": "submit_final_answer", "arguments": {"answer": EXPECTED_ANSWER}}
    )
    assert obs["type"] == "error"
    assert obs["payload"]["code"] == "judge_error"
    assert "not configured" in obs["payload"]["message"]


DENSE_KEY_POINTS = (
    "the response cache leak on 4.2.0 (INC-1044)",
    "the cache stampede with single-flight locking (INC-1120)",
    "the stale authorization cache after role changes (INC-1121)",
    "cites the incident ids INC-1044, INC-1120, and INC-1121",
)


def test_dense_submission_full_conveyance_earns_1_0(pg_url):
    env = DBArenaEnv(
        _SingleTaskCatalog(_judged_task(pg_url, judge={"key_points": DENSE_KEY_POINTS, "reward_mode": "dense"})),
        judge_resolver=lambda g, e, s, kp=None, rm="dense": JudgeOutcome(passed=True, score=1.0, satisfied=4, total=4),
    )
    env.reset(options={"task_id": R03_TASK_ID})
    obs, reward, terminated, _truncated, _info = env.step(
        {"tool": "submit_final_answer", "arguments": {"answer": "A faithful answer in different words."}}
    )
    assert obs["type"] == "episode_result"
    assert reward == 1.0 and terminated is True
    assert obs["payload"]["message"] == "judge verdict: pass"
    assert obs["payload"]["reward"] == 1.0
    assert "/4" not in obs["payload"]["message"]


def test_dense_submission_partial_earns_partial_credit(pg_url):
    env = DBArenaEnv(
        _SingleTaskCatalog(_judged_task(pg_url, judge={"key_points": DENSE_KEY_POINTS, "reward_mode": "dense"})),
        judge_resolver=lambda g, e, s, kp=None, rm="dense": JudgeOutcome(passed=False, score=0.5, satisfied=2, total=4),
    )
    env.reset(options={"task_id": R03_TASK_ID})
    obs, reward, terminated, _truncated, _info = env.step(
        {"tool": "submit_final_answer", "arguments": {"answer": "covers only two problems"}}
    )
    assert reward == 0.5 and terminated is True
    assert obs["payload"]["message"] == "judge verdict: fail"
    assert obs["payload"]["reward"] == 0.5
    assert "2/4" not in obs["payload"]["message"]
    assert "/" not in obs["payload"]["message"]


def test_dense_submission_wrong_answer_near_zero(pg_url):
    env = DBArenaEnv(
        _SingleTaskCatalog(_judged_task(pg_url, judge={"key_points": DENSE_KEY_POINTS, "reward_mode": "dense"})),
        judge_resolver=lambda g, e, s, kp=None, rm="dense": JudgeOutcome(passed=False, score=0.0, satisfied=0, total=4),
    )
    env.reset(options={"task_id": R03_TASK_ID})
    obs, reward, terminated, _truncated, _info = env.step(
        {"tool": "submit_final_answer", "arguments": {"answer": "completely wrong"}}
    )
    assert reward == 0.0 and terminated is True
    assert obs["payload"]["message"] == "judge verdict: fail"
    assert obs["reward"] == 0.0


def test_sparse_submission_keeps_binary_reward(pg_url):
    calls = []

    def resolver(goal, expected, submitted, key_points=None, reward_mode="sparse"):
        calls.append(reward_mode)
        return JudgeOutcome(passed=True)

    env = DBArenaEnv(_SingleTaskCatalog(_judged_task(pg_url)), judge_resolver=resolver)
    env.reset(options={"task_id": R03_TASK_ID})
    obs, reward, terminated, _truncated, _info = env.step(
        {"tool": "submit_final_answer", "arguments": {"answer": EXPECTED_ANSWER}}
    )
    assert reward == 1.0 and terminated is True
    assert calls == ["sparse"]
    assert "pass" in obs["payload"]["message"].lower()
