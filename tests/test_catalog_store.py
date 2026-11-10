from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from dbarena.catalog.errors import DuplicateTaskError, TaskNotFoundError, TaskValidationError
from dbarena.catalog.store import TaskCatalogStore, TaskRecord
from dbarena.models.terminal import MatchingRules, StateCheck, TerminalCondition

REPO_ROOT = Path(__file__).resolve().parents[1]
DDL_PATH = REPO_ROOT / "db" / "init" / "001-schema.sql"


def _task(task_id: str = "DBA-A03-001", **overrides) -> TaskRecord:
    defaults = dict(
        task_id=task_id,
        goal="Report the percentage of discounted shipment revenue from promotional parts.",
        submission="Call submit_final_answer with exactly one tuple: [percentage as a numeric string].",
        category="OLAP",
        task_type="IS",
        max_steps=12,
        sqlalchemy_url="postgresql+psycopg://postgres:testpw@taskdb:5432/taskdb",
        terminal_condition=TerminalCondition(
            expected_tuples=[["42.15"]],
            matching=MatchingRules(numeric_tolerance=0.01),
        ),
        query_timeout=30,
        row_limit=None,
    )
    defaults.update(overrides)
    return TaskRecord(**defaults)


@pytest.fixture(scope="module")
def store(pg_url):
    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS episode_steps, episodes, tasks CASCADE"))
        conn.execute(text(DDL_PATH.read_text(encoding="utf-8")))
    yield TaskCatalogStore(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM episodes"))
        conn.execute(text("DELETE FROM tasks"))
    engine.dispose()


class TestTaskCrud:
    def test_create_and_get(self, store):
        task = _task()
        created = store.create(task)
        assert created == task
        fetched = store.get(task.task_id)
        assert fetched == task

    def test_list_returns_all(self, store):
        store.create(_task("t-list-a"))
        store.create(_task("t-list-b"))
        ids = [t.task_id for t in store.list()]
        assert "t-list-a" in ids and "t-list-b" in ids

    def test_get_missing_returns_none(self, store):
        assert store.get("does-not-exist") is None

    def test_update_existing(self, store):
        store.create(_task("t-update"))
        updated = store.update(_task("t-update", max_steps=99, query_timeout=60))
        fetched = store.get("t-update")
        assert fetched.max_steps == 99
        assert fetched.query_timeout == 60
        assert updated == fetched

    def test_update_missing_raises(self, store):
        with pytest.raises(TaskNotFoundError):
            store.update(_task("t-update-missing"))

    def test_delete(self, store):
        store.create(_task("t-delete"))
        assert store.delete("t-delete") is True
        assert store.get("t-delete") is None
        assert store.delete("t-delete") is False

    def test_duplicate_rejected(self, store):
        store.create(_task("t-dup"))
        with pytest.raises(DuplicateTaskError):
            store.create(_task("t-dup"))

    def test_analytics_category_round_trips(self, store):
        task = _task("t-analytics", category="ANALYTICS")
        created = store.create(task)
        assert created == task
        fetched = store.get("t-analytics")
        assert fetched.category == "ANALYTICS"
        listed = [t.task_id for t in store.list(category="ANALYTICS")]
        assert "t-analytics" in listed
        updated = store.update(_task("t-analytics", category="ANALYTICS", max_steps=20))
        assert updated.category == "ANALYTICS"
        assert store.get("t-analytics").max_steps == 20


class TestDatabaseConstraints:
    def test_negative_max_steps_rejected_by_validation(self, store):
        with pytest.raises(TaskValidationError):
            store.create(_task("t-bad-steps", max_steps=0))

    def test_empty_goal_rejected_by_validation(self, store):
        with pytest.raises(TaskValidationError):
            store.create(_task("t-bad-goal", goal="   "))

    def test_list_filters_by_classification(self, store):
        store.create(_task("t-filter-a", category="OLAP", task_type="IS"))
        store.create(_task("t-filter-b", category="OLTP", task_type="CC"))
        olap_ids = [t.task_id for t in store.list(category="OLAP")]
        assert "t-filter-a" in olap_ids
        assert "t-filter-b" not in olap_ids
        both = store.list(category="OLTP", task_type="CC")
        assert [t.task_id for t in both] == ["t-filter-b"]
        everything = store.list()
        assert {t.task_id for t in everything} >= {"t-filter-a", "t-filter-b"}

    def test_database_checks_reject_raw_inserts(self, store):
        with pytest.raises(IntegrityError):
            with store.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                           max_steps, sqlalchemy_url,
                                           terminal_condition, query_timeout)
                        VALUES ('t-bad-steps-db', 'g', 'submit the answer', 'OLAP', 'IS', 0,
                                'postgresql://u:p@h/db',
                                '{"expected_tuples": []}', 10)
                        """
                    )
                )

    def test_invalid_category_rejected_by_check(self, store):
        with pytest.raises(IntegrityError):
            with store.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                           max_steps, sqlalchemy_url,
                                           terminal_condition, query_timeout)
                        VALUES ('t-bad-category-db', 'g', 'submit the answer', 'oltp', 'IS', 5,
                                'postgresql://u:p@h/db',
                                '{"expected_tuples": []}', 10)
                        """
                    )
                )

    def test_missing_submission_rejected_by_check(self, store):
        with pytest.raises(IntegrityError):
            with store.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                           max_steps, sqlalchemy_url,
                                           terminal_condition, query_timeout)
                        VALUES ('t-bad-submission-db', 'g', '   ', 'OLAP', 'IS', 5,
                                'postgresql://u:p@h/db',
                                '{"expected_tuples": []}', 10)
                        """
                    )
                )

    def test_terminal_condition_without_tuples_rejected(self, store):
        with pytest.raises(IntegrityError):
            with store.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                           max_steps, sqlalchemy_url,
                                           terminal_condition, query_timeout)
                        VALUES ('t-bad-terminal', 'g', 'submit the answer', 'OLAP', 'IS', 5,
                                'postgresql+psycopg://u:p@h/db',
                                '{"matching": {"order_sensitive": false}}', 10)
                        """
                    )
                )

    def test_well_formed_state_checks_accepted_by_check(self, store):
        with store.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                       max_steps, sqlalchemy_url,
                                       terminal_condition, query_timeout)
                    VALUES ('t-ok-state-checks', 'g', 'submit the answer', 'OLTP', 'CC', 5,
                            'postgresql://u:p@h/db',
                            '{"expected_tuples": [[1]], "state_checks": ['
                            '{"sql": "SELECT count(*) FROM orders", "expected_tuples": [[25]]},'
                            '{"sql": "SELECT 1", "expected_tuples": [], "matching": {"numeric_tolerance": 0.01}}]}',
                            10)
                    """
                )
            )

    def test_well_formed_rules_accepted_by_check(self, store):
        with store.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                       max_steps, sqlalchemy_url,
                                       terminal_condition, query_timeout)
                    VALUES ('t-ok-rules', 'g', 'submit the answer', 'OLTP', 'CC', 5,
                            'postgresql://u:p@h/db',
                            '{"expected_tuples": [[1]], "rules": ['
                            '{"sql": "SELECT count(*) = 1 FROM orders", "description": "one order"},'
                            '{"sql": "SELECT true"}]}',
                            10)
                    """
                )
            )

    @pytest.mark.parametrize(
        "task_id, condition",
        [
            ("t-bad-rules-not-array", '{"expected_tuples": [[1]], "rules": "nope"}'),
            ("t-bad-rules-not-object", '{"expected_tuples": [[1]], "rules": ["SELECT 1"]}'),
            (
                "t-bad-rules-missing-sql",
                '{"expected_tuples": [[1]], "rules": [{"description": "d"}]}',
            ),
            (
                "t-bad-rules-empty-sql",
                '{"expected_tuples": [[1]], "rules": [{"sql": ""}]}',
            ),
            (
                "t-bad-rules-sql-not-string",
                '{"expected_tuples": [[1]], "rules": [{"sql": 1}]}',
            ),
            (
                "t-bad-rules-description-not-string",
                '{"expected_tuples": [[1]], "rules": [{"sql": "SELECT 1", "description": 5}]}',
            ),
            (
                "t-bad-rules-description-wrong-type",
                '{"expected_tuples": [[1]], "rules": [{"sql": "SELECT 1", "description": true}]}',
            ),
        ],
    )
    def test_malformed_rules_rejected_by_check(self, store, task_id, condition):
        with pytest.raises(IntegrityError):
            with store.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                           max_steps, sqlalchemy_url,
                                           terminal_condition, query_timeout)
                        VALUES (:task_id, 'g', 'submit the answer', 'OLTP', 'CC', 5,
                                'postgresql://u:p@h/db', CAST(:condition AS jsonb), 10)
                        """
                    ),
                    {"task_id": task_id, "condition": condition},
                )

    @pytest.mark.parametrize(
        "task_id, condition",
        [
            ("t-bad-sc-not-array", '{"expected_tuples": [[1]], "state_checks": "nope"}'),
            (
                "t-bad-sc-not-object",
                '{"expected_tuples": [[1]], "state_checks": ["SELECT 1"]}',
            ),
            (
                "t-bad-sc-missing-sql",
                '{"expected_tuples": [[1]], "state_checks": [{"expected_tuples": [[1]]}]}',
            ),
            (
                "t-bad-sc-empty-sql",
                '{"expected_tuples": [[1]], "state_checks": [{"sql": "", "expected_tuples": [[1]]}]}',
            ),
            (
                "t-bad-sc-sql-not-string",
                '{"expected_tuples": [[1]], "state_checks": [{"sql": 1, "expected_tuples": [[1]]}]}',
            ),
            (
                "t-bad-sc-missing-tuples",
                '{"expected_tuples": [[1]], "state_checks": [{"sql": "SELECT 1"}]}',
            ),
            (
                "t-bad-sc-tuples-not-array",
                '{"expected_tuples": [[1]], "state_checks": [{"sql": "SELECT 1", "expected_tuples": "rows"}]}',
            ),
        ],
    )
    def test_malformed_state_checks_rejected_by_check(self, store, task_id, condition):
        with pytest.raises(IntegrityError):
            with store.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                           max_steps, sqlalchemy_url,
                                           terminal_condition, query_timeout)
                        VALUES (:task_id, 'g', 'submit the answer', 'OLTP', 'CC', 5,
                                'postgresql://u:p@h/db', CAST(:condition AS jsonb), 10)
                        """
                    ),
                    {"task_id": task_id, "condition": condition},
                )

    def test_state_check_task_round_trips(self, store):
        checks = (
            StateCheck(
                sql="SELECT count(*) FROM orders",
                expected_tuples=[[25]],
                matching=MatchingRules(numeric_tolerance=0.01),
            ),
            StateCheck(sql="SELECT o_orderstatus FROM orders WHERE o_orderkey = 7", expected_tuples=[["F"]]),
        )
        task = _task(
            "t-state-checks",
            category="OLTP",
            task_type="CC",
            terminal_condition=TerminalCondition(
                expected_tuples=[[7]],
                matching=MatchingRules(),
                state_checks=checks,
            ),
        )
        created = store.create(task)
        assert created == task
        fetched = store.get("t-state-checks")
        assert fetched == task
        assert fetched.terminal_condition.state_checks == checks

        updated = store.update(
            _task(
                "t-state-checks",
                category="OLTP",
                task_type="CC",
                terminal_condition=TerminalCondition(expected_tuples=[[8]], state_checks=checks[:1]),
            )
        )
        assert [check.sql for check in updated.terminal_condition.state_checks] == [checks[0].sql]
        assert store.get("t-state-checks").terminal_condition.state_checks == checks[:1]

    def test_state_check_without_matching_round_trips(self, store):
        condition = TerminalCondition(
            expected_tuples=[[1]],
            state_checks=(StateCheck(sql="SELECT 1", expected_tuples=[[1]]),),
        )
        task = _task("t-state-check-default-matching", terminal_condition=condition)
        store.create(task)
        fetched = store.get("t-state-check-default-matching")
        assert fetched.terminal_condition.state_checks == condition.state_checks
        assert fetched.terminal_condition.state_checks[0].matching == MatchingRules()


class TestStartupMigration:
    def test_backfills_classification_on_pre_change_volumes(self, pg_url):
        from dbarena.catalog.migrations import ensure_schema

        engine = create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS category"))
            conn.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS task_type"))
            conn.execute(
                text(
                    """
                    INSERT INTO tasks (task_id, goal, submission, max_steps, sqlalchemy_url,
                                       terminal_condition, query_timeout)
                    VALUES ('legacy-task', 'legacy goal', 'submit it', 5,
                            'postgresql://u:p@h/db', '{"expected_tuples": [[1]]}', 10)
                    """
                )
            )

        ensure_schema(engine)

        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT category, task_type FROM tasks WHERE task_id = 'legacy-task'")
            ).one()
        assert row.category == "ANALYTICS"
        assert row.task_type == "IS"
        engine.dispose()

    def test_old_volume_adopts_analytics_category_and_reclassifies_seeds(self, pg_url):
        from dbarena.catalog.migrations import ensure_schema

        engine = create_engine(pg_url)
        with engine.begin() as conn:
            # start from an empty table so the legacy three-value constraint can be installed
            conn.execute(text("DELETE FROM episodes"))
            conn.execute(text("DELETE FROM tasks"))
            # simulate a volume created before this change: three-value category constraint
            conn.execute(text("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_category_valid"))
            conn.execute(
                text(
                    "ALTER TABLE tasks ADD CONSTRAINT tasks_category_valid "
                    "CHECK (category IN ('OLTP', 'OLAP', 'RAG'))"
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                       max_steps, sqlalchemy_url, terminal_condition, query_timeout)
                    VALUES ('DBA-A03-001', 'g', 'submit the answer', 'OLAP', 'IS', 5,
                            'postgresql://u:p@h/db', '{"expected_tuples": [[1]]}', 10)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                       max_steps, sqlalchemy_url, terminal_condition, query_timeout)
                    VALUES ('user-task', 'g', 'submit the answer', 'OLAP', 'IS', 5,
                            'postgresql://u:p@h/db', '{"expected_tuples": [[1]]}', 10)
                    """
                )
            )

        ensure_schema(engine)

        with engine.begin() as conn:
            rows = {
                row.task_id: row.category
                for row in conn.execute(text("SELECT task_id, category FROM tasks")).mappings().all()
            }
            definition = conn.execute(
                text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'tasks_category_valid'")
            ).scalar()
            # the evolved constraint accepts ANALYTICS
            conn.execute(
                text(
                    """
                    INSERT INTO tasks (task_id, goal, submission, category, task_type,
                                       max_steps, sqlalchemy_url, terminal_condition, query_timeout)
                    VALUES ('post-migration-analytics', 'g', 'submit the answer', 'ANALYTICS', 'IS', 5,
                            'postgresql://u:p@h/db', '{"expected_tuples": [[1]]}', 10)
                    """
                )
            )
        assert rows["DBA-A03-001"] == "ANALYTICS"
        assert rows["user-task"] == "OLAP"
        assert "ANALYTICS" in definition
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM tasks WHERE task_id IN ('DBA-A03-001', 'user-task', 'post-migration-analytics')")
            )
        engine.dispose()

    def test_old_volume_adopts_abandoned_episode_status(self, pg_url):
        from dbarena.catalog.migrations import ensure_schema

        engine = create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM episode_steps"))
            conn.execute(text("DELETE FROM episodes"))
            # simulate a volume created before this change: four-value status constraint
            conn.execute(text("ALTER TABLE episodes DROP CONSTRAINT IF EXISTS episodes_status_valid"))
            conn.execute(
                text(
                    "ALTER TABLE episodes ADD CONSTRAINT episodes_status_valid "
                    "CHECK (status IN ('running', 'submitted', 'truncated', 'failed'))"
                )
            )

        ensure_schema(engine)

        with engine.begin() as conn:
            definition = conn.execute(
                text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'episodes_status_valid'")
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO episodes (task_id, task_snapshot, status) "
                    "VALUES ('t-migration-abandoned', '{}'::jsonb, 'abandoned')"
                )
            )
            stored = conn.execute(
                text("SELECT status FROM episodes WHERE task_id = 't-migration-abandoned'")
            ).scalar()
            conn.execute(text("DELETE FROM episodes WHERE task_id = 't-migration-abandoned'"))
        assert "abandoned" in definition
        assert stored == "abandoned"
        engine.dispose()


