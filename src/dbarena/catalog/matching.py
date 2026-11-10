import re
from decimal import Decimal, InvalidOperation
from typing import Any

from dbarena.models.terminal import MatchingRules

_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def normalize_cell(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        text = value.strip()
        if _NUMBER_RE.match(text):
            try:
                return Decimal(text)
            except InvalidOperation:
                return value
        return value
    return str(value)


def _cells_equal(a: Any, b: Any, rules: MatchingRules) -> bool:
    if a is None or b is None:
        return a is None and b is None
    na, nb = normalize_cell(a), normalize_cell(b)
    if isinstance(na, Decimal) and isinstance(nb, Decimal):
        if rules.numeric_tolerance is not None:
            return abs(na - nb) <= Decimal(str(rules.numeric_tolerance))
        return na == nb
    if isinstance(na, Decimal) != isinstance(nb, Decimal):
        return False
    return na == nb


def _rows_equal(a: list[Any], b: list[Any], rules: MatchingRules) -> bool:
    if len(a) != len(b):
        return False
    return all(_cells_equal(x, y, rules) for x, y in zip(a, b, strict=True))


def match_tuples(
    submitted: list[list[Any]],
    expected: list[list[Any]],
    rules: MatchingRules,
) -> tuple[bool, str]:
    if len(submitted) != len(expected):
        return False, (
            f"Expected {len(expected)} tuple(s), submitted {len(submitted)}."
        )
    if not expected:
        return True, "Submitted an empty result as expected."

    if rules.order_sensitive:
        for index, (got, want) in enumerate(zip(submitted, expected, strict=True)):
            if not _rows_equal(got, want, rules):
                return False, f"Tuple {index} does not match the expected value (order-sensitive comparison)."
        return True, "All tuples match in order."

    unmatched = list(expected)
    for got in submitted:
        for index, want in enumerate(unmatched):
            if _rows_equal(got, want, rules):
                del unmatched[index]
                break
        else:
            return False, "Submitted tuples do not match the expected set."
    return True, "All expected tuples were matched."