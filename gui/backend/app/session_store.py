import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text

from dbarena.actions.registry import EngineRegistry, default_registry
from dbarena.env import DBArenaEnv, EpisodeClosedError


@dataclass
class StepRecord:
    step_number: int
    action: dict[str, Any]
    observation: dict[str, Any]
    reward: float
    terminated: bool
    truncated: bool


@dataclass
class SessionState:
    session_id: str
    task_id: str
    env: DBArenaEnv | None
    steps: list[StepRecord] = field(default_factory=list)
    episode_db_id: str | None = None
    read_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "read_only": self.read_only,
            "steps": [
                {
                    "step_number": s.step_number,
                    "action": s.action,
                    "observation": s.observation,
                    "reward": s.reward,
                    "terminated": s.terminated,
                    "truncated": s.truncated,
                }
                for s in self.steps
            ],
        }


class SessionNotFoundError(Exception):
    pass


class SessionNotActiveError(Exception):
    pass


class EmbeddingGateError(Exception):
    """A RAG session was refused because the embedding lifecycle is not ready."""


class EpisodeSessionStore:
    def __init__(
        self,
        catalog: Any,
        registry: EngineRegistry | None = None,
        admin_engine: Engine | None = None,
        embed_resolver: Any = None,
        embed_gate: Any = None,
        judge_resolver: Any = None,
        episode_transaction_factory: Any = None,
    ):
        self.catalog = catalog
        self.registry = registry if registry is not None else default_registry()
        self.admin_engine = admin_engine
        self.embed_resolver = embed_resolver
        self.embed_gate = embed_gate
        self.judge_resolver = judge_resolver
        self.episode_transaction_factory = episode_transaction_factory
        self._sessions: dict[str, SessionState] = {}

    def _check_embed_gate(self, task_id: str) -> None:
        if self.embed_gate is None:
            return
        task = self.catalog.get(task_id)
        if task is None:
            return
        message = self.embed_gate(task)
        if message:
            raise EmbeddingGateError(message)

    def create(self, task_id: str) -> SessionState:
        self._check_embed_gate(task_id)
        env = DBArenaEnv(
            self.catalog,
            registry=self.registry,
            embed_resolver=self.embed_resolver,
            judge_resolver=self.judge_resolver,
            episode_transaction_factory=self.episode_transaction_factory,
        )
        observation, _info = env.reset(options={"task_id": task_id})
        state = SessionState(session_id="", task_id=task_id, env=env)
        state.steps.append(
            StepRecord(
                step_number=0,
                action={"tool": "reset", "arguments": {"task_id": task_id}},
                observation=observation,
                reward=0.0,
                terminated=False,
                truncated=False,
            )
        )
        if self.admin_engine is not None:
            state.episode_db_id = self._insert_episode(task_id, env.task)
            state.session_id = state.episode_db_id
            self._insert_step(state.episode_db_id, state.steps[0])
        else:
            state.session_id = str(uuid.uuid4())
        self._sessions[state.session_id] = state
        return state

    def step(self, session_id: str, tool: str, arguments: dict[str, Any]) -> StepRecord:
        state = self._active(session_id)
        try:
            obs, reward, terminated, truncated, _info = state.env.step({"tool": tool, "arguments": arguments})
        except EpisodeClosedError as exc:
            raise SessionNotActiveError(str(exc)) from exc
        record = StepRecord(
            step_number=state.env.step_number,
            action={"tool": tool, "arguments": arguments},
            observation=obs,
            reward=reward,
            terminated=terminated,
            truncated=truncated,
        )
        state.steps.append(record)
        if self.admin_engine is not None and state.episode_db_id is not None:
            self._insert_step(state.episode_db_id, record)
            if terminated or truncated:
                self._finish_episode(state.episode_db_id, "submitted" if terminated else "truncated", reward)
        return record

    def reset_session(self, session_id: str) -> SessionState:
        state = self._active(session_id)
        self._check_embed_gate(state.task_id)
        observation, _info = state.env.reset(options={"task_id": state.task_id})
        state.steps.clear()
        state.steps.append(
            StepRecord(
                step_number=0,
                action={"tool": "reset", "arguments": {"task_id": state.task_id}},
                observation=observation,
                reward=0.0,
                terminated=False,
                truncated=False,
            )
        )
        if self.admin_engine is not None:
            state.episode_db_id = self._insert_episode(state.task_id, state.env.task)
            self._insert_step(state.episode_db_id, state.steps[0])
        return state

    def get(self, session_id: str) -> SessionState:
        if session_id in self._sessions:
            return self._sessions[session_id]
        restored = self._reconstruct(session_id)
        if restored is None:
            raise SessionNotFoundError(session_id)
        self._sessions[session_id] = restored
        return restored

    def list(self) -> list[dict[str, Any]]:
        if self.admin_engine is None:
            return [s.to_dict() for s in self._sessions.values()]
        with self.admin_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT episode_id, task_id, status, reward, started_at, ended_at "
                    "FROM episodes ORDER BY started_at"
                )
            ).mappings().all()
        return [
            {
                "session_id": str(row["episode_id"]),
                "task_id": row["task_id"],
                "status": row["status"],
                "reward": float(row["reward"]),
                "started_at": row["started_at"].isoformat(),
                "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
            }
            for row in rows
        ]

    def _active(self, session_id: str) -> SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            restored = self._reconstruct(session_id)
            if restored is not None:
                raise SessionNotActiveError(
                    "session history was restored after a restart; it is read-only. Create a new session."
                )
            raise SessionNotFoundError(session_id)
        if state.env is None or state.read_only:
            raise SessionNotActiveError("session is not active")
        return state

    def _reconstruct(self, session_id: str) -> SessionState | None:
        if self.admin_engine is None:
            return None
        with self.admin_engine.connect() as conn:
            episode = conn.execute(
                text("SELECT episode_id, task_id, task_snapshot FROM episodes WHERE episode_id = CAST(:id AS uuid)"),
                {"id": session_id},
            ).mappings().first()
            if episode is None:
                return None
            steps = conn.execute(
                text(
                    "SELECT step_number, action, observation FROM episode_steps "
                    "WHERE episode_id = CAST(:id AS uuid) ORDER BY step_number"
                ),
                {"id": session_id},
            ).all()
        state = SessionState(
            session_id=str(episode["episode_id"]), task_id=episode["task_id"], env=None, read_only=True
        )
        for step_number, action, observation in steps:
            state.steps.append(
                StepRecord(
                    step_number=step_number,
                    action=action,
                    observation=observation,
                    reward=float(observation.get("reward") or 0.0),
                    terminated=bool(observation.get("terminated", False)),
                    truncated=bool(observation.get("truncated", False)),
                )
            )
        return state

    def _insert_episode(self, task_id: str, task: Any) -> str:
        snapshot = {
            "task_id": task.task_id,
            "goal": task.goal,
            "submission": task.submission,
            "category": task.category,
            "task_type": task.task_type,
            "max_steps": task.max_steps,
            "sqlalchemy_url": task.sqlalchemy_url,
            "terminal_condition": task.terminal_condition.to_dict(),
            "query_timeout": task.query_timeout,
            "row_limit": task.row_limit,
        }
        with self.admin_engine.begin() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO episodes (task_id, task_snapshot) "
                    "VALUES (:task_id, CAST(:snapshot AS jsonb)) RETURNING episode_id"
                ),
                {"task_id": task_id, "snapshot": _json(snapshot)},
            ).scalar_one()
        return str(row)

    def _insert_step(self, episode_id: str, record: StepRecord) -> None:
        with self.admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO episode_steps (episode_id, step_number, action, observation) "
                    "VALUES (CAST(:id AS uuid), :step, CAST(:action AS jsonb), CAST(:observation AS jsonb))"
                ),
                {
                    "id": episode_id,
                    "step": record.step_number,
                    "action": _json(record.action),
                    "observation": _json(record.observation),
                },
            )

    def _finish_episode(self, episode_id: str, status: str, reward: float) -> None:
        with self.admin_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE episodes SET status = :status, reward = :reward, ended_at = now() "
                    "WHERE episode_id = CAST(:id AS uuid)"
                ),
                {"status": status, "reward": reward, "id": episode_id},
            )


def mark_orphaned_episodes(engine: Engine) -> None:
    """Mark episodes left ``running`` by a previous backend process as abandoned.

    Runs once at startup, before any session can exist in this process, so every
    ``running`` row at that moment is an orphan of an earlier process.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE episodes SET status = 'abandoned', reward = 0, ended_at = now() "
                "WHERE status = 'running'"
            )
        )


def _json(value: Any) -> str:
    import json

    return json.dumps(value)
