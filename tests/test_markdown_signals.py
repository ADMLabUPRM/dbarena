from dbarena.models.actions import ActionCall
from dbarena.models.observations import (
    EpisodeResultPayload,
    ErrorCode,
    ErrorPayload,
    Observation,
    ObservationType,
    QueryResultPayload,
)
from dbarena.observations.markdown import to_markdown

SQL_ACTION = ActionCall(tool="run_sql", arguments={"sql": "SELECT 1"})


def _query_obs(**signals) -> Observation:
    return Observation(
        type=ObservationType.QUERY_RESULT,
        payload=QueryResultPayload(columns=["k"], rows=[[1]], row_count=1, complete=True),
        action=SQL_ACTION,
        **signals,
    )


class TestStepSignals:
    def test_step_line_appended_when_step_number_present(self):
        obs = _query_obs(step_number=3, reward=0.0, terminated=False, truncated=False)
        md = to_markdown(obs)
        assert md.endswith("Step 3 | reward 0.0 | episode continues")

    def test_no_step_line_without_step_number(self):
        obs = _query_obs()
        assert "Step " not in to_markdown(obs)
        assert "reward" not in to_markdown(obs)

    def test_truncated_status(self):
        obs = _query_obs(step_number=4, reward=0.0, terminated=False, truncated=True)
        assert to_markdown(obs).endswith("Step 4 | reward 0.0 | truncated")

    def test_terminated_status(self):
        obs = _query_obs(step_number=5, reward=1.0, terminated=True, truncated=False)
        assert to_markdown(obs).endswith("Step 5 | reward 1.0 | terminated")

    def test_reward_none_renders_dash(self):
        obs = _query_obs(step_number=0, reward=None, terminated=False, truncated=False)
        assert to_markdown(obs).endswith("Step 0 | reward - | episode continues")

    def test_episode_result_does_not_duplicate_signal(self):
        obs = Observation(
            type=ObservationType.EPISODE_RESULT,
            payload=EpisodeResultPayload(matched=True, message="ok", reward=1.0),
            action=ActionCall(tool="submit_final_answer"),
            step_number=2,
            reward=1.0,
            terminated=True,
        )
        md = to_markdown(obs)
        assert md.lower().count("reward") == 1
        assert "Step 2" not in md

    def test_error_observation_carries_signal(self):
        obs = Observation(
            type=ObservationType.ERROR,
            payload=ErrorPayload(code=ErrorCode.TIMEOUT, message="too slow"),
            action=SQL_ACTION,
            step_number=2,
            reward=0.0,
        )
        assert to_markdown(obs).endswith("Step 2 | reward 0.0 | episode continues")
