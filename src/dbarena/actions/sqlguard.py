import re

from dbarena.actions.errors import QueryError

_TRANSACTION_CONTROL_SQL = (
    r"^\s*(COMMIT|END|BEGIN|START\s+TRANSACTION|SAVEPOINT|RELEASE|ROLLBACK|ABORT|PREPARE\s+TRANSACTION)\b"
)


def _strip_literals_and_comments(sql: str) -> str:
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(quote + quote)
        elif sql[i : i + 2] == "--":
            while i < n and sql[i] != "\n":
                i += 1
        elif sql[i : i + 2] == "/*":
            i += 2
            while i < n and sql[i : i + 2] != "*/":
                i += 1
            i = min(i + 2, n)
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def assert_no_transaction_control(sql: str) -> None:
    pattern = re.compile(_TRANSACTION_CONTROL_SQL, re.IGNORECASE)
    for statement in _strip_literals_and_comments(sql).split(";"):
        if pattern.match(statement):
            raise QueryError("transaction control statements are not allowed")
