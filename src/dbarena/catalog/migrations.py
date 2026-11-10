from sqlalchemy import Engine, text

CATEGORIES = ("OLTP", "OLAP", "ANALYTICS", "RAG")
TASK_TYPES = ("NAV", "IS", "CC", "MIX")

_CHECK_CONSTRAINTS = {
    "tasks_task_type_valid": "task_type IN ('NAV', 'IS', 'CC', 'MIX')",
}

# Definitions that may evolve between releases: the constraint is dropped and
# re-added on every startup so existing databases adopt the current shape.
_REAPPLIED_CHECK_CONSTRAINTS = {
    "tasks_category_valid": ("tasks", "category IN ('OLTP', 'OLAP', 'ANALYTICS', 'RAG')"),
    "tasks_terminal_condition_shape": (
        "tasks",
        (
            "jsonb_typeof(terminal_condition) = 'object' AND ("
            "(terminal_condition ? 'expected_tuples' "
            "AND jsonb_typeof(terminal_condition -> 'expected_tuples') = 'array') "
            "OR (terminal_condition ? 'judge' AND jsonb_typeof(terminal_condition -> 'judge') = 'object' "
            "AND jsonb_typeof(terminal_condition -> 'judge' -> 'expected') = 'string' "
            "AND length(terminal_condition -> 'judge' ->> 'expected') > 0))"
        ),
    ),
    "episodes_reward_range": ("episodes", "reward >= 0 AND reward <= 1"),
    "episodes_status_valid": (
        "episodes",
        "status IN ('running', 'submitted', 'truncated', 'failed', 'abandoned')",
    ),
}


def ensure_schema(engine: Engine) -> None:
    """Idempotent startup migration bringing any tasks table up to the current shape.

    Adds columns that newer tasks require, backfills classification for rows stored
    before those columns existed, and restores NOT NULL/CHECK constraints when absent
    (for example after a column drop/re-add cycle).
    """
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS submission text"))
        conn.execute(text("UPDATE tasks SET submission = '' WHERE submission IS NULL"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS category text"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_type text"))
        # Drop the evolving constraints up front: the classification rewrites below
        # must not run against the legacy category check.
        for name, (table, _definition) in _REAPPLIED_CHECK_CONSTRAINTS.items():
            conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))
            if name == "episodes_reward_range":
                conn.execute(text("ALTER TABLE episodes DROP CONSTRAINT IF EXISTS episodes_reward_binary"))
        conn.execute(
            text(
                "UPDATE tasks SET category = 'ANALYTICS', task_type = 'IS' "
                "WHERE category IS NULL OR task_type IS NULL"
            )
        )
        conn.execute(
            text("UPDATE tasks SET category = 'ANALYTICS' WHERE category = 'OLAP' AND task_id LIKE 'DBA-A%'")
        )
        conn.execute(text("ALTER TABLE tasks ALTER COLUMN category SET NOT NULL"))
        conn.execute(text("ALTER TABLE tasks ALTER COLUMN task_type SET NOT NULL"))
        for name, definition in _CHECK_CONSTRAINTS.items():
            exists = conn.execute(
                text("SELECT 1 FROM pg_constraint WHERE conrelid = 'tasks'::regclass AND conname = :name"),
                {"name": name},
            ).scalar()
            if not exists:
                conn.execute(text(f"ALTER TABLE tasks ADD CONSTRAINT {name} CHECK ({definition})"))
        for name, (table, definition) in _REAPPLIED_CHECK_CONSTRAINTS.items():
            conn.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({definition})"))


def ensure_embedding_schema(engine: Engine) -> None:
    """Idempotent startup migration for the embedding lifecycle tables.

    Single-row configuration plus the re-embed job history; safe to run on any
    existing admin database (fresh or migrated).
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS embedding_config (
                    id boolean PRIMARY KEY DEFAULT true CHECK (id),
                    base_url text NOT NULL,
                    model text NOT NULL,
                    dim integer NOT NULL,
                    api_key text NOT NULL DEFAULT '',
                    max_model_tokens integer NOT NULL DEFAULT 512,
                    chunk_chars integer NOT NULL DEFAULT 800,
                    chunk_overlap integer NOT NULL DEFAULT 0,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(text("ALTER TABLE embedding_config ADD COLUMN IF NOT EXISTS max_model_tokens integer"))
        conn.execute(text("ALTER TABLE embedding_config ADD COLUMN IF NOT EXISTS chunk_chars integer"))
        conn.execute(text("ALTER TABLE embedding_config ADD COLUMN IF NOT EXISTS chunk_overlap integer"))
        conn.execute(text("UPDATE embedding_config SET max_model_tokens = 512 WHERE max_model_tokens IS NULL"))
        conn.execute(text("UPDATE embedding_config SET chunk_chars = 800 WHERE chunk_chars IS NULL"))
        conn.execute(text("UPDATE embedding_config SET chunk_overlap = 0 WHERE chunk_overlap IS NULL"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS embedding_jobs (
                    job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    state text NOT NULL,
                    fingerprint text NOT NULL,
                    completed integer NOT NULL DEFAULT 0,
                    remaining integer NOT NULL DEFAULT 0,
                    total integer NOT NULL DEFAULT 0,
                    detail text,
                    started_at timestamptz NOT NULL DEFAULT now(),
                    ended_at timestamptz
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS rag_fixture_meta (
                    id boolean PRIMARY KEY DEFAULT true CHECK (id),
                    query_text text NOT NULL,
                    version text NOT NULL,
                    resolved_cutoff date NOT NULL,
                    gold_incident_ids jsonb NOT NULL,
                    r03_question text NOT NULL DEFAULT '',
                    r03_expected_answer text NOT NULL DEFAULT '',
                    r03_gold_incident_ids jsonb NOT NULL DEFAULT '[]'::jsonb
                )
                """
            )
        )
        conn.execute(text("ALTER TABLE rag_fixture_meta ADD COLUMN IF NOT EXISTS r03_question text"))
        conn.execute(text("ALTER TABLE rag_fixture_meta ADD COLUMN IF NOT EXISTS r03_expected_answer text"))
        conn.execute(text("ALTER TABLE rag_fixture_meta ADD COLUMN IF NOT EXISTS r03_gold_incident_ids jsonb"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS rag_variant_tasks (
                    task_id text PRIMARY KEY,
                    kind text NOT NULL,
                    question text NOT NULL,
                    expected_answer text NOT NULL DEFAULT '',
                    gold_incident_ids jsonb NOT NULL,
                    filters jsonb NOT NULL DEFAULT '{}'::jsonb,
                    key_points jsonb NOT NULL DEFAULT '[]'::jsonb,
                    reward_mode text NOT NULL DEFAULT 'sparse'
                )
                """
            )
        )
        conn.execute(text("ALTER TABLE rag_variant_tasks ADD COLUMN IF NOT EXISTS key_points jsonb"))
        conn.execute(text("ALTER TABLE rag_variant_tasks ADD COLUMN IF NOT EXISTS reward_mode text"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS judge_config (
                    id boolean PRIMARY KEY DEFAULT true CHECK (id),
                    base_url text NOT NULL,
                    model text NOT NULL,
                    api_key text NOT NULL DEFAULT '',
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        )
