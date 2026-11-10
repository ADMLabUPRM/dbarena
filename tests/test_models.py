import pytest

from dbarena.models import (
    ActionCall,
    ColumnInfo,
    EpisodeResultPayload,
    ErrorCode,
    ErrorPayload,
    MatchingRules,
    Observation,
    ObservationType,
    QueryResultPayload,
    SchemaDescriptionPayload,
    StateCheck,
    TableDescription,
    TableDescriptionPayload,
    TableSummary,
    TerminalCondition,
    VerificationRule,
)


class TestActionCall:
    def test_round_trip(self):
        call = ActionCall(tool="run_sql", arguments={"sql": "SELECT 1", "limit": 5})
        assert ActionCall.from_dict(call.to_dict()) == call

    def test_defaults(self):
        call = ActionCall(tool="describe_schema")
        assert ActionCall.from_dict(call.to_dict()) == call

    def test_missing_tool_raises(self):
        with pytest.raises(ValueError):
            ActionCall.from_dict({"arguments": {}})


class TestQueryResultPayload:
    def test_round_trip_complete(self):
        payload = QueryResultPayload(
            columns=["l_orderkey", "revenue"],
            rows=[[1, "1000.50"], [2, "2000.00"]],
            row_count=2,
            complete=True,
            row_limit=50,
        )
        restored = QueryResultPayload.from_dict(payload.to_dict())
        assert restored == payload

    def test_round_trip_truncated(self):
        payload = QueryResultPayload(
            columns=["k"], rows=[[1], [2]], row_count=2, complete=False, row_limit=2
        )
        restored = QueryResultPayload.from_dict(payload.to_dict())
        assert restored == payload
        assert restored.complete is False


class TestTableDescriptionPayload:
    def test_round_trip(self):
        payload = TableDescriptionPayload(
            tables=[
                TableDescription(
                    name="customer",
                    columns=[
                        ColumnInfo(name="c_custkey", type="integer", nullable=False, primary_key=True),
                        ColumnInfo(name="c_name", type="character varying(25)", default="NULL"),
                    ],
                ),
                TableDescription(name="missing", columns=[], found=False),
            ]
        )
        assert TableDescriptionPayload.from_dict(payload.to_dict()) == payload


class TestSchemaDescriptionPayload:
    def test_round_trip(self):
        payload = SchemaDescriptionPayload(
            tables=[
                TableSummary(
                    name="nation", column_count=4, columns=["n_nationkey", "n_name", "n_regionkey", "n_comment"]
                ),
                TableSummary(name="region", column_count=3, columns=["r_regionkey", "r_name", "r_comment"]),
            ]
        )
        assert SchemaDescriptionPayload.from_dict(payload.to_dict()) == payload


class TestErrorPayload:
    def test_round_trip(self):
        payload = ErrorPayload(code=ErrorCode.TIMEOUT, message="query exceeded 30s")
        assert ErrorPayload.from_dict(payload.to_dict()) == payload

    def test_unknown_code_raises(self):
        with pytest.raises(ValueError):
            ErrorPayload.from_dict({"code": "bogus_code", "message": "x"})


class TestEpisodeResultPayload:
    def test_round_trip(self):
        payload = EpisodeResultPayload(matched=True, message="answer matches", reward=1.0)
        assert EpisodeResultPayload.from_dict(payload.to_dict()) == payload


class TestObservation:
    def _sample(self) -> Observation:
        return Observation(
            type=ObservationType.QUERY_RESULT,
            payload=QueryResultPayload(columns=["a"], rows=[[1]], row_count=1, complete=True),
            action=ActionCall(tool="run_sql", arguments={"sql": "SELECT 1 AS a"}),
            meta={"duration_ms": 12},
        )

    def test_round_trip(self):
        obs = self._sample()
        restored = Observation.from_dict(obs.to_dict())
        assert restored == obs

    def test_round_trip_without_action(self):
        obs = Observation(
            type=ObservationType.SCHEMA_DESCRIPTION,
            payload=SchemaDescriptionPayload(tables=[TableSummary(name="t", column_count=1, columns=["c"])]),
        )
        assert Observation.from_dict(obs.to_dict()) == obs

    def test_round_trip_error(self):
        obs = Observation(
            type=ObservationType.ERROR,
            payload=ErrorPayload(code=ErrorCode.UNSUPPORTED_ENGINE, message="engine 'mysql' has no adapter"),
            action=ActionCall(tool="run_sql"),
        )
        assert Observation.from_dict(obs.to_dict()) == obs

    def test_round_trip_with_task_block(self):
        obs = Observation(
            type=ObservationType.QUERY_RESULT,
            payload=QueryResultPayload(columns=["a"], rows=[[1]], row_count=1, complete=True),
            action=ActionCall(tool="run_sql"),
            task={
                "task_id": "t-1",
                "goal": "Answer.",
                "submission": "Submit the answer as tuples.",
                "category": "OLAP",
                "task_type": "IS",
            },
        )
        assert Observation.from_dict(obs.to_dict()) == obs

    def test_task_block_defaults_to_none(self):
        obs = Observation(
            type=ObservationType.ERROR,
            payload=ErrorPayload(code=ErrorCode.INTERNAL, message="boom"),
        )
        data = obs.to_dict()
        assert data["task"] is None
        assert Observation.from_dict(data) == obs

    def test_invalid_task_block_raises(self):
        data = self._sample().to_dict()
        data["task"] = {"task_id": "t"}
        with pytest.raises(ValueError, match="task block"):
            Observation.from_dict(data)

    def test_round_trip_episode_result(self):
        obs = Observation(
            type=ObservationType.EPISODE_RESULT,
            payload=EpisodeResultPayload(matched=False, message="submitted answer does not match", reward=0.0),
            action=ActionCall(tool="submit_final_answer", arguments={"tuples": [[1]]}),
        )
        assert Observation.from_dict(obs.to_dict()) == obs

    def test_wrong_version_raises(self):
        data = self._sample().to_dict()
        data["schema_version"] = "dbarena-observation/v999"
        with pytest.raises(ValueError, match="schema version"):
            Observation.from_dict(data)

    def test_unknown_type_raises(self):
        data = self._sample().to_dict()
        data["type"] = "telepathy"
        with pytest.raises(ValueError, match="observation type"):
            Observation.from_dict(data)


class TestTerminalCondition:
    def test_round_trip(self):
        cond = TerminalCondition(
            expected_tuples=[[1, "1995-09-01"], [2, "1995-09-15"]],
            matching=MatchingRules(order_sensitive=True, numeric_tolerance=0.01),
        )
        assert TerminalCondition.from_dict(cond.to_dict()) == cond

    def test_defaults(self):
        cond = TerminalCondition(expected_tuples=[[1]])
        assert TerminalCondition.from_dict(cond.to_dict()) == cond

    def test_missing_tuples_raises(self):
        with pytest.raises(ValueError, match="expected_tuples"):
            TerminalCondition.from_dict({"matching": {}})

    def test_unsupported_null_handling_raises(self):
        with pytest.raises(ValueError, match="null_handling"):
            MatchingRules.from_dict({"null_handling": "coalesce"})

    def test_state_check_round_trip(self):
        cond = TerminalCondition(
            expected_tuples=[[7]],
            state_checks=(
                StateCheck(
                    sql="SELECT count(*) FROM orders",
                    expected_tuples=[[25]],
                    matching=MatchingRules(numeric_tolerance=0.01),
                ),
                StateCheck(sql="SELECT o_orderstatus FROM orders WHERE o_orderkey = 7", expected_tuples=[["F"]]),
            ),
        )
        restored = TerminalCondition.from_dict(cond.to_dict())
        assert restored == cond
        assert restored.to_dict()["state_checks"][1]["expected_tuples"] == [["F"]]

    def test_state_checks_absent_yields_empty(self):
        cond = TerminalCondition.from_dict({"expected_tuples": [[1]]})
        assert cond.state_checks == ()
        assert "state_checks" not in cond.to_dict()

    def test_state_checks_not_array_raises(self):
        with pytest.raises(ValueError, match="state_checks"):
            TerminalCondition.from_dict({"expected_tuples": [[1]], "state_checks": "nope"})

    def test_state_check_missing_fields_raises(self):
        with pytest.raises(ValueError, match="state check"):
            StateCheck.from_dict({"sql": "SELECT 1"})

    def test_state_check_unsupported_matching_raises(self):
        with pytest.raises(ValueError, match="null_handling"):
            StateCheck.from_dict(
                {"sql": "SELECT 1", "expected_tuples": [[1]], "matching": {"null_handling": "coalesce"}}
            )

    def test_rules_round_trip(self):
        cond = TerminalCondition(
            expected_tuples=[[7]],
            rules=(
                VerificationRule(sql="SELECT o_totalprice = 100 FROM orders WHERE o_orderkey = 7", description="total"),
                VerificationRule(sql="SELECT count(*) = 1 FROM orders"),
            ),
        )
        restored = TerminalCondition.from_dict(cond.to_dict())
        assert restored == cond
        assert restored.to_dict()["rules"][0]["description"] == "total"
        assert "description" not in restored.to_dict()["rules"][1]

    def test_rules_absent_yields_empty(self):
        cond = TerminalCondition.from_dict({"expected_tuples": [[1]]})
        assert cond.rules == ()
        assert "rules" not in cond.to_dict()

    def test_rules_not_array_raises(self):
        with pytest.raises(ValueError, match="rules"):
            TerminalCondition.from_dict({"expected_tuples": [[1]], "rules": "nope"})

    def test_rule_missing_sql_raises(self):
        with pytest.raises(ValueError, match="verification rule"):
            VerificationRule.from_dict({"description": "d"})

    def test_rule_non_string_description_raises(self):
        with pytest.raises(ValueError, match="description"):
            VerificationRule.from_dict({"sql": "SELECT 1", "description": 5})
