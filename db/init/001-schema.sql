CREATE TABLE IF NOT EXISTS tasks (
    task_id text PRIMARY KEY,
    goal text NOT NULL,
    submission text NOT NULL,
    category text NOT NULL,
    task_type text NOT NULL,
    max_steps integer NOT NULL,
    sqlalchemy_url text NOT NULL,
    terminal_condition jsonb NOT NULL,
    query_timeout integer NOT NULL,
    row_limit integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tasks_goal_not_empty CHECK (length(trim(goal)) > 0),
    CONSTRAINT tasks_submission_not_empty CHECK (length(trim(submission)) > 0),
    CONSTRAINT tasks_category_valid CHECK (category IN ('OLTP', 'OLAP', 'ANALYTICS', 'RAG')),
    CONSTRAINT tasks_task_type_valid CHECK (task_type IN ('NAV', 'IS', 'CC', 'MIX')),
    CONSTRAINT tasks_max_steps_positive CHECK (max_steps > 0),
    CONSTRAINT tasks_query_timeout_positive CHECK (query_timeout > 0),
    CONSTRAINT tasks_row_limit_positive CHECK (row_limit IS NULL OR row_limit > 0),
    CONSTRAINT tasks_url_not_empty CHECK (length(trim(sqlalchemy_url)) > 0),
    CONSTRAINT tasks_terminal_condition_shape CHECK (
        jsonb_typeof(terminal_condition) = 'object'
        AND (
            (
                terminal_condition ? 'expected_tuples'
                AND jsonb_typeof(terminal_condition -> 'expected_tuples') = 'array'
            )
            OR (
                terminal_condition ? 'judge'
                AND jsonb_typeof(terminal_condition -> 'judge') = 'object'
                AND jsonb_typeof(terminal_condition -> 'judge' -> 'expected') = 'string'
                AND length(terminal_condition -> 'judge' ->> 'expected') > 0
            )
        )
        AND (
            NOT (terminal_condition ? 'state_checks')
            OR (
                jsonb_typeof(terminal_condition -> 'state_checks') = 'array'
                AND NOT jsonb_path_exists(
                    terminal_condition -> 'state_checks',
                    (
                        '$[*] ? (!(@.type() == "object")'
                        || ' || !(@.sql.type() == "string") || @.sql == ""'
                        || ' || !(@.expected_tuples.type() == "array"))'
                    )::jsonpath
                )
            )
        )
        AND (
            NOT (terminal_condition ? 'rules')
            OR (
                jsonb_typeof(terminal_condition -> 'rules') = 'array'
                AND NOT jsonb_path_exists(
                    terminal_condition -> 'rules',
                    (
                        '$[*] ? (!(@.type() == "object")'
                        || ' || !(@.sql.type() == "string") || @.sql == ""'
                        || ' || (@."description" != null && !(@."description".type() == "string")))'
                    )::jsonpath
                )
            )
        )
    )
);

CREATE TABLE IF NOT EXISTS episodes (
    episode_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id text NOT NULL,
    task_snapshot jsonb NOT NULL,
    status text NOT NULL DEFAULT 'running',
    reward numeric NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    CONSTRAINT episodes_status_valid CHECK (status IN ('running', 'submitted', 'truncated', 'failed', 'abandoned')),
    CONSTRAINT episodes_reward_range CHECK (reward >= 0 AND reward <= 1)
);

CREATE TABLE IF NOT EXISTS episode_steps (
    episode_id uuid NOT NULL REFERENCES episodes (episode_id) ON DELETE CASCADE,
    step_number integer NOT NULL,
    action jsonb NOT NULL,
    observation jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (episode_id, step_number),
    CONSTRAINT episode_steps_step_nonnegative CHECK (step_number >= 0)
);

CREATE INDEX IF NOT EXISTS episodes_task_id_idx ON episodes (task_id, started_at);
