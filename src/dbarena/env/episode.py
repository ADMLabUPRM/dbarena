from sqlalchemy import Connection, Engine, text

IDLE_TRANSACTION_TIMEOUT_SECONDS = 3600


class EpisodeTransaction:
    """A transaction spanning one episode, for every task.

    Opened at reset: every agent statement and state-check query executes on the
    held connection and is visible within the episode, but nothing is committed.
    `rollback()` at episode end restores the target database to its pre-episode
    state; a dropped connection has the same effect because nothing is committed.
    PostgreSQL sessions additionally apply statement and idle-in-transaction
    timeouts to bound the damage of an abandoned session: the server terminates
    it and the episode's writes roll back. Engines without these knobs (DuckDB)
    open a bare transaction and keep the same rollback semantics.
    """

    def __init__(self, engine: Engine, timeout_seconds: float):
        self._connection = engine.connect()
        self._transaction = self._connection.begin()
        self._closed = False
        if engine.dialect.name == "postgresql":
            self._connection.execute(
                text(
                    "SELECT set_config('statement_timeout', :statement, true), "
                    "set_config('idle_in_transaction_session_timeout', :idle, true)"
                ),
                {
                    "statement": str(max(1, int(timeout_seconds * 1000))),
                    "idle": str(IDLE_TRANSACTION_TIMEOUT_SECONDS * 1000),
                },
            )

    @property
    def connection(self) -> Connection:
        return self._connection

    def rollback(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            try:
                self._transaction.rollback()
            except Exception:
                pass  # the connection may already be gone; the server aborts the transaction either way
        finally:
            self._connection.close()
