import datetime
import uuid
from decimal import Decimal
from typing import Any

FALLBACK_ENCODING = "repr"


def jsonify_value(value: Any) -> tuple[Any, str | None]:
    if value is None or isinstance(value, (bool, int, str, float)):
        return value, None
    if isinstance(value, Decimal):
        return str(value), None
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat(), None
    if isinstance(value, uuid.UUID):
        return str(value), None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex(), None
    return repr(value), FALLBACK_ENCODING


def jsonify_row(row: list[Any] | tuple[Any, ...]) -> tuple[list[Any], str | None]:
    values: list[Any] = []
    fallback_used = False
    for cell in row:
        converted, encoding = jsonify_value(cell)
        values.append(converted)
        if encoding is not None:
            fallback_used = True
    return values, (FALLBACK_ENCODING if fallback_used else None)
