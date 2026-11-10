from dbarena.catalog.matching import match_tuples, normalize_cell
from dbarena.models.terminal import MatchingRules

NO_TOLERANCE = MatchingRules()
TOLERANT = MatchingRules(numeric_tolerance=0.01)
ORDERED = MatchingRules(order_sensitive=True)


class TestNormalizeCell:
    def test_numbers_and_numeric_strings_converge(self):
        assert normalize_cell(42) == normalize_cell("42")
        assert normalize_cell(42.0) == normalize_cell("42.0")
        assert normalize_cell(" 7.25 ") == normalize_cell("7.25")

    def test_non_numeric_strings_stay_strings(self):
        assert normalize_cell("alpha") == "alpha"
        assert normalize_cell("1e") == "1e"

    def test_none_and_bool_untouched(self):
        assert normalize_cell(None) is None
        assert normalize_cell(True) is True


class TestMultisetMatching:
    def test_exact_match_order_insensitive(self):
        matched, _ = match_tuples([[2, "b"], [1, "a"]], [[1, "a"], [2, "b"]], NO_TOLERANCE)
        assert matched is True

    def test_row_count_mismatch_message(self):
        matched, message = match_tuples([[1]], [[1], [2]], NO_TOLERANCE)
        assert matched is False
        assert "Expected 2 tuple(s), submitted 1." in message

    def test_value_mismatch(self):
        matched, message = match_tuples([[1, "x"]], [[1, "y"]], NO_TOLERANCE)
        assert matched is False
        assert "do not match" in message

    def test_numeric_string_equivalence(self):
        matched, _ = match_tuples([["42"]], [[42]], NO_TOLERANCE)
        assert matched is True

    def test_duplicates_require_equal_multiplicity(self):
        matched, _ = match_tuples([[1]], [[1], [1]], NO_TOLERANCE)
        assert matched is False

        matched, _ = match_tuples([[1], [1]], [[1], [1]], NO_TOLERANCE)
        assert matched is True

    def test_ties_resolved_independently(self):
        matched, _ = match_tuples([[1], [1], [2]], [[2], [1], [1]], NO_TOLERANCE)
        assert matched is True


class TestOrderSensitiveMatching:
    def test_reordered_rows_fail(self):
        matched, message = match_tuples([[2], [1]], [[1], [2]], ORDERED)
        assert matched is False
        assert "order-sensitive" in message

    def test_same_order_passes(self):
        matched, _ = match_tuples([[1], [2]], [[1], [2]], ORDERED)
        assert matched is True


class TestNumericTolerance:
    def test_within_tolerance_matches(self):
        matched, _ = match_tuples([["42.149"]], [["42.15"]], TOLERANT)
        assert matched is True

    def test_outside_tolerance_fails(self):
        matched, _ = match_tuples([["42.10"]], [["42.15"]], TOLERANT)
        assert matched is False

    def test_tolerance_boundary_inclusive(self):
        matched, _ = match_tuples([["42.14"]], [["42.15"]], TOLERANT)
        assert matched is True

    def test_rounding_percentages(self):
        rules = MatchingRules(numeric_tolerance=0.005)
        matched, _ = match_tuples([["33.336"]], [["33.34"]], rules)
        assert matched is True

    def test_tolerance_does_not_apply_to_strings(self):
        matched, _ = match_tuples([["ALPHA"]], [["ALPHB"]], TOLERANT)
        assert matched is False


class TestNullHandling:
    def test_null_matches_null(self):
        matched, _ = match_tuples([[None, "x"]], [[None, "x"]], NO_TOLERANCE)
        assert matched is True

    def test_null_does_not_match_value(self):
        matched, _ = match_tuples([[None]], [["x"]], NO_TOLERANCE)
        assert matched is False

    def test_null_does_not_match_zero(self):
        matched, _ = match_tuples([[None]], [[0]], NO_TOLERANCE)
        assert matched is False

    def test_literal_null_string_is_not_sql_null(self):
        matched, _ = match_tuples([["NULL"]], [[None]], NO_TOLERANCE)
        assert matched is False


class TestEmptyResultExpectation:
    def test_empty_matches_empty(self):
        matched, message = match_tuples([], [], NO_TOLERANCE)
        assert matched is True
        assert "empty result as expected" in message

    def test_nonempty_vs_empty_fails(self):
        matched, _ = match_tuples([[1]], [], NO_TOLERANCE)
        assert matched is False

    def test_empty_vs_nonempty_fails(self):
        matched, _ = match_tuples([], [[1]], NO_TOLERANCE)
        assert matched is False
