from dbarena.models.actions import ActionCall
from dbarena.models.observations import ColumnInfo, ErrorCode, TableDescription, TableSummary
from dbarena.observations.builders import (
    episode_result_observation,
    error_observation,
    query_result_observation,
    schema_description_observation,
    table_description_observation,
)
from dbarena.observations.values import jsonify_row, jsonify_value

SQL_ACTION = ActionCall(tool="run_sql", arguments={"sql": "SELECT 1"})


class TestJsonifyValue:
    def test_primitives_pass_through(self):
        for value in (None, True, 3, 2.5, "text"):
            converted, encoding = jsonify_value(value)
            assert converted == value
            assert encoding is None

    def test_decimal_becomes_string(self):
        from decimal import Decimal

        converted, encoding = jsonify_value(Decimal("1000.50"))
        assert converted == "1000.50"
        assert encoding is None

    def test_datetime_becomes_isoformat(self):
        import datetime

        dt = datetime.datetime(1995, 9, 1, 12, 30, 45)
        converted, encoding = jsonify_value(dt)
        assert converted == "1995-09-01T12:30:45"
        assert encoding is None

    def test_date_becomes_isoformat(self):
        import datetime

        converted, _ = jsonify_value(datetime.date(1995, 9, 1))
        assert converted == "1995-09-01"

    def test_bytes_become_hex(self):
        converted, encoding = jsonify_value(b"\x00\xab")
        assert converted == "00ab"
        assert encoding is None

    def test_unknown_becomes_repr_with_note(self):
        class Mystery:
            def __repr__(self):
                return "Mystery()"

        converted, encoding = jsonify_value(Mystery())
        assert converted == "Mystery()"
        assert encoding == "repr"


class TestJsonifyRow:
    def test_mixed_row(self):
        import datetime
        from decimal import Decimal

        row, note = jsonify_row([1, Decimal("2.5"), datetime.date(1995, 9, 1)])
        assert row == [1, "2.5", "1995-09-01"]
        assert note is None

    def test_fallback_note_propagates(self):
        class Mystery:
            def __repr__(self):
                return "Mystery()"

        row, note = jsonify_row([Mystery()])
        assert row == ["Mystery()"]
        assert note == "repr"


class TestQueryResultObservation:
    def test_complete_result(self):
        obs, note = query_result_observation(SQL_ACTION, ["a", "b"], [[1, "x"], [2, "y"]], row_limit=50)
        assert obs.type.value == "query_result"
        payload = obs.payload
        assert payload.columns == ["a", "b"]
        assert payload.rows == [[1, "x"], [2, "y"]]
        assert payload.row_count == 2
        assert payload.complete is True
        assert payload.row_limit == 50
        assert payload.total_row_count == 2
        assert "truncated" not in obs.meta
        assert note is None

    def test_truncated_result_is_explicit(self):
        rows = [[i] for i in range(120)]
        obs, _ = query_result_observation(SQL_ACTION, ["k"], rows, row_limit=50)
        payload = obs.payload
        assert payload.row_count == 50
        assert payload.complete is False
        assert payload.row_limit == 50
        assert payload.total_row_count is None
        assert obs.meta["truncated"] is True

    def test_exactly_at_limit_is_complete(self):
        rows = [[i] for i in range(50)]
        obs, _ = query_result_observation(SQL_ACTION, ["k"], rows, row_limit=50)
        assert obs.payload.complete is True
        assert "truncated" not in obs.meta

    def test_values_are_jsonified(self):
        import datetime
        from decimal import Decimal

        obs, _ = query_result_observation(
            SQL_ACTION,
            ["d", "amt"],
            [[datetime.date(1995, 9, 1), Decimal("7.25")]],
            row_limit=10,
        )
        assert obs.payload.rows == [["1995-09-01", "7.25"]]

    def test_meta_duration(self):
        obs, _ = query_result_observation(SQL_ACTION, ["a"], [[1]], row_limit=10, duration_ms=7.5)
        assert obs.meta["duration_ms"] == 7.5

    def test_envelope_round_trip(self):
        from dbarena.models.observations import Observation

        obs, _ = query_result_observation(SQL_ACTION, ["a"], [[1]], row_limit=10, duration_ms=1.0)
        assert Observation.from_dict(obs.to_dict()) == obs


class TestTableDescriptionObservation:
    def test_mixed_found_and_missing(self):
        tables = [
            TableDescription(
                name="customer",
                columns=[ColumnInfo(name="c_custkey", type="integer", nullable=False, primary_key=True)],
            ),
            TableDescription(name="nope", columns=[], found=False),
        ]
        obs = table_description_observation(SQL_ACTION, tables)
        assert obs.type.value == "table_description"
        assert obs.payload.tables[0].found is True
        assert obs.payload.tables[1].found is False
        assert obs.payload.tables[1].columns == []


class TestSchemaDescriptionObservation:
    def test_lists_tables(self):
        tables = [
            TableSummary(name="nation", column_count=4, columns=["n_nationkey", "n_name", "n_regionkey", "n_comment"]),
            TableSummary(name="region", column_count=3, columns=["r_regionkey", "r_name", "r_comment"]),
        ]
        obs = schema_description_observation(SQL_ACTION, tables)
        assert obs.type.value == "schema_description"
        assert len(obs.payload.tables) == 2
        assert obs.payload.tables[0].column_count == 4


class TestErrorObservation:
    def test_each_error_code(self):
        for code in ErrorCode:
            obs = error_observation(SQL_ACTION, code, "message for " + code.value)
            assert obs.type.value == "error"
            assert obs.payload.code == code
            assert obs.payload.message == "message for " + code.value

    def test_error_without_action(self):
        obs = error_observation(None, ErrorCode.INTERNAL, "boom")
        assert obs.action is None


class TestEpisodeResultObservation:
    def test_success_and_failure(self):
        win = episode_result_observation(SQL_ACTION, True, "answer matches", 1.0)
        lose = episode_result_observation(SQL_ACTION, False, "submitted answer does not match", 0.0)
        assert win.payload.matched is True and win.payload.reward == 1.0
        assert lose.payload.matched is False and lose.payload.reward == 0.0
