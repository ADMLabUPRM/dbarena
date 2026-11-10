import argparse
import copy
import os
import sys
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, create_engine, text

from dbarena.catalog.store import TaskCatalogStore
from dbarena.catalog.validation import parse_task_payload

TRUSTED_ORDER_LOOKUP = """
SELECT o.o_entry_d, o.o_carrier_id, o.o_ol_cnt
FROM oorder o
WHERE o.o_w_id = {w_id}
  AND o.o_d_id = {d_id}
  AND o.o_id = {o_id}
"""

TRUSTED_ORDER_LINE_ITEMS = """
SELECT ol.ol_number, ol.ol_i_id, ol.ol_quantity, ol.ol_amount
FROM order_line ol
WHERE ol.ol_w_id = {busiest_w}
  AND ol.ol_d_id = {busiest_d}
  AND ol.ol_o_id = {busiest_o}
ORDER BY ol.ol_number
"""

TRUSTED_LOW_STOCK_COUNT = """
SELECT count(*)
FROM stock
WHERE s_w_id = {w_id}
  AND s_quantity < {threshold}
"""

TRUSTED_ORDER_RECONCILIATION = """
SELECT round(sum(ol.ol_amount), 2) AS stored_revenue,
       round(sum(ol.ol_quantity * i.i_price), 2) AS recomputed_revenue
FROM order_line ol
JOIN item i ON i.i_id = ol.ol_i_id
WHERE ol.ol_w_id = {busiest_w}
  AND ol.ol_d_id = {busiest_d}
  AND ol.ol_o_id = {busiest_o}
GROUP BY ol.ol_w_id, ol.ol_d_id, ol.ol_o_id
"""

TRUSTED_CUSTOMER_LATEST_ORDER = """
SELECT o.o_id, o.o_entry_d
FROM oorder o
WHERE o.o_w_id = {w_id}
  AND o.o_d_id = {d_id}
  AND o.o_c_id = {c_id}
ORDER BY o.o_id DESC
LIMIT 1
"""


def _cell(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _expected_rows(conn, sql: str) -> list[list]:
    return [[_cell(v) for v in row] for row in conn.execute(text(sql)).all()]


def compute_expected_tuples(engine, trusted_sql: str) -> list[list]:
    with engine.connect() as conn:
        return _expected_rows(conn, trusted_sql)


def compute_state_expectations(engine, reference: dict) -> tuple[list[list], list[dict], list[dict]]:
    """Compute a CC task's confirmation and state-check expectations from a reference execution.

    The reference writes run inside a transaction that is always rolled back, so the
    fixture is left untouched; expectations reflect the state the fixture would have
    after a correct agent execution. Verification rules are also executed under the
    reference and must all return their boolean-true verdict — a broken rule fails
    the seed instead of silently shipping an impossible oracle.
    """
    conn = engine.connect()
    trans = conn.begin()
    try:
        for statement in reference["writes"]:
            conn.execute(text(statement))
        confirmation = _expected_rows(conn, reference["confirmation_sql"])
        checks = []
        for check in reference["state_checks"]:
            checks.append(
                {
                    "sql": check["sql"],
                    "expected_tuples": _expected_rows(conn, check["sql"]),
                    "matching": dict(check.get("matching", {})),
                }
            )
        rules = []
        for position, rule in enumerate(reference.get("rules", ()), start=1):
            rows = _expected_rows(conn, rule["sql"])
            passed = len(rows) == 1 and len(rows[0]) == 1 and rows[0][0] is True
            rules.append({**rule, "passed": passed, "position": position})
    finally:
        trans.rollback()
        conn.close()
    return confirmation, checks, rules


def resolve_params(engine, params: dict[str, str] | None) -> dict[str, Any]:
    if not params:
        return {}
    with engine.connect() as conn:
        resolved = {name: conn.execute(text(query)).scalar() for name, query in params.items()}
    missing = sorted(name for name, value in resolved.items() if value is None)
    if missing:
        raise RuntimeError(
            f"could not resolve task parameters {missing}; the underlying fixture looks incomplete. "
            "Re-run the fixture seed (make seed, make seed-olap or make seed-ds) and retry."
        )
    return resolved


RETIRED_TASK_IDS = ("DBA-A05-001", "DBA-A06-001", "DBA-A18-001")

BUSIEST_ORDER_W_SQL = (
    "SELECT ol_w_id FROM order_line GROUP BY ol_w_id, ol_d_id, ol_o_id "
    "ORDER BY count(*) DESC, ol_w_id ASC, ol_d_id ASC, ol_o_id ASC LIMIT 1"
)

BUSIEST_ORDER_D_SQL = (
    "SELECT ol_d_id FROM order_line GROUP BY ol_w_id, ol_d_id, ol_o_id "
    "ORDER BY count(*) DESC, ol_w_id ASC, ol_d_id ASC, ol_o_id ASC LIMIT 1"
)

BUSIEST_ORDER_O_SQL = (
    "SELECT ol_o_id FROM order_line GROUP BY ol_w_id, ol_d_id, ol_o_id "
    "ORDER BY count(*) DESC, ol_w_id ASC, ol_d_id ASC, ol_o_id ASC LIMIT 1"
)

BUSIEST_ORDER_PARAMS = {
    "busiest_w": BUSIEST_ORDER_W_SQL,
    "busiest_d": BUSIEST_ORDER_D_SQL,
    "busiest_o": BUSIEST_ORDER_O_SQL,
}

UNDELIVERED_BUSIEST_W_SQL = (
    "SELECT o.o_w_id FROM oorder o "
    "JOIN order_line ol ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id "
    "WHERE o.o_carrier_id IS NULL GROUP BY o.o_w_id, o.o_d_id, o.o_id "
    "ORDER BY count(*) DESC, o.o_w_id ASC, o.o_d_id ASC, o.o_id ASC LIMIT 1"
)

UNDELIVERED_BUSIEST_D_SQL = (
    "SELECT o.o_d_id FROM oorder o "
    "JOIN order_line ol ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id "
    "WHERE o.o_carrier_id IS NULL GROUP BY o.o_w_id, o.o_d_id, o.o_id "
    "ORDER BY count(*) DESC, o.o_w_id ASC, o.o_d_id ASC, o.o_id ASC LIMIT 1"
)

UNDELIVERED_BUSIEST_O_SQL = (
    "SELECT o.o_id FROM oorder o "
    "JOIN order_line ol ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id "
    "WHERE o.o_carrier_id IS NULL GROUP BY o.o_w_id, o.o_d_id, o.o_id "
    "ORDER BY count(*) DESC, o.o_w_id ASC, o.o_d_id ASC, o.o_id ASC LIMIT 1"
)

UNDELIVERED_BUSIEST_ORDER_PARAMS = {
    "busiest_w": UNDELIVERED_BUSIEST_W_SQL,
    "busiest_d": UNDELIVERED_BUSIEST_D_SQL,
    "busiest_o": UNDELIVERED_BUSIEST_O_SQL,
}

TASK_SEEDS = [
    {
        "payload": {
            "task_id": "DBA-O01-001",
            "goal": (
                "Report the entry date, carrier, and line count of order {o_id} "
                "in warehouse {w_id}, district {d_id}."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[entry_date, carrier, line_count]."
            ),
            "category": "OLTP",
            "task_type": "IS",
            "max_steps": 8,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": TRUSTED_ORDER_LOOKUP,
        "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
        "params": {
            "w_id": "SELECT min(w_id) FROM warehouse",
            "d_id": "SELECT min(d_id) FROM district",
            "o_id": (
                "SELECT min(o_id) FROM oorder "
                "WHERE o_w_id = (SELECT min(w_id) FROM warehouse) "
                "AND o_d_id = (SELECT min(d_id) FROM district)"
            ),
        },
    },
    {
        "payload": {
            "task_id": "DBA-O01-002",
            "goal": (
                "List the order lines of the order that carries the most order lines — order "
                "{busiest_o} in warehouse {busiest_w}, district {busiest_d}: for each line report "
                "its line number, item, quantity, and amount, ordered by line number ascending."
            ),
            "submission": (
                "Call submit_final_answer with one tuple per order line, in the required order: "
                "[line_number, item, quantity, amount as a numeric string with two decimals]."
            ),
            "category": "OLTP",
            "task_type": "IS",
            "max_steps": 10,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": TRUSTED_ORDER_LINE_ITEMS,
        "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
        "params": dict(BUSIEST_ORDER_PARAMS),
    },
    {
        "payload": {
            "task_id": "DBA-O02-001",
            "goal": (
                "How many stock items in warehouse {w_id} have available quantity below {threshold}?"
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: [count]."
            ),
            "category": "OLTP",
            "task_type": "IS",
            "max_steps": 8,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": TRUSTED_LOW_STOCK_COUNT,
        "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
        "params": {
            "w_id": "SELECT min(w_id) FROM warehouse",
            "threshold": "SELECT 50",
        },
    },
    {
        "payload": {
            "task_id": "DBA-O06-001",
            "goal": (
                "For order {busiest_o} in warehouse {busiest_w}, district {busiest_d}, report the "
                "revenue computed from its order lines — the sum of their amounts, rounded to two "
                "decimal places — alongside the recomputed total at current item prices — the sum "
                "of quantity times each item's current retail price, rounded to two decimal places."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[stored_revenue as a numeric string with two decimals, "
                "recomputed_revenue as a numeric string with two decimals]."
            ),
            "category": "OLTP",
            "task_type": "IS",
            "max_steps": 10,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": TRUSTED_ORDER_RECONCILIATION,
        "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
        "params": dict(BUSIEST_ORDER_PARAMS),
    },
    {
        "payload": {
            "task_id": "DBA-O01-003",
            "goal": (
                "Report the order id and entry date of the most recent order placed by customer "
                "{c_id} in warehouse {w_id}, district {d_id}."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[order_id, entry_date]."
            ),
            "category": "OLTP",
            "task_type": "IS",
            "max_steps": 8,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": TRUSTED_CUSTOMER_LATEST_ORDER,
        "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
        "params": {
            "w_id": "SELECT min(w_id) FROM warehouse",
            "d_id": "SELECT min(d_id) FROM district",
            "c_id": (
                "SELECT min(o_c_id) FROM oorder "
                "WHERE o_w_id = (SELECT min(w_id) FROM warehouse) "
                "AND o_d_id = (SELECT min(d_id) FROM district)"
            ),
        },
    },
    {
        "payload": {
            "task_id": "DBA-O09-001",
            "goal": (
                "The purchasing team needs the dataset that pairs each stocked item with the "
                "warehouse that holds it, its available quantity, and the district-specific "
                "distribution strings. Locate that table in the target database and expose its "
                "definition and grain."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: [table_name, column_count as an integer, "
                "row_count as an integer]."
            ),
            "category": "OLTP",
            "task_type": "NAV",
            "max_steps": 10,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": (
            "SELECT t.table_name, "
            "(SELECT count(*) FROM information_schema.columns c "
            " WHERE c.table_name = t.table_name AND c.table_schema = 'public') AS column_count, "
            "s.row_count "
            "FROM information_schema.tables t "
            "JOIN ("
            "  SELECT 'customer' AS table_name, count(*) AS row_count FROM customer"
            "  UNION ALL SELECT 'district', count(*) FROM district"
            "  UNION ALL SELECT 'history', count(*) FROM history"
            "  UNION ALL SELECT 'item', count(*) FROM item"
            "  UNION ALL SELECT 'new_order', count(*) FROM new_order"
            "  UNION ALL SELECT 'oorder', count(*) FROM oorder"
            "  UNION ALL SELECT 'order_line', count(*) FROM order_line"
            "  UNION ALL SELECT 'stock', count(*) FROM stock"
            "  UNION ALL SELECT 'warehouse', count(*) FROM warehouse"
            ") s ON s.table_name = t.table_name "
            "WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
            "AND EXISTS (SELECT 1 FROM information_schema.columns c1 "
            "            WHERE c1.table_name = t.table_name AND c1.column_name = 's_w_id') "
            "AND EXISTS (SELECT 1 FROM information_schema.columns c2 "
            "            WHERE c2.table_name = t.table_name AND c2.column_name = 's_i_id')"
        ),
        "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
        "params": {},
    },
    {
        "payload": {
            "task_id": "DBA-O10-001",
            "goal": (
                "A reconciliation script needs the order-lines dataset: locate the table whose primary "
                "key consists of exactly four columns — a warehouse identifier, a district identifier, "
                "an order identifier, and a line number — and expose the key's definition."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple, in key-column order: "
                "[table_name, first_key_column, second_key_column, third_key_column, fourth_key_column]."
            ),
            "category": "OLTP",
            "task_type": "NAV",
            "max_steps": 12,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": (
            "SELECT tc.table_name, "
            "max(kcu.column_name) FILTER (WHERE kcu.ordinal_position = 1) AS key_col_1, "
            "max(kcu.column_name) FILTER (WHERE kcu.ordinal_position = 2) AS key_col_2, "
            "max(kcu.column_name) FILTER (WHERE kcu.ordinal_position = 3) AS key_col_3, "
            "max(kcu.column_name) FILTER (WHERE kcu.ordinal_position = 4) AS key_col_4 "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema "
            "WHERE tc.table_schema = 'public' AND tc.constraint_type = 'PRIMARY KEY' "
            "GROUP BY tc.table_name, kcu.constraint_name "
            "HAVING count(*) = 4 "
            "AND sum(CASE WHEN kcu.column_name = 'ol_w_id' THEN 1 ELSE 0 END) = 1 "
            "AND sum(CASE WHEN kcu.column_name = 'ol_d_id' THEN 1 ELSE 0 END) = 1 "
            "AND sum(CASE WHEN kcu.column_name = 'ol_o_id' THEN 1 ELSE 0 END) = 1 "
            "AND sum(CASE WHEN kcu.column_name = 'ol_number' THEN 1 ELSE 0 END) = 1"
        ),
        "matching": {"order_sensitive": True, "numeric_tolerance": None, "null_handling": "exact"},
        "params": {},
    },
    {
        "payload": {
            "task_id": "DBA-O11-001",
            "goal": (
                "For a data-quality review, locate the three datasets behind the warehouse supply "
                "chain: the one holding item master data (names and retail prices), the one holding "
                "per-warehouse stock levels and district distribution strings, and the one holding "
                "order line details with delivery dates. Submit the three table names."
            ),
            "submission": (
                "Call submit_final_answer with exactly three tuples, one per table: [table_name]."
            ),
            "category": "OLTP",
            "task_type": "NAV",
            "max_steps": 10,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": (
            "SELECT DISTINCT t.table_name "
            "FROM information_schema.tables t "
            "JOIN information_schema.columns c "
            "  ON c.table_name = t.table_name AND c.table_schema = t.table_schema "
            "WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
            "AND ("
            "  (c.column_name = 'i_name' AND EXISTS (SELECT 1 FROM information_schema.columns c2 "
            "    WHERE c2.table_name = t.table_name AND c2.column_name = 'i_price'))"
            "  OR c.column_name = 's_dist_01'"
            "  OR (c.column_name = 'ol_delivery_d' AND EXISTS (SELECT 1 FROM information_schema.columns c3 "
            "    WHERE c3.table_name = t.table_name AND c3.column_name = 'ol_amount'))"
            ") "
            "ORDER BY t.table_name"
        ),
        "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
        "params": {},
    },
    {
        "payload": {
            "task_id": "DBA-O12-001",
            "goal": (
                "Warehouse control asks for the busiest undelivered order: locate the order with no "
                "carrier assigned that carries the most order lines; if several tie, take the smallest "
                "warehouse id, then district id, then order id. Report it."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[warehouse_id, district_id, order_id, line_count as an integer]."
            ),
            "category": "OLTP",
            "task_type": "MIX",
            "max_steps": 10,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": (
            "SELECT o.o_w_id, o.o_d_id, o.o_id, count(*) AS line_count "
            "FROM oorder o JOIN order_line ol ON ol.ol_w_id = o.o_w_id "
            "AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id "
            "WHERE o.o_carrier_id IS NULL "
            "GROUP BY o.o_w_id, o.o_d_id, o.o_id "
            "ORDER BY count(*) DESC, o.o_w_id ASC, o.o_d_id ASC, o.o_id ASC "
            "LIMIT 1"
        ),
        "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
        "params": {},
    },
]


STATE_SEEDS = [
    {
        "payload": {
            "task_id": "DBA-O03-001",
            "goal": (
                "Place order {o_id} for customer {c_id} in district {d_id} of warehouse {w_id}: create it "
                "dated 2026-10-15 12:00:00 with no carrier assigned, exactly two order lines, all local, "
                "and advance the district's next order id by one. Line 1 is {q1} units of item {i1} and "
                "line 2 is {q2} units of item {i2}, both supplied by the order's warehouse; each line's "
                "amount is quantity times the item's current retail price rounded to two decimals, its "
                "delivery date is null, and its dist info is that item's stock distribution string for "
                "the order's district (s_dist_01 of its stock row)."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: [the order id of the order you created]."
            ),
            "category": "OLTP",
            "task_type": "CC",
            "max_steps": 16,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "w_id": "SELECT min(w_id) FROM warehouse",
            "d_id": "SELECT min(d_id) FROM district",
            "c_id": (
                "SELECT min(c_id) FROM customer "
                "WHERE c_w_id = (SELECT min(w_id) FROM warehouse) "
                "AND c_d_id = (SELECT min(d_id) FROM district)"
            ),
            "i1": "SELECT min(i_id) FROM item",
            "i2": "SELECT min(i_id) + 7 FROM item",
            "q1": "SELECT 5",
            "q2": "SELECT 2",
            "o_id": (
                "SELECT d_next_o_id FROM district "
                "WHERE d_w_id = (SELECT min(w_id) FROM warehouse) "
                "AND d_id = (SELECT min(d_id) FROM district)"
            ),
        },
        "reference": {
            "writes": [
                (
                    "INSERT INTO oorder (o_id, o_d_id, o_w_id, o_c_id, o_entry_d, o_carrier_id, o_ol_cnt, o_all_local) "
                    "VALUES ({o_id}, {d_id}, {w_id}, {c_id}, TIMESTAMP '2026-10-15 12:00:00', NULL, 2, 1)"
                ),
                (
                    "INSERT INTO order_line (ol_w_id, ol_d_id, ol_o_id, ol_number, ol_i_id, ol_supply_w_id, "
                    "ol_delivery_d, ol_quantity, ol_amount, ol_dist_info) "
                    "SELECT {w_id}, {d_id}, {o_id}, 1, {i1}, {w_id}, NULL, {q1}, "
                    "round({q1} * i.i_price, 2), s.s_dist_01 "
                    "FROM item i JOIN stock s ON s.s_i_id = i.i_id "
                    "WHERE i.i_id = {i1} AND s.s_w_id = {w_id}"
                ),
                (
                    "INSERT INTO order_line (ol_w_id, ol_d_id, ol_o_id, ol_number, ol_i_id, ol_supply_w_id, "
                    "ol_delivery_d, ol_quantity, ol_amount, ol_dist_info) "
                    "SELECT {w_id}, {d_id}, {o_id}, 2, {i2}, {w_id}, NULL, {q2}, "
                    "round({q2} * i.i_price, 2), s.s_dist_01 "
                    "FROM item i JOIN stock s ON s.s_i_id = i.i_id "
                    "WHERE i.i_id = {i2} AND s.s_w_id = {w_id}"
                ),
                (
                    "INSERT INTO new_order (no_w_id, no_d_id, no_o_id) "
                    "VALUES ({w_id}, {d_id}, {o_id})"
                ),
                (
                    "UPDATE district SET d_next_o_id = d_next_o_id + 1 "
                    "WHERE d_w_id = {w_id} AND d_id = {d_id}"
                ),
            ],
            "confirmation_sql": (
                "SELECT o_id FROM oorder WHERE o_w_id = {w_id} AND o_d_id = {d_id} AND o_id = {o_id}"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
            "rules": [
                {
                    "description": "exactly one order was created with the required key and header fields",
                    "sql": (
                        "SELECT count(*) = 1 FROM oorder WHERE o_w_id = {w_id} AND o_d_id = {d_id} "
                        "AND o_id = {o_id} AND o_c_id = {c_id} "
                        "AND o_entry_d = TIMESTAMP '2026-10-15 12:00:00' "
                        "AND o_carrier_id IS NULL AND o_ol_cnt = 2 AND o_all_local = 1"
                    ),
                },
                {
                    "description": "exactly two order lines were created",
                    "sql": (
                        "SELECT count(*) = 2 FROM order_line WHERE ol_w_id = {w_id} "
                        "AND ol_d_id = {d_id} AND ol_o_id = {o_id}"
                    ),
                },
                {
                    "description": "line 1: item, quantity, amount, stock dist info, null delivery",
                    "sql": (
                        "SELECT ol_i_id = {i1} AND ol_quantity = {q1} "
                        "AND ol_amount = round({q1} * (SELECT i_price FROM item WHERE i_id = {i1}), 2) "
                        "AND ol_dist_info = (SELECT s_dist_01 FROM stock WHERE s_w_id = {w_id} AND s_i_id = {i1}) "
                        "AND ol_delivery_d IS NULL AND ol_supply_w_id = {w_id} "
                        "FROM order_line WHERE ol_w_id = {w_id} AND ol_d_id = {d_id} "
                        "AND ol_o_id = {o_id} AND ol_number = 1"
                    ),
                },
                {
                    "description": "line 2: item, quantity, amount, stock dist info, null delivery",
                    "sql": (
                        "SELECT ol_i_id = {i2} AND ol_quantity = {q2} "
                        "AND ol_amount = round({q2} * (SELECT i_price FROM item WHERE i_id = {i2}), 2) "
                        "AND ol_dist_info = (SELECT s_dist_01 FROM stock WHERE s_w_id = {w_id} AND s_i_id = {i2}) "
                        "AND ol_delivery_d IS NULL AND ol_supply_w_id = {w_id} "
                        "FROM order_line WHERE ol_w_id = {w_id} AND ol_d_id = {d_id} "
                        "AND ol_o_id = {o_id} AND ol_number = 2"
                    ),
                },
                {
                    "description": "a new_order row was created for the order",
                    "sql": (
                        "SELECT count(*) = 1 FROM new_order WHERE no_w_id = {w_id} "
                        "AND no_d_id = {d_id} AND no_o_id = {o_id}"
                    ),
                },
                {
                    "description": "the district's next order id advanced by one",
                    "sql": (
                        "SELECT d_next_o_id = {o_id} + 1 FROM district "
                        "WHERE d_w_id = {w_id} AND d_id = {d_id}"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": (
                        "SELECT ol_number, ol_i_id, ol_quantity, ol_amount FROM order_line "
                        "WHERE ol_w_id = {w_id} AND ol_d_id = {d_id} AND ol_o_id = {o_id} "
                        "ORDER BY ol_number"
                    ),
                    "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT count(*) FROM oorder",
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT count(*) FROM order_line",
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT count(*) FROM new_order",
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                },
            ],
        },
    },
    {
        "payload": {
            "task_id": "DBA-O04-001",
            "goal": (
                "Customer {c_id} in district {d_id} of warehouse {w_id} just made a payment of 250.00. "
                "Debit the customer's account balance c_balance by exactly 250.00. Do not modify any "
                "other customer or any other table."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: [the customer id whose balance you debited]."
            ),
            "category": "OLTP",
            "task_type": "CC",
            "max_steps": 8,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "w_id": "SELECT min(w_id) FROM warehouse",
            "d_id": "SELECT min(d_id) FROM district",
            "c_id": (
                "SELECT min(c_id) FROM customer "
                "WHERE c_w_id = (SELECT min(w_id) FROM warehouse) "
                "AND c_d_id = (SELECT min(d_id) FROM district)"
            ),
        },
        "reference": {
            "writes": [
                (
                    "UPDATE customer SET c_balance = c_balance - 250.00 "
                    "WHERE c_w_id = {w_id} AND c_d_id = {d_id} AND c_id = {c_id}"
                ),
            ],
            "confirmation_sql": (
                "SELECT c_id FROM customer WHERE c_w_id = {w_id} AND c_d_id = {d_id} AND c_id = {c_id}"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
            "state_checks": [
                {
                    "sql": (
                        "SELECT round(c_balance, 2) FROM customer WHERE c_w_id = {w_id} "
                        "AND c_d_id = {d_id} AND c_id = {c_id}"
                    ),
                    "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT round(sum(c_balance), 2) FROM customer",
                    "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
            ],
        },
    },
    {
        "payload": {
            "task_id": "DBA-O05-001",
            "goal": (
                "Fulfill the oldest undelivered order in district {d_id} of warehouse {w_id}: find the "
                "order with no carrier assigned having the smallest order id, assign it carrier "
                "{carrier_id}, and set every one of its order lines' delivery date to "
                "2026-11-01 12:00:00. Do not modify any other order, order line, or table."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: [the order id of the order you fulfilled]."
            ),
            "category": "OLTP",
            "task_type": "CC",
            "max_steps": 10,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "w_id": "SELECT min(w_id) FROM warehouse",
            "d_id": "SELECT min(d_id) FROM district",
            "o_id": (
                "SELECT min(o_id) FROM oorder "
                "WHERE o_w_id = (SELECT min(w_id) FROM warehouse) "
                "AND o_d_id = (SELECT min(d_id) FROM district) "
                "AND o_carrier_id IS NULL"
            ),
            "carrier_id": "SELECT max(o_carrier_id) FROM oorder WHERE o_carrier_id IS NOT NULL",
        },
        "reference": {
            "writes": [
                (
                    "UPDATE oorder SET o_carrier_id = {carrier_id} "
                    "WHERE o_w_id = {w_id} AND o_d_id = {d_id} AND o_id = {o_id}"
                ),
                (
                    "UPDATE order_line SET ol_delivery_d = TIMESTAMP '2026-11-01 12:00:00' "
                    "WHERE ol_w_id = {w_id} AND ol_d_id = {d_id} AND ol_o_id = {o_id}"
                ),
            ],
            "confirmation_sql": (
                "SELECT o_id FROM oorder WHERE o_w_id = {w_id} AND o_d_id = {d_id} AND o_id = {o_id}"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
            "rules": [
                {
                    "description": "the fulfilled order carries the assigned carrier",
                    "sql": (
                        "SELECT o_carrier_id = {carrier_id} FROM oorder "
                        "WHERE o_w_id = {w_id} AND o_d_id = {d_id} AND o_id = {o_id}"
                    ),
                },
                {
                    "description": "every line of the fulfilled order is stamped with the delivery date",
                    "sql": (
                        "SELECT count(*) = 0 FROM order_line WHERE ol_w_id = {w_id} "
                        "AND ol_d_id = {d_id} AND ol_o_id = {o_id} "
                        "AND (ol_delivery_d IS NULL OR ol_delivery_d <> TIMESTAMP '2026-11-01 12:00:00')"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": (
                        "SELECT count(*) FROM order_line WHERE ol_w_id = {w_id} "
                        "AND ol_d_id = {d_id} AND ol_o_id = {o_id} "
                        "AND ol_delivery_d = TIMESTAMP '2026-11-01 12:00:00'"
                    ),
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                },
                {
                    "sql": (
                        "SELECT count(*) FROM order_line WHERE ol_delivery_d = TIMESTAMP '2026-11-01 12:00:00' "
                        "AND (ol_w_id, ol_d_id, ol_o_id) <> ({w_id}, {d_id}, {o_id})"
                    ),
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                },
            ],
        },
    },
    {
        "payload": {
            "task_id": "DBA-O13-001",
            "goal": (
                "Warehouse control needs the busiest undelivered order prioritized: locate the order "
                "with no carrier assigned that carries the most order lines (if several tie, take the "
                "smallest warehouse id, then district id, then order id), report it, and assign carrier "
                "{carrier_id} to it. Do not modify anything else."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[warehouse_id, district_id, order_id, line_count as an integer]."
            ),
            "category": "OLTP",
            "task_type": "MIX",
            "max_steps": 12,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": dict(
            UNDELIVERED_BUSIEST_ORDER_PARAMS,
            carrier_id="SELECT max(o_carrier_id) FROM oorder WHERE o_carrier_id IS NOT NULL",
        ),
        "reference": {
            "writes": [
                (
                    "UPDATE oorder SET o_carrier_id = {carrier_id} "
                    "WHERE o_w_id = {busiest_w} AND o_d_id = {busiest_d} AND o_id = {busiest_o}"
                ),
            ],
            "confirmation_sql": (
                "SELECT o_w_id, o_d_id, o_id, "
                "(SELECT count(*) FROM order_line WHERE ol_w_id = o.o_w_id "
                "AND ol_d_id = o.o_d_id AND ol_o_id = o.o_id) "
                "FROM oorder o WHERE o_w_id = {busiest_w} AND o_d_id = {busiest_d} AND o_id = {busiest_o}"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
            "state_checks": [
                {
                    "sql": (
                        "SELECT o_carrier_id FROM oorder WHERE o_w_id = {busiest_w} "
                        "AND o_d_id = {busiest_d} AND o_id = {busiest_o}"
                    ),
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT count(*) FROM oorder WHERE o_carrier_id IS NULL",
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT count(*) FROM oorder WHERE o_carrier_id IS NOT NULL",
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                },
            ],
        },
    },
    {
        "payload": {
            "task_id": "DBA-O14-001",
            "goal": (
                "Pricing approved a retail price change: item {i_id}'s retail price becomes "
                "{new_price}. Apply the new price to that item, correct the amount of every order "
                "line of order {busiest_o} in warehouse {busiest_w}, district {busiest_d} that "
                "references item {i_id} to quantity times the new retail price (rounded to two "
                "decimals), and recalculate the order's corrected total as the sum of its line "
                "amounts. Submit the corrected total. Do not modify any other item, order line, or order."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[corrected_total as a numeric string with two decimals]."
            ),
            "category": "OLTP",
            "task_type": "MIX",
            "max_steps": 14,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": dict(
            BUSIEST_ORDER_PARAMS,
            i_id=(
                "SELECT min(ol_i_id) FROM order_line "
                f"WHERE ol_w_id = ({BUSIEST_ORDER_W_SQL}) "
                f"AND ol_d_id = ({BUSIEST_ORDER_D_SQL}) "
                f"AND ol_o_id = ({BUSIEST_ORDER_O_SQL})"
            ),
            new_price="SELECT round((SELECT max(i_price) FROM item) + 50, 2)",
        ),
        "reference": {
            "writes": [
                "UPDATE item SET i_price = {new_price} WHERE i_id = {i_id}",
                (
                    "UPDATE order_line SET ol_amount = round(ol_quantity * {new_price}, 2) "
                    "WHERE ol_w_id = {busiest_w} AND ol_d_id = {busiest_d} AND ol_o_id = {busiest_o} "
                    "AND ol_i_id = {i_id}"
                ),
            ],
            "confirmation_sql": (
                "SELECT round((SELECT sum(ol_amount) FROM order_line WHERE ol_w_id = {busiest_w} "
                "AND ol_d_id = {busiest_d} AND ol_o_id = {busiest_o}), 2)"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
            "rules": [
                {
                    "description": "the item carries exactly the approved new retail price",
                    "sql": (
                        "SELECT count(*) = 1 FROM item WHERE i_id = {i_id} "
                        "AND i_price = {new_price}"
                    ),
                },
                {
                    "description": (
                        "every line of the order is priced at quantity times its item's current retail price"
                    ),
                    "sql": (
                        "SELECT count(*) = 0 FROM order_line ol JOIN item i ON i.i_id = ol.ol_i_id "
                        "WHERE ol.ol_w_id = {busiest_w} AND ol.ol_d_id = {busiest_d} "
                        "AND ol.ol_o_id = {busiest_o} "
                        "AND ol.ol_amount <> round(ol.ol_quantity * i.i_price, 2)"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": "SELECT round(i_price, 2) FROM item WHERE i_id = {i_id}",
                    "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
            ],
        },
    },
]


ANALYTICS_SEEDS = [
    {
        "payload": {
            "task_id": "DBA-A02-001",
            "goal": (
                "Show the ten highest-revenue unshipped orders for the {segment} market segment as of "
                "{cutoff_date}: consider only open orders (status 'O') placed before {cutoff_date} for "
                "customers in that segment, compute each order's revenue as the sum over its line items of "
                "extended price times one minus discount, rank orders by revenue descending, and break ties "
                "by the smaller order key."
            ),
            "submission": (
                "Call submit_final_answer with exactly ten tuples in ranking order: "
                "[order_key, revenue as a numeric string with two decimals, "
                "order_date as YYYY-MM-DD, shipping_priority]."
            ),
            "category": "ANALYTICS",
            "task_type": "IS",
            "max_steps": 12,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": (
            "SELECT l.l_orderkey, "
            "sum(l.l_extendedprice * (1 - l.l_discount)) AS revenue, "
            "o.o_orderdate, o.o_shippriority "
            "FROM customer c "
            "JOIN orders o ON c.c_custkey = o.o_custkey "
            "JOIN lineitem l ON l.l_orderkey = o.o_orderkey "
            "WHERE c.c_mktsegment = '{segment}' "
            "AND o.o_orderstatus = 'O' "
            "AND o.o_orderdate < DATE '{cutoff_date}' "
            "GROUP BY l.l_orderkey, o.o_orderdate, o.o_shippriority "
            "ORDER BY revenue DESC, o.o_orderdate ASC, l.l_orderkey ASC "
            "LIMIT 10"
        ),
        "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
        "params": {
            "segment": "SELECT 'BUILDING'",
            # Q3's canonical 1995-03-15 cutoff leaves ~1 qualifying open order
            # at SF 0.01 (dbgen marks orders open relative to its fixed
            # 1995-06-01 "today"), so the pinned cutoff is mid-July 1995 —
            # ~91 qualifying groups at this fixture, comfortably ranking ten.
            "cutoff_date": "SELECT DATE '1995-07-15'",
        },
        "expected_row_count": 10,
    },
    {
        "payload": {
            "task_id": "DBA-A03-001",
            "goal": (
                "What percentage of discounted shipment revenue came from promotional parts shipped between "
                "{month_start} (inclusive) and {month_end_exclusive} (exclusive)? Treat a part as promotional "
                "when its type begins with PROMO. Use extended price times one minus discount as revenue, and "
                "report the percentage to two decimal places; if there is no qualifying revenue, report 0.00."
            ),
            "submission": (
                'Call submit_final_answer with exactly one tuple: '
                '[percentage as a numeric string with two decimals, e.g. "12.34"].'
            ),
            "category": "ANALYTICS",
            "task_type": "IS",
            "max_steps": 12,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": (
            "SELECT round(100.00 * "
            "sum(CASE WHEN p.p_type LIKE 'PROMO%' "
            "THEN l.l_extendedprice * (1 - l.l_discount) ELSE 0 END) / "
            "sum(l.l_extendedprice * (1 - l.l_discount)), 2) "
            "FROM lineitem l "
            "JOIN part p ON p.p_partkey = l.l_partkey "
            "WHERE l.l_shipdate >= DATE '{month_start}' "
            "AND l.l_shipdate < DATE '{month_end_exclusive}'"
        ),
        "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
        "params": {
            "month_start": "SELECT DATE '1995-09-01'",
            "month_end_exclusive": "SELECT DATE '1995-10-01'",
        },
    },
    {
        "payload": {
            "task_id": "DBA-A04-001",
            "goal": (
                "How much revenue would be recovered by eliminating the discount on shipped line items with "
                "a discount between {discount_low} and {discount_high} (both inclusive), a ship date on or "
                "after {year_start} and before {year_end_exclusive}, and a quantity below {quantity_limit}? "
                "Recovered revenue is the sum of extended price times discount over those line items; report "
                "the total to two decimal places."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[recovered_revenue as a numeric string with two decimals]."
            ),
            "category": "ANALYTICS",
            "task_type": "IS",
            "max_steps": 10,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": (
            "SELECT round(sum(l.l_extendedprice * l.l_discount), 2) "
            "FROM lineitem l "
            "WHERE l.l_shipdate >= DATE '{year_start}' "
            "AND l.l_shipdate < DATE '{year_end_exclusive}' "
            "AND l.l_discount BETWEEN {discount_low} AND {discount_high} "
            "AND l.l_quantity < {quantity_limit}"
        ),
        "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
        "params": {
            "year_start": "SELECT DATE '1994-01-01'",
            "year_end_exclusive": "SELECT DATE '1995-01-01'",
            "discount_low": "SELECT 0.05",
            "discount_high": "SELECT 0.07",
            "quantity_limit": "SELECT 24",
        },
    },
    {
        "payload": {
            "task_id": "DBA-A01-001",
            "goal": (
                "A marketing analysis is starting on this warehouse: locate the dataset that holds the "
                "customer master — each customer's name, account balance, and market segment — and expose "
                "its definition and grain."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: [table_name, column_count as an integer, "
                "row_count as an integer]."
            ),
            "category": "ANALYTICS",
            "task_type": "NAV",
            "max_steps": 10,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": (
            "SELECT t.table_name, "
            "(SELECT count(*) FROM duckdb_columns() c "
            " WHERE c.table_name = t.table_name AND c.schema_name = 'main') AS column_count, "
            "s.row_count "
            "FROM duckdb_tables() t "
            "JOIN ("
            "  SELECT 'customer' AS table_name, count(*) AS row_count FROM customer"
            "  UNION ALL SELECT 'lineitem', count(*) FROM lineitem"
            "  UNION ALL SELECT 'nation', count(*) FROM nation"
            "  UNION ALL SELECT 'orders', count(*) FROM orders"
            "  UNION ALL SELECT 'part', count(*) FROM part"
            "  UNION ALL SELECT 'partsupp', count(*) FROM partsupp"
            "  UNION ALL SELECT 'region', count(*) FROM region"
            "  UNION ALL SELECT 'supplier', count(*) FROM supplier"
            ") s ON s.table_name = t.table_name "
            "WHERE t.schema_name = 'main' "
            "AND EXISTS (SELECT 1 FROM duckdb_columns() c1 "
            "            WHERE c1.table_name = t.table_name AND c1.column_name = 'c_mktsegment') "
            "AND EXISTS (SELECT 1 FROM duckdb_columns() c2 "
            "            WHERE c2.table_name = t.table_name AND c2.column_name = 'c_acctbal')"
        ),
        "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
        "params": {},
    },
    {
        "payload": {
            "task_id": "DBA-A10-001",
            "goal": (
                "For market rollups, locate the two small geography reference datasets in this warehouse: "
                "the one holding the region names, and the one holding each nation with the region it "
                "belongs to. Submit both table names."
            ),
            "submission": (
                "Call submit_final_answer with exactly two tuples, one per table: [table_name]."
            ),
            "category": "ANALYTICS",
            "task_type": "NAV",
            "max_steps": 10,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": (
            "SELECT DISTINCT t.table_name "
            "FROM duckdb_tables() t "
            "JOIN duckdb_columns() c "
            "  ON c.table_name = t.table_name AND c.schema_name = t.schema_name "
            "WHERE t.schema_name = 'main' "
            "AND ("
            "  (c.column_name = 'r_name' AND EXISTS (SELECT 1 FROM duckdb_columns() c2 "
            "    WHERE c2.table_name = t.table_name AND c2.column_name = 'r_comment'))"
            "  OR (c.column_name = 'n_regionkey' AND EXISTS (SELECT 1 FROM duckdb_columns() c3 "
            "    WHERE c3.table_name = t.table_name AND c3.column_name = 'n_name'))"
            ") "
            "ORDER BY t.table_name"
        ),
        "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
        "params": {},
    },
    {
        "payload": {
            "task_id": "DBA-A11-001",
            "goal": (
                "Capacity planning needs the biggest dataset in this warehouse: locate the table with the "
                "most rows and expose its size."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: [table_name, column_count as an integer, "
                "row_count as an integer]."
            ),
            "category": "ANALYTICS",
            "task_type": "NAV",
            "max_steps": 12,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "trusted_sql": (
            "SELECT t.table_name, "
            "(SELECT count(*) FROM duckdb_columns() c "
            " WHERE c.table_name = t.table_name AND c.schema_name = 'main') AS column_count, "
            "s.row_count "
            "FROM duckdb_tables() t "
            "JOIN ("
            "  SELECT 'customer' AS table_name, count(*) AS row_count FROM customer"
            "  UNION ALL SELECT 'lineitem', count(*) FROM lineitem"
            "  UNION ALL SELECT 'nation', count(*) FROM nation"
            "  UNION ALL SELECT 'orders', count(*) FROM orders"
            "  UNION ALL SELECT 'part', count(*) FROM part"
            "  UNION ALL SELECT 'partsupp', count(*) FROM partsupp"
            "  UNION ALL SELECT 'region', count(*) FROM region"
            "  UNION ALL SELECT 'supplier', count(*) FROM supplier"
            ") s ON s.table_name = t.table_name "
            "WHERE t.schema_name = 'main' "
            "ORDER BY s.row_count DESC "
            "LIMIT 1"
        ),
        "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
        "params": {},
    },
]


# Isolation-emulation pilot (change add-isolation-emulation-variant): a copy of the
# new-order task whose terminal condition adds generated table-set and whole-table
# fingerprint checks, so any collateral write — an extra table, a mutated unrelated
# row, a missed update — fails the episode with the existing state-check machinery.
# Built by deep copy: the DBA-O03-001 entry above is never mutated.
ISOLATION_EMULATION_ORDER_KEYS = {
    "warehouse": "w_id",
    "district": "d_w_id, d_id",
    "item": "i_id",
    "stock": "s_w_id, s_i_id",
    "customer": "c_w_id, c_d_id, c_id",
    "history": "h_c_id, h_c_d_id, h_c_w_id, h_d_id, h_w_id, h_date, h_amount, h_data",
    "oorder": "o_w_id, o_d_id, o_id",
    "new_order": "no_w_id, no_d_id, no_o_id",
    "order_line": "ol_w_id, ol_d_id, ol_o_id, ol_number",
}

ISOLATION_EMULATION_SOURCE_ID = "DBA-O03-001"
ISOLATION_EMULATION_VARIANT_ID = "DBA-O03-002"


def _emulation_state_checks() -> list[dict]:
    checks = [
        {
            "sql": (
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            ),
            "matching": {"order_sensitive": True, "numeric_tolerance": None, "null_handling": "exact"},
        }
    ]
    for table, order_key in ISOLATION_EMULATION_ORDER_KEYS.items():
        checks.append(
            {
                "sql": (
                    f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY {order_key}), '')) "
                    f"FROM {table} AS t"
                ),
                "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
            }
        )
    return checks


def _isolation_emulation_variant_seed() -> dict:
    base = next(
        seed for seed in STATE_SEEDS if seed["payload"]["task_id"] == ISOLATION_EMULATION_SOURCE_ID
    )
    variant = copy.deepcopy(base)
    variant["payload"]["task_id"] = ISOLATION_EMULATION_VARIANT_ID
    variant["reference"]["state_checks"] = [
        *base["reference"]["state_checks"],
        *_emulation_state_checks(),
    ]
    return variant


STATE_SEEDS = [*STATE_SEEDS, _isolation_emulation_variant_seed()]


ANALYTICS_STATE_SEEDS = [
    {
        "payload": {
            "task_id": "DBA-A09-001",
            "goal": (
                "Save a monthly shipment revenue summary as table {summary_table}: one row per ship date "
                "with shipments in the window starting {month_start} (inclusive) and ending "
                "{month_end_exclusive} (exclusive), with columns ship_date (DATE) and gross_revenue "
                "(that day's sum of extended price times one minus discount, to two decimals)."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: [number of rows in the saved summary, "
                "total gross_revenue across them as a numeric string with two decimals]."
            ),
            "category": "ANALYTICS",
            "task_type": "CC",
            "max_steps": 16,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "summary_table": "SELECT 'ship_revenue_summary'",
            "month_start": "SELECT DATE '1995-09-01'",
            "month_end_exclusive": "SELECT DATE '1995-10-01'",
        },
        "reference": {
            "writes": [
                (
                    "CREATE TABLE {summary_table} ("
                    "ship_date DATE, gross_revenue DECIMAL(15,2))"
                ),
                (
                    "INSERT INTO {summary_table} "
                    "SELECT l_shipdate, round(sum(l_extendedprice * (1 - l_discount)), 2) "
                    "FROM lineitem "
                    "WHERE l_shipdate >= DATE '{month_start}' "
                    "AND l_shipdate < DATE '{month_end_exclusive}' "
                    "GROUP BY l_shipdate"
                ),
            ],
            "confirmation_sql": (
                "SELECT count(*), round(sum(gross_revenue), 2) FROM {summary_table}"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
            "rules": [
                {
                    "description": "one row per distinct ship date in the pinned window",
                    "sql": (
                        "SELECT (SELECT count(*) FROM {summary_table}) = "
                        "(SELECT count(DISTINCT l_shipdate) FROM lineitem "
                        "WHERE l_shipdate >= DATE '{month_start}' "
                        "AND l_shipdate < DATE '{month_end_exclusive}')"
                    ),
                },
                {
                    "description": "each ship date appears exactly once",
                    "sql": "SELECT count(*) = count(DISTINCT ship_date) FROM {summary_table}",
                },
                {
                    "description": "the saved total equals the fixture's revenue for the window",
                    "sql": (
                        "SELECT round((SELECT sum(gross_revenue) FROM {summary_table}), 2) = "
                        "round((SELECT sum(l_extendedprice * (1 - l_discount)) FROM lineitem "
                        "WHERE l_shipdate >= DATE '{month_start}' "
                        "AND l_shipdate < DATE '{month_end_exclusive}'), 2)"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": "SELECT ship_date, gross_revenue FROM {summary_table} ORDER BY ship_date",
                    "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT count(*) FROM {summary_table}",
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                },
            ],
        },
    },
    {
        "payload": {
            "task_id": "DBA-A12-001",
            "goal": (
                "The sales team wants its high-value list saved: create table {segment_table} holding "
                "every customer whose total order revenue — the sum of extended price times one minus "
                "discount across all their orders' line items — exceeds {threshold}. Columns: c_custkey "
                "(BIGINT) and total_revenue (that sum, to two decimals)."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[number of customers saved]."
            ),
            "category": "ANALYTICS",
            "task_type": "CC",
            "max_steps": 14,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "segment_table": "SELECT 'high_value_customers'",
            "threshold": "SELECT 3000000",
        },
        "reference": {
            "writes": [
                (
                    "CREATE TABLE {segment_table} ("
                    "c_custkey BIGINT, total_revenue DECIMAL(15,2))"
                ),
                (
                    "INSERT INTO {segment_table} "
                    "SELECT o.o_custkey, round(sum(l.l_extendedprice * (1 - l.l_discount)), 2) "
                    "FROM orders o JOIN lineitem l ON l.l_orderkey = o.o_orderkey "
                    "GROUP BY o.o_custkey "
                    "HAVING sum(l.l_extendedprice * (1 - l.l_discount)) > {threshold}"
                ),
            ],
            "confirmation_sql": "SELECT count(*) FROM {segment_table}",
            "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
            "rules": [
                {
                    "description": "each saved customer appears exactly once",
                    "sql": "SELECT count(*) = count(DISTINCT c_custkey) FROM {segment_table}",
                },
                {
                    "description": "no saved customer is below the threshold",
                    "sql": "SELECT min(total_revenue) > {threshold} FROM {segment_table}",
                },
                {
                    "description": "the saved list matches the fixture's qualifying customers",
                    "sql": (
                        "SELECT (SELECT count(*) FROM {segment_table}) = "
                        "(SELECT count(*) FROM (SELECT o.o_custkey FROM orders o "
                        "JOIN lineitem l ON l.l_orderkey = o.o_orderkey GROUP BY o.o_custkey "
                        "HAVING sum(l.l_extendedprice * (1 - l.l_discount)) > {threshold}) t)"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": "SELECT c_custkey, total_revenue FROM {segment_table} ORDER BY c_custkey",
                    "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT round(sum(total_revenue), 2) FROM {segment_table}",
                    "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
            ],
        },
    },
    {
        "payload": {
            "task_id": "DBA-A13-001",
            "goal": (
                "Find the five customers who generated the most revenue from orders placed during "
                "{year_start} through the day before {year_end_exclusive} — revenue is the sum of "
                "extended price times one minus discount over those orders' line items, ties broken by "
                "the smaller customer key — save them as table {top_table} with columns c_custkey "
                "(BIGINT) and total_revenue (to two decimals), and report the ranking."
            ),
            "submission": (
                "Call submit_final_answer with exactly five tuples in ranking order: "
                "[c_custkey, total_revenue as a numeric string with two decimals]."
            ),
            "category": "ANALYTICS",
            "task_type": "MIX",
            "max_steps": 12,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "top_table": "SELECT 'top_customers'",
            "year_start": "SELECT DATE '1995-01-01'",
            "year_end_exclusive": "SELECT DATE '1996-01-01'",
        },
        "reference": {
            "writes": [
                (
                    "CREATE TABLE {top_table} ("
                    "c_custkey BIGINT, total_revenue DECIMAL(15,2))"
                ),
                (
                    "INSERT INTO {top_table} "
                    "SELECT o.o_custkey, round(sum(l.l_extendedprice * (1 - l.l_discount)), 2) "
                    "FROM orders o JOIN lineitem l ON l.l_orderkey = o.o_orderkey "
                    "WHERE o.o_orderdate >= DATE '{year_start}' "
                    "AND o.o_orderdate < DATE '{year_end_exclusive}' "
                    "GROUP BY o.o_custkey "
                    "ORDER BY sum(l.l_extendedprice * (1 - l.l_discount)) DESC, o.o_custkey ASC "
                    "LIMIT 5"
                ),
            ],
            "confirmation_sql": (
                "SELECT c_custkey, total_revenue FROM {top_table} "
                "ORDER BY total_revenue DESC, c_custkey ASC"
            ),
            "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
            "rules": [
                {
                    "description": "exactly five customers were saved",
                    "sql": "SELECT count(*) = 5 FROM {top_table}",
                },
                {
                    "description": "each saved customer appears exactly once",
                    "sql": "SELECT count(*) = count(DISTINCT c_custkey) FROM {top_table}",
                },
                {
                    "description": "the saved rows are the fixture's ranked top five for the window",
                    "sql": (
                        "SELECT count(*) = 5 FROM ("
                        "SELECT o.o_custkey AS c_custkey, round(sum(l.l_extendedprice * (1 - l.l_discount)), 2) AS r "
                        "FROM orders o JOIN lineitem l ON l.l_orderkey = o.o_orderkey "
                        "WHERE o.o_orderdate >= DATE '{year_start}' "
                        "AND o.o_orderdate < DATE '{year_end_exclusive}' "
                        "GROUP BY o.o_custkey ORDER BY r DESC, o.o_custkey ASC LIMIT 5"
                        ") f JOIN {top_table} t "
                        "ON t.c_custkey = f.c_custkey AND t.total_revenue = f.r"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": (
                        "SELECT c_custkey, total_revenue FROM {top_table} "
                        "ORDER BY total_revenue DESC, c_custkey ASC"
                    ),
                    "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
            ],
        },
    },
]

WAREHOUSE_SEEDS = [
    {
        "payload": {
            "task_id": "DBA-W01-001",
            "goal": (
                "Stage the store sales for the window {window_start} through {window_end} (inclusive) "
                "into table {staging_table}: one row per ticket line (ticket number and item key) carrying "
                "the sales date, the customer's surrogate id, the quantity, and the net paid amount — or "
                "into table {quarantine_table} with reason {quarantine_reason} when the line has no "
                "identified customer. Nothing outside the window may be staged or quarantined, and every "
                "source line in the window goes to exactly one of the two tables."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[number of staged lines, number of quarantined lines]."
            ),
            "category": "OLAP",
            "task_type": "CC",
            "max_steps": 14,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "window_start": "SELECT DATE '1998-01-02'",
            "window_end": "SELECT DATE '1998-04-01'",
            "staging_table": "SELECT 'ticket_staging'",
            "quarantine_table": "SELECT 'quarantine'",
            "quarantine_reason": "SELECT 'missing_customer_identifier'",
        },
        "reference": {
            "writes": [
                (
                    "CREATE TABLE {staging_table} (ticket_number BIGINT, item_id BIGINT, sales_date DATE, "
                    "customer_id BIGINT, quantity BIGINT, net_paid DECIMAL(12,2))"
                ),
                (
                    "CREATE TABLE {quarantine_table} (ticket_number BIGINT, item_id BIGINT, "
                    "sales_date DATE, reason VARCHAR)"
                ),
                (
                    "INSERT INTO {staging_table} "
                    "SELECT ss.ss_ticket_number, ss.ss_item_sk, d.d_date, ss.ss_customer_sk, "
                    "ss.ss_quantity, ss.ss_net_paid "
                    "FROM store_sales ss JOIN date_dim d ON d.d_date_sk = ss.ss_sold_date_sk "
                    "WHERE d.d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}' "
                    "AND ss.ss_customer_sk IS NOT NULL"
                ),
                (
                    "INSERT INTO {quarantine_table} "
                    "SELECT ss.ss_ticket_number, ss.ss_item_sk, d.d_date, '{quarantine_reason}' "
                    "FROM store_sales ss JOIN date_dim d ON d.d_date_sk = ss.ss_sold_date_sk "
                    "WHERE d.d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}' "
                    "AND ss.ss_customer_sk IS NULL"
                ),
            ],
            "confirmation_sql": (
                "SELECT (SELECT count(*) FROM {staging_table}), "
                "(SELECT count(*) FROM {quarantine_table})"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
            "rules": [
                {
                    "description": "staged plus quarantined covers the source window exactly once",
                    "sql": (
                        "SELECT (SELECT count(*) FROM {staging_table}) + "
                        "(SELECT count(*) FROM {quarantine_table}) = ("
                        "SELECT count(*) FROM store_sales WHERE ss_sold_date_sk IN "
                        "(SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}'))"
                    ),
                },
                {
                    "description": "every staged line carries the customer identifier",
                    "sql": "SELECT count(*) = 0 FROM {staging_table} WHERE customer_id IS NULL",
                },
                {
                    "description": "every quarantined line lacks the customer identifier",
                    "sql": (
                        "SELECT count(*) = 0 FROM {quarantine_table} q JOIN store_sales ss "
                        "ON ss.ss_ticket_number = q.ticket_number AND ss.ss_item_sk = q.item_id "
                        "WHERE ss.ss_customer_sk IS NOT NULL"
                    ),
                },
                {
                    "description": "staged lines match their source values",
                    "sql": (
                        "SELECT count(*) = 0 FROM {staging_table} t JOIN store_sales ss "
                        "ON ss.ss_ticket_number = t.ticket_number AND ss.ss_item_sk = t.item_id "
                        "WHERE t.customer_id IS DISTINCT FROM ss.ss_customer_sk "
                        "OR t.quantity IS DISTINCT FROM ss.ss_quantity "
                        "OR t.net_paid IS DISTINCT FROM ss.ss_net_paid"
                    ),
                },
                {
                    "description": "the staging holds one row per ticket line",
                    "sql": (
                        "SELECT count(*) = count(DISTINCT (ticket_number, item_id)) FROM {staging_table}"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": (
                        "SELECT ticket_number, item_id, reason FROM {quarantine_table} "
                        "ORDER BY ticket_number, item_id"
                    ),
                    "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT count(*) FROM {staging_table}",
                    "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
            ],
        },
    },
    {
        "payload": {
            "task_id": "DBA-W03-001",
            "goal": (
                "The daily item revenue summary {summary_table} is being loaded incrementally: the "
                "pipeline already loaded the sub-window {loaded_start} through {loaded_end} (inclusive) "
                "in a previous run. Complete the load for the full window {window_start} through "
                "{window_end} (inclusive) under the declared skip-existing replay policy — a key already "
                "present must be left exactly as it is, never duplicated — so the finished table holds "
                "one row per item and sales date with sales in the window: item_id (the item's surrogate "
                "key), sales_date (DATE), and gross_sales (that key's sum of extended sales price with "
                "missing measures counted as zero, to two decimals)."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[number of summary rows, total gross_sales as a numeric string with two decimals]."
            ),
            "category": "OLAP",
            "task_type": "CC",
            "max_steps": 14,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "summary_table": "SELECT 'daily_item_revenue'",
            "window_start": "SELECT DATE '1998-01-02'",
            "window_end": "SELECT DATE '1998-01-31'",
            "loaded_start": "SELECT DATE '1998-01-02'",
            "loaded_end": "SELECT DATE '1998-01-15'",
        },
        "reference": {
            "writes": [
                (
                    "CREATE TABLE {summary_table} (item_id BIGINT, sales_date DATE, "
                    "gross_sales DECIMAL(15,2))"
                ),
                (
                    "INSERT INTO {summary_table} "
                    "SELECT ss.ss_item_sk, d.d_date, round(sum(COALESCE(ss.ss_ext_sales_price, 0)), 2) "
                    "FROM store_sales ss JOIN date_dim d ON d.d_date_sk = ss.ss_sold_date_sk "
                    "WHERE d.d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}' "
                    "GROUP BY 1, 2"
                ),
            ],
            "confirmation_sql": (
                "SELECT count(*), round(sum(gross_sales), 2) FROM {summary_table}"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
            "rules": [
                {
                    "description": "one row per item and sales date",
                    "sql": "SELECT count(*) = count(DISTINCT (item_id, sales_date)) FROM {summary_table}",
                },
                {
                    "description": "every summary key's gross matches the source window",
                    "sql": (
                        "SELECT count(*) = 0 FROM {summary_table} t "
                        "WHERE t.gross_sales IS DISTINCT FROM COALESCE(("
                        "SELECT round(sum(COALESCE(ss.ss_ext_sales_price, 0)), 2) FROM store_sales ss "
                        "WHERE ss.ss_item_sk = t.item_id "
                        "AND ss.ss_sold_date_sk = (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date = t.sales_date)), 0.00)"
                    ),
                },
                {
                    "description": "summary keys cover the source window exactly",
                    "sql": (
                        "SELECT (SELECT count(*) FROM {summary_table}) = (SELECT count(*) FROM ("
                        "SELECT DISTINCT ss.ss_item_sk, ss.ss_sold_date_sk FROM store_sales ss "
                        "WHERE ss.ss_sold_date_sk IN (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}')))"
                    ),
                },
                {
                    "description": "no rows outside the pinned window",
                    "sql": (
                        "SELECT count(*) = 0 FROM {summary_table} t WHERE t.sales_date NOT BETWEEN "
                        "DATE '{window_start}' AND DATE '{window_end}'"
                    ),
                },
                {
                    "description": "the already-loaded sub-window is complete",
                    "sql": (
                        "SELECT (SELECT count(*) FROM {summary_table} WHERE sales_date BETWEEN "
                        "DATE '{loaded_start}' AND DATE '{loaded_end}') = (SELECT count(*) FROM ("
                        "SELECT DISTINCT ss.ss_item_sk, ss.ss_sold_date_sk FROM store_sales ss "
                        "WHERE ss.ss_sold_date_sk IN (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{loaded_start}' AND DATE '{loaded_end}')))"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": (
                        "SELECT sales_date, round(sum(gross_sales), 2) FROM {summary_table} "
                        "GROUP BY sales_date ORDER BY sales_date"
                    ),
                    "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT count(*) FROM {summary_table}",
                    "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
            ],
        },
    },
    {
        "payload": {
            "task_id": "DBA-W04-001",
            "goal": (
                "Build the observed price history {price_history_table} for the {category} items that "
                "sold at two or more distinct unit prices during {window_start} through {window_end} "
                "(inclusive): from the store-sales unit prices observed in the window, one version row "
                "per item and per contiguous run of its sale dates at an identical price — columns "
                "item_id (the item's surrogate key), unit_price (to two decimals), valid_from (the run's "
                "first sale date), valid_to (the run's last sale date). Runs partition each item's "
                "date-ordered observed sales: a run ends where the next observed sale of that item has "
                "a different price. Every observed sale of a covered item must resolve to exactly one "
                "version carrying its own price."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[number of covered items, number of version rows]."
            ),
            "category": "OLAP",
            "task_type": "CC",
            "max_steps": 16,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "price_history_table": "SELECT 'item_price_history'",
            "category": "SELECT 'Books'",
            "window_start": "SELECT DATE '1998-01-02'",
            "window_end": "SELECT DATE '1998-01-31'",
        },
        "reference": {
            "writes": [
                (
                    "CREATE TABLE {price_history_table} (item_id BIGINT, unit_price DECIMAL(7,2), "
                    "valid_from DATE, valid_to DATE)"
                ),
                (
                    "INSERT INTO {price_history_table} "
                    "SELECT item_id, unit_price, min(sale_date), max(sale_date) FROM ("
                    "SELECT item_id, unit_price, sale_date, "
                    "sum(change_flag) OVER (PARTITION BY item_id ORDER BY sale_date) AS run FROM ("
                    "SELECT ss.ss_item_sk AS item_id, ss.ss_sales_price AS unit_price, d.d_date AS sale_date, "
                    "CASE WHEN ss.ss_sales_price IS DISTINCT FROM "
                    "lag(ss.ss_sales_price) OVER (PARTITION BY ss.ss_item_sk ORDER BY d.d_date) "
                    "THEN 1 ELSE 0 END AS change_flag "
                    "FROM store_sales ss "
                    "JOIN item i ON i.i_item_sk = ss.ss_item_sk "
                    "JOIN date_dim d ON d.d_date_sk = ss.ss_sold_date_sk "
                    "WHERE i.i_category = '{category}' AND ss.ss_sales_price IS NOT NULL "
                    "AND d.d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}') flag) s "
                    "WHERE item_id IN ("
                    "SELECT ss.ss_item_sk FROM store_sales ss "
                    "JOIN item i ON i.i_item_sk = ss.ss_item_sk "
                    "JOIN date_dim d ON d.d_date_sk = ss.ss_sold_date_sk "
                    "WHERE i.i_category = '{category}' AND ss.ss_sales_price IS NOT NULL "
                    "AND d.d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}' "
                    "GROUP BY 1 HAVING count(DISTINCT ss.ss_sales_price) >= 2) "
                    "GROUP BY item_id, unit_price, run"
                ),
            ],
            "confirmation_sql": (
                "SELECT count(DISTINCT item_id), count(*) FROM {price_history_table}"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
            "rules": [
                {
                    "description": "every covered item has at least two versions",
                    "sql": (
                        "SELECT count(*) = 0 FROM ("
                        "SELECT item_id, count(*) AS n FROM {price_history_table} GROUP BY 1) "
                        "WHERE n < 2"
                    ),
                },
                {
                    "description": "only items with at least two distinct observed prices are covered",
                    "sql": (
                        "SELECT count(*) = 0 FROM {price_history_table} t WHERE t.item_id NOT IN ("
                        "SELECT ss.ss_item_sk FROM store_sales ss "
                        "WHERE ss.ss_sales_price IS NOT NULL "
                        "AND ss.ss_sold_date_sk IN (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}') "
                        "GROUP BY 1 HAVING count(DISTINCT ss.ss_sales_price) >= 2)"
                    ),
                },
                {
                    "description": "every covered item's observed sale resolves to one version at its own price",
                    "sql": (
                        "SELECT count(*) = 0 FROM store_sales ss "
                        "WHERE ss.ss_item_sk IN (SELECT item_id FROM {price_history_table}) "
                        "AND ss.ss_sales_price IS NOT NULL "
                        "AND ss.ss_sold_date_sk IN (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}') "
                        "AND NOT EXISTS (SELECT 1 FROM {price_history_table} p "
                        "WHERE p.item_id = ss.ss_item_sk AND p.unit_price = ss.ss_sales_price "
                        "AND ss.ss_sold_date_sk IN (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN p.valid_from AND p.valid_to))"
                    ),
                },
                {
                    "description": "versions of an item never overlap",
                    "sql": (
                        "SELECT count(*) = 0 FROM {price_history_table} a JOIN {price_history_table} b "
                        "ON a.item_id = b.item_id AND a.valid_from < b.valid_from "
                        "AND b.valid_from <= a.valid_to"
                    ),
                },
                {
                    "description": "every version's range sits inside the window",
                    "sql": (
                        "SELECT count(*) = 0 FROM {price_history_table} WHERE valid_from < "
                        "DATE '{window_start}' OR valid_to > DATE '{window_end}'"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": (
                        "SELECT item_id, unit_price, valid_from, valid_to "
                        "FROM {price_history_table} ORDER BY item_id, valid_from"
                    ),
                    "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT count(*) FROM {price_history_table}",
                    "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
            ],
        },
    },
    {
        "payload": {
            "task_id": "DBA-W05-001",
            "goal": (
                "The nightly job building {summary_table} crashed on dates containing sales with missing "
                "measures, leaving the window incomplete: diagnose which dates the crashed run never "
                "wrote, then rebuild the summary for the full window {window_start} through "
                "{window_end} (inclusive) — one row per date that has at least one store sale, with "
                "sales_date (DATE) and revenue_total (that date's sum of net paid with missing measures "
                "counted as zero, to two decimals). Then record the completion in {marker_table}: one row "
                "with table_name {marker_name} and watermark set to the last summarized date."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[number of summary rows, total revenue_total as a numeric string with two decimals, "
                "the watermark date as YYYY-MM-DD]."
            ),
            "category": "OLAP",
            "task_type": "MIX",
            "max_steps": 16,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "summary_table": "SELECT 'revenue_daily'",
            "marker_table": "SELECT 'load_markers'",
            "marker_name": "SELECT 'revenue_daily'",
            "window_start": "SELECT DATE '1998-01-02'",
            "window_end": "SELECT DATE '1998-01-31'",
        },
        "reference": {
            "writes": [
                (
                    "CREATE TABLE {summary_table} (sales_date DATE, revenue_total DECIMAL(15,2))"
                ),
                (
                    "INSERT INTO {summary_table} "
                    "SELECT d.d_date, round(sum(COALESCE(ss.ss_net_paid, 0)), 2) "
                    "FROM store_sales ss JOIN date_dim d ON d.d_date_sk = ss.ss_sold_date_sk "
                    "WHERE d.d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}' "
                    "GROUP BY 1"
                ),
                (
                    "CREATE TABLE {marker_table} (table_name VARCHAR, watermark DATE)"
                ),
                (
                    "INSERT INTO {marker_table} "
                    "SELECT '{marker_name}', max(sales_date) FROM {summary_table}"
                ),
            ],
            "confirmation_sql": (
                "SELECT (SELECT count(*) FROM {summary_table}), "
                "round((SELECT sum(revenue_total) FROM {summary_table}), 2), "
                "(SELECT watermark FROM {marker_table} WHERE table_name = '{marker_name}')"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
            "rules": [
                {
                    "description": "one row per date with sales in the window",
                    "sql": (
                        "SELECT (SELECT count(*) FROM {summary_table}) = (SELECT count(*) FROM ("
                        "SELECT DISTINCT ss_sold_date_sk FROM store_sales "
                        "WHERE ss_sold_date_sk IN (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}')))"
                    ),
                },
                {
                    "description": "every date's total matches the source with missing measures as zero",
                    "sql": (
                        "SELECT count(*) = 0 FROM {summary_table} t "
                        "WHERE t.revenue_total IS DISTINCT FROM COALESCE(("
                        "SELECT round(sum(COALESCE(ss.ss_net_paid, 0)), 2) FROM store_sales ss "
                        "WHERE ss.ss_sold_date_sk = (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date = t.sales_date)), 0.00)"
                    ),
                },
                {
                    "description": "no summary row carries a null or negative revenue",
                    "sql": (
                        "SELECT count(*) = 0 FROM {summary_table} "
                        "WHERE revenue_total IS NULL OR revenue_total < 0"
                    ),
                },
                {
                    "description": "no dates outside the pinned window",
                    "sql": (
                        "SELECT count(*) = 0 FROM {summary_table} WHERE sales_date NOT BETWEEN "
                        "DATE '{window_start}' AND DATE '{window_end}'"
                    ),
                },
                {
                    "description": "the marker records the last summarized date",
                    "sql": (
                        "SELECT (SELECT watermark FROM {marker_table} WHERE table_name = '{marker_name}') "
                        "= (SELECT max(sales_date) FROM {summary_table})"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": "SELECT sales_date, revenue_total FROM {summary_table} ORDER BY sales_date",
                    "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT table_name, watermark FROM {marker_table} ORDER BY table_name",
                    "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
            ],
        },
    },
    {
        "payload": {
            "task_id": "DBA-W06-001",
            "goal": (
                "A naive daily returns summary aggregates store returns by their return date, and the "
                "numbers never reconcile with the sales view: many returned lines reference a ticket "
                "whose sale happened on an earlier date. Diagnose the linkage, then build "
                "{returns_summary_table} that attributes each returned line to the date of its sale: "
                "one row per sale date holding returned_amount (the sum of return amounts of the "
                "returned lines whose matching sale — same ticket number and item key — falls on that "
                "date within the window {window_start} through {window_end} (inclusive), with missing "
                "return amounts counted as zero, to two decimals). The summary must reconcile with the "
                "source: every returned line of an in-window sale is counted exactly once."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[number of summary rows, total returned_amount as a numeric string with two decimals]."
            ),
            "category": "OLAP",
            "task_type": "MIX",
            "max_steps": 16,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "returns_summary_table": "SELECT 'returns_by_sale_date'",
            "window_start": "SELECT DATE '1998-01-02'",
            "window_end": "SELECT DATE '1998-01-31'",
        },
        "reference": {
            "writes": [
                (
                    "CREATE TABLE {returns_summary_table} (sale_date DATE, returned_amount DECIMAL(15,2))"
                ),
                (
                    "INSERT INTO {returns_summary_table} "
                    "SELECT d.d_date, round(sum(COALESCE(sr.sr_return_amt, 0)), 2) "
                    "FROM store_returns sr "
                    "JOIN store_sales ss ON ss.ss_ticket_number = sr.sr_ticket_number "
                    "AND ss.ss_item_sk = sr.sr_item_sk "
                    "JOIN date_dim d ON d.d_date_sk = ss.ss_sold_date_sk "
                    "WHERE d.d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}' "
                    "GROUP BY 1"
                ),
            ],
            "confirmation_sql": (
                "SELECT count(*), round(sum(returned_amount), 2) FROM {returns_summary_table}"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
            "rules": [
                {
                    "description": "one row per sale date with returns",
                    "sql": (
                        "SELECT (SELECT count(*) FROM {returns_summary_table}) = (SELECT count(*) FROM ("
                        "SELECT DISTINCT ss.ss_sold_date_sk FROM store_returns sr "
                        "JOIN store_sales ss ON ss.ss_ticket_number = sr.sr_ticket_number "
                        "AND ss.ss_item_sk = sr.sr_item_sk "
                        "WHERE ss.ss_sold_date_sk IN (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}')))"
                    ),
                },
                {
                    "description": "every date's amount matches the ticket-linked source",
                    "sql": (
                        "SELECT count(*) = 0 FROM {returns_summary_table} t "
                        "WHERE t.returned_amount IS DISTINCT FROM COALESCE(("
                        "SELECT round(sum(COALESCE(sr.sr_return_amt, 0)), 2) FROM store_returns sr "
                        "JOIN store_sales ss ON ss.ss_ticket_number = sr.sr_ticket_number "
                        "AND ss.ss_item_sk = sr.sr_item_sk "
                        "WHERE ss.ss_sold_date_sk = (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date = t.sale_date)), 0.00)"
                    ),
                },
                {
                    "description": "the grand total reconciles with all returns of in-window sales",
                    "sql": (
                        "SELECT round((SELECT sum(returned_amount) FROM {returns_summary_table}), 2) = ("
                        "SELECT round(sum(COALESCE(sr.sr_return_amt, 0)), 2) FROM store_returns sr "
                        "WHERE EXISTS (SELECT 1 FROM store_sales ss "
                        "WHERE ss.ss_ticket_number = sr.sr_ticket_number "
                        "AND ss.ss_item_sk = sr.sr_item_sk "
                        "AND ss.ss_sold_date_sk IN (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}')))"
                    ),
                },
                {
                    "description": "no dates outside the pinned window",
                    "sql": (
                        "SELECT count(*) = 0 FROM {returns_summary_table} WHERE sale_date NOT BETWEEN "
                        "DATE '{window_start}' AND DATE '{window_end}'"
                    ),
                },
                {
                    "description": "returned lines are never attributed to their return date instead",
                    "sql": (
                        "SELECT count(*) = 0 FROM {returns_summary_table} t WHERE t.sale_date IN ("
                        "SELECT rd.d_date FROM store_returns sr "
                        "JOIN date_dim rd ON rd.d_date_sk = sr.sr_returned_date_sk "
                        "WHERE rd.d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}') "
                        "AND t.sale_date NOT IN ("
                        "SELECT sd.d_date FROM store_returns sr "
                        "JOIN store_sales ss ON ss.ss_ticket_number = sr.sr_ticket_number "
                        "AND ss.ss_item_sk = sr.sr_item_sk "
                        "JOIN date_dim sd ON sd.d_date_sk = ss.ss_sold_date_sk "
                        "WHERE sd.d_date = t.sale_date)"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": (
                        "SELECT sale_date, returned_amount FROM {returns_summary_table} ORDER BY sale_date"
                    ),
                    "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT count(*) FROM {returns_summary_table}",
                    "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
            ],
        },
    },
]

DSDB_SEEDS = [
    {
        "payload": {
            "task_id": "DBA-A05-001",
            "goal": (
                "Give me the item-sales report for the categories {categories} during the 30-day period "
                "starting {start_date} (through {end_date}, inclusive): the ten highest-revenue items across "
                "those categories, one tuple per item with the item's category, class, item id, total revenue "
                "(sum of extended sales price, to two decimals), and the item's contribution to its class "
                "revenue as a percentage (item revenue over the sum of revenue of every item in the same "
                "class, to two decimals). Order tuples by revenue descending with item id as tie-break."
            ),
            "submission": (
                "Call submit_final_answer with exactly ten tuples: "
                "[category, class, item id, revenue as a numeric string with two decimals, "
                "class contribution percentage as a numeric string with two decimals]."
            ),
            "category": "ANALYTICS",
            "task_type": "IS",
            "max_steps": 14,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "categories": "SELECT 'Books, Electronics, Men, and Women'",
            "start_date": "SELECT DATE '1998-01-02'",
            "end_date": "SELECT DATE '1998-01-31'",
        },
        "trusted_sql": (
            "SELECT i.i_category, i.i_class, i.i_item_id, "
            "round(sum(COALESCE(ss.ss_ext_sales_price, 0)), 2) AS item_revenue, "
            "round(sum(COALESCE(ss.ss_ext_sales_price, 0)) * 100.0 / "
            "sum(sum(COALESCE(ss.ss_ext_sales_price, 0))) OVER (PARTITION BY i.i_class), 2) AS class_contribution_pct "
            "FROM store_sales ss "
            "JOIN item i ON i.i_item_sk = ss.ss_item_sk "
            "JOIN date_dim d ON d.d_date_sk = ss.ss_sold_date_sk "
            "WHERE i.i_category IN ('Books', 'Electronics', 'Men', 'Women') "
            "AND d.d_date BETWEEN DATE '{start_date}' AND DATE '{end_date}' "
            "GROUP BY i.i_category, i.i_class, i.i_item_id "
            "ORDER BY item_revenue DESC, i.i_item_id ASC "
            "LIMIT 10"
        ),
        "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
        "expected_row_count": 10,
    },
    {
        "payload": {
            "task_id": "DBA-A06-001",
            "goal": (
                "Store net sales declined between the 30-day period starting {period_a_start} (through "
                "{period_a_end}, inclusive) and the 30-day period starting {period_b_start} (through "
                "{period_b_end}, inclusive). Net sales are the sum of net paid amounts over store sales. "
                "Report the item categories that contributed most to the decline: one tuple per category "
                "with its signed contribution (net sales in the second period minus net sales in the first, "
                "negative for a decline, to two decimals), ordered most negative first with the category "
                "name as tie-break, followed by one final tuple with the overall net-sales change. This is "
                "a contribution analysis that reconciles to the overall change, not a causal claim."
            ),
            "submission": (
                "Call submit_final_answer with exactly eleven tuples ordered as described: "
                "[category or 'ALL', signed contribution as a numeric string with two decimals]."
            ),
            "category": "ANALYTICS",
            "task_type": "IS",
            "max_steps": 14,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "period_a_start": "SELECT DATE '1998-05-01'",
            "period_a_end": "SELECT DATE '1998-05-31'",
            "period_b_start": "SELECT DATE '1998-06-01'",
            "period_b_end": "SELECT DATE '1998-06-30'",
        },
        "trusted_sql": (
            "WITH sales AS ("
            "SELECT i.i_category AS cat, d.d_date AS dt, ss.ss_net_paid AS amt "
            "FROM store_sales ss "
            "JOIN item i ON i.i_item_sk = ss.ss_item_sk "
            "JOIN date_dim d ON d.d_date_sk = ss.ss_sold_date_sk "
            "WHERE d.d_date BETWEEN DATE '{period_a_start}' AND DATE '{period_b_end}'), "
            "totals AS ("
            "SELECT round(sum(CASE WHEN dt BETWEEN DATE '{period_a_start}' AND DATE '{period_a_end}' "
            "THEN amt ELSE 0 END), 2) AS a_total, "
            "round(sum(CASE WHEN dt BETWEEN DATE '{period_b_start}' AND DATE '{period_b_end}' "
            "THEN amt ELSE 0 END), 2) AS b_total FROM sales), "
            "per_cat AS ("
            "SELECT cat, "
            "round(sum(CASE WHEN dt BETWEEN DATE '{period_a_start}' AND DATE '{period_a_end}' "
            "THEN amt ELSE 0 END), 2) AS a_total, "
            "round(sum(CASE WHEN dt BETWEEN DATE '{period_b_start}' AND DATE '{period_b_end}' "
            "THEN amt ELSE 0 END), 2) AS b_total FROM sales GROUP BY cat) "
            "SELECT cat, contribution FROM ("
            "SELECT x.cat, round(x.b_total - x.a_total, 2) AS contribution FROM per_cat x "
            "UNION ALL "
            "SELECT 'ALL', round(t.b_total - t.a_total, 2) FROM totals t) u "
            "ORDER BY CASE WHEN u.cat = 'ALL' THEN 1 ELSE 0 END, u.contribution ASC, u.cat ASC"
        ),
        "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
        "expected_row_count": 11,
    },
    {
        "payload": {
            "task_id": "DBA-A07-001",
            "goal": (
                "Compare 30-day repeat-purchase rates for the customers whose first observed store purchase "
                "occurred in {cohorts}: one tuple per cohort month with the cohort month, the number of "
                "customers whose first recorded store purchase falls in that month, the number of those "
                "customers with at least one further store purchase in the 30 days after that first "
                "purchase (strictly after, through day 30, inclusive), and the repeat rate as a percentage "
                "to two decimals. Only recorded purchases count — returns never qualify as purchase "
                "activity. Customers whose 30-day observation window extends past the recorded sales "
                "history stay in the denominator and count as non-repeat."
            ),
            "submission": (
                "Call submit_final_answer with exactly two tuples ordered by cohort month: "
                "[cohort month as YYYY-MM, customers, repeaters, "
                "repeat rate percentage as a numeric string with two decimals]."
            ),
            "category": "ANALYTICS",
            "task_type": "IS",
            "max_steps": 14,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "cohorts": "SELECT 'September 1998 and October 1998'",
        },
        "trusted_sql": (
            "WITH f AS ("
            "SELECT ss_customer_sk AS cid, min(ss_sold_date_sk) AS fsk "
            "FROM store_sales WHERE ss_customer_sk IS NOT NULL GROUP BY 1), "
            "coh AS ("
            "SELECT f.cid AS cid, first_d.d_date AS fd "
            "FROM f JOIN date_dim first_d ON first_d.d_date_sk = f.fsk "
            "WHERE strftime(first_d.d_date, '%Y-%m') IN ('1998-09', '1998-10')), "
            "per_cohort AS ("
            "SELECT strftime(fd, '%Y-%m') AS cohort_month, count(*) AS customers, "
            "sum(CASE WHEN EXISTS ("
            "SELECT 1 FROM store_sales s2 JOIN date_dim d2 ON d2.d_date_sk = s2.ss_sold_date_sk "
            "WHERE s2.ss_customer_sk = coh.cid AND d2.d_date > coh.fd "
            "AND d2.d_date <= coh.fd + INTERVAL 30 DAY) THEN 1 ELSE 0 END) AS repeaters "
            "FROM coh GROUP BY 1) "
            "SELECT cohort_month, customers, repeaters, "
            "round(repeaters * 100.0 / customers, 2) AS repeat_rate_pct "
            "FROM per_cohort ORDER BY cohort_month"
        ),
        "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
        "expected_row_count": 2,
    },
    {
        "payload": {
            "task_id": "DBA-A08-001",
            "goal": (
                "Using the registered two-proportion comparison method, compare store sales made under "
                "promotion {promotion_id} against store sales made under any other promotion during the "
                "window {window_start} through {window_end} (inclusive): one tuple with the treatment "
                "line count, the treatment lines with a recorded return, the treatment return rate as a "
                "percentage to two decimals, then the same three values for the control group (sales "
                "under any other promotion), then the absolute lift (treatment rate minus control rate, "
                "percentage points to two decimals) and the relative lift (absolute lift over the "
                "control rate, as a percentage to two decimals). A line counts as returned when a store "
                "return references the same ticket number and item key. Sales without a registered "
                "promotion belong to neither group. This is an observational comparison that reconciles "
                "the group partitions, not a causal claim."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[treatment lines, treatment returned lines, treatment return rate as a numeric string "
                "with two decimals, control lines, control returned lines, control return rate as a "
                "numeric string with two decimals, absolute lift as a numeric string with two decimals, "
                "relative lift as a numeric string with two decimals]."
            ),
            "category": "ANALYTICS",
            "task_type": "IS",
            "max_steps": 14,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "promotion_id": "SELECT 1",
            "window_start": "SELECT DATE '1998-01-02'",
            "window_end": "SELECT DATE '1998-01-31'",
        },
        "trusted_sql": (
            "WITH pop AS ("
            "SELECT CASE WHEN ss.ss_promo_sk = {promotion_id} THEN 'treatment' ELSE 'control' END AS g, "
            "count(*) AS n, "
            "sum(CASE WHEN EXISTS (SELECT 1 FROM store_returns sr "
            "WHERE sr.sr_ticket_number = ss.ss_ticket_number "
            "AND sr.sr_item_sk = ss.ss_item_sk) THEN 1 ELSE 0 END) AS ret "
            "FROM store_sales ss "
            "WHERE ss.ss_promo_sk IS NOT NULL "
            "AND ss.ss_sold_date_sk IN (SELECT d_date_sk FROM date_dim "
            "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}') "
            "GROUP BY 1), "
            "t AS (SELECT n, ret FROM pop WHERE g = 'treatment'), "
            "ctl AS (SELECT n, ret FROM pop WHERE g = 'control') "
            "SELECT t.n, t.ret, round(t.ret * 100.0 / t.n, 2), "
            "ctl.n, ctl.ret, round(ctl.ret * 100.0 / ctl.n, 2), "
            "round((t.ret * 100.0 / t.n) - (ctl.ret * 100.0 / ctl.n), 2), "
            "round(((t.ret * 100.0 / t.n) - (ctl.ret * 100.0 / ctl.n)) "
            "* 100.0 / (ctl.ret * 100.0 / ctl.n), 2) "
            "FROM t, ctl"
        ),
        "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
        "expected_row_count": 1,
    },
]

DSDB_STATE_SEEDS = [
    {
        "payload": {
            "task_id": "DBA-W02-001",
            "goal": (
                "Create the daily store-sales summary as table {summary_table}: one row per store, item, "
                "and sales date for store sales dated {window_start} through {window_end} (inclusive), with "
                "columns store_id (the store's surrogate key), item_id (the item's surrogate key), "
                "sales_date (DATE), gross_sales (that key's sum of extended sales price, to two decimals — "
                "sales lines without a recorded extended price contribute zero), "
                "return_amount (that key's sum of return amounts for returns whose ticket number and item "
                "match a sale of the same store, item, and date inside the window — never attribute returns "
                "by aggregating them per item or per store; returns without a recorded amount contribute "
                "zero, to two decimals), and net_sales (gross minus returns, to two decimals)."
            ),
            "submission": (
                "Call submit_final_answer with exactly one tuple: "
                "[number of summary rows, total gross_sales, total return_amount, total net_sales, "
                "each total as a numeric string with two decimals]."
            ),
            "category": "OLAP",
            "task_type": "CC",
            "max_steps": 16,
            "query_timeout": 30,
            "row_limit": 50,
        },
        "params": {
            "summary_table": "SELECT 'store_sales_daily_summary'",
            "window_start": "SELECT DATE '1998-01-02'",
            "window_end": "SELECT DATE '1998-01-08'",
        },
        "reference": {
            "writes": [
                (
                    "CREATE TABLE {summary_table} (store_id BIGINT, item_id BIGINT, sales_date DATE, "
                    "gross_sales DECIMAL(15,2), return_amount DECIMAL(15,2), net_sales DECIMAL(15,2))"
                ),
                (
                    "INSERT INTO {summary_table} "
                    "SELECT ss.ss_store_sk, ss.ss_item_sk, d.d_date, "
                    "round(sum(COALESCE(ss.ss_ext_sales_price, 0)), 2), 0.00, "
                    "round(sum(COALESCE(ss.ss_ext_sales_price, 0)), 2) "
                    "FROM store_sales ss "
                    "JOIN date_dim d ON d.d_date_sk = ss.ss_sold_date_sk "
                    "WHERE d.d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}' "
                    "GROUP BY 1, 2, 3"
                ),
                (
                    "UPDATE {summary_table} t SET return_amount = m.ret, "
                    "net_sales = round(t.gross_sales - m.ret, 2) FROM ("
                    "SELECT ss.ss_store_sk AS st, ss.ss_item_sk AS it, d.d_date AS dt, "
                    "round(sum(COALESCE(sr.sr_return_amt, 0)), 2) AS ret "
                    "FROM store_returns sr "
                    "JOIN store_sales ss ON ss.ss_ticket_number = sr.sr_ticket_number "
                    "AND ss.ss_item_sk = sr.sr_item_sk "
                    "JOIN date_dim d ON d.d_date_sk = ss.ss_sold_date_sk "
                    "WHERE d.d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}' "
                    "GROUP BY 1, 2, 3) m "
                    "WHERE t.store_id = m.st AND t.item_id = m.it AND t.sales_date = m.dt"
                ),
            ],
            "confirmation_sql": (
                "SELECT count(*), round(sum(gross_sales), 2), round(sum(return_amount), 2), "
                "round(sum(net_sales), 2) FROM {summary_table}"
            ),
            "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
            "rules": [
                {
                    "description": "one row per store, item, and sales date",
                    "sql": (
                        "SELECT count(*) = count(DISTINCT (store_id, item_id, sales_date)) "
                        "FROM {summary_table}"
                    ),
                },
                {
                    "description": "every summary key's gross matches the source window",
                    "sql": (
                        "SELECT count(*) = 0 FROM {summary_table} t "
                        "WHERE t.gross_sales IS DISTINCT FROM COALESCE(("
                        "SELECT round(sum(COALESCE(ss.ss_ext_sales_price, 0)), 2) FROM store_sales ss "
                        "WHERE ss.ss_store_sk = t.store_id AND ss.ss_item_sk = t.item_id "
                        "AND ss.ss_sold_date_sk = (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date = t.sales_date) "
                        "AND ss.ss_sold_date_sk IN (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}')), 0.00)"
                    ),
                },
                {
                    "description": "summary keys cover the source window exactly",
                    "sql": (
                        "SELECT (SELECT count(*) FROM {summary_table}) = (SELECT count(*) FROM ("
                        "SELECT DISTINCT ss.ss_store_sk, ss.ss_item_sk, ss.ss_sold_date_sk "
                        "FROM store_sales ss WHERE ss.ss_sold_date_sk IN "
                        "(SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}')))"
                    ),
                },
                {
                    "description": "returns are attributed per key from ticket-and-item matches only",
                    "sql": (
                        "SELECT count(*) = 0 FROM {summary_table} t "
                        "WHERE t.return_amount IS DISTINCT FROM COALESCE(("
                        "SELECT round(sum(COALESCE(sr.sr_return_amt, 0)), 2) FROM store_returns sr "
                        "WHERE EXISTS (SELECT 1 FROM store_sales ss "
                        "WHERE ss.ss_ticket_number = sr.sr_ticket_number "
                        "AND ss.ss_item_sk = sr.sr_item_sk "
                        "AND ss.ss_store_sk = t.store_id AND ss.ss_item_sk = t.item_id "
                        "AND ss.ss_sold_date_sk = (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date = t.sales_date) "
                        "AND ss.ss_sold_date_sk IN (SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}'))), 0.00)"
                    ),
                },
                {
                    "description": "net equals gross minus returns on every row",
                    "sql": (
                        "SELECT count(*) = 0 FROM {summary_table} "
                        "WHERE net_sales IS DISTINCT FROM round(gross_sales - return_amount, 2)"
                    ),
                },
                {
                    "description": "the window's returns actually exercise the attribution",
                    "sql": (
                        "SELECT (SELECT count(*) FROM {summary_table} WHERE return_amount > 0) >= 1 AND "
                        "round((SELECT sum(return_amount) FROM {summary_table}), 2) = ("
                        "SELECT round(sum(COALESCE(sr.sr_return_amt, 0)), 2) FROM store_returns sr WHERE EXISTS ("
                        "SELECT 1 FROM store_sales ss WHERE ss.ss_ticket_number = sr.sr_ticket_number "
                        "AND ss.ss_item_sk = sr.sr_item_sk AND ss.ss_sold_date_sk IN "
                        "(SELECT d_date_sk FROM date_dim "
                        "WHERE d_date BETWEEN DATE '{window_start}' AND DATE '{window_end}')))"
                    ),
                },
            ],
            "state_checks": [
                {
                    "sql": (
                        "SELECT store_id, item_id, sales_date, gross_sales, return_amount, net_sales "
                        "FROM {summary_table} ORDER BY sales_date, store_id, item_id"
                    ),
                    "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
                {
                    "sql": "SELECT count(*) FROM {summary_table}",
                    "matching": {"order_sensitive": False, "numeric_tolerance": 0.01, "null_handling": "exact"},
                },
            ],
        },
    },
]


def _render_reference(reference: dict, params: dict[str, Any]) -> dict:
    rendered = {
        "writes": [statement.format(**params) for statement in reference["writes"]],
        "confirmation_sql": reference["confirmation_sql"].format(**params),
        "state_checks": [
            {**check, "sql": check["sql"].format(**params)} for check in reference["state_checks"]
        ],
        "rules": [],
    }
    for rule in reference.get("rules", ()):
        entry = {"sql": rule["sql"].format(**params)}
        if rule.get("description") is not None:
            entry["description"] = rule["description"]
        rendered["rules"].append(entry)
    return rendered


def seed_analytics_tasks(store: TaskCatalogStore, olapdb: Engine, olapdb_url: str) -> list[dict]:
    """Seed the analytics (A-family) tasks against the analytical DuckDB fixture.

    Trusted-SQL seeds (IS/NAV) resolve their oracles directly from the fixture;
    state-seed seeds (CC/MIX) execute reference writes under rollback, mirroring
    the OLTP STATE_SEEDS path.
    """
    seeded = []
    for seed in ANALYTICS_SEEDS:
        params = resolve_params(olapdb, seed.get("params"))
        expected = compute_expected_tuples(olapdb, seed["trusted_sql"].format(**params))
        payload = dict(seed["payload"])
        payload["goal"] = payload["goal"].format(**params)
        payload["sqlalchemy_url"] = olapdb_url
        payload["terminal_condition"] = {
            "expected_tuples": expected,
            "matching": seed["matching"],
        }
        if not expected:
            raise SystemExit(
                f"{payload['task_id']}: trusted oracle returned no rows for params {params}; "
                "refusing to seed a task with an empty expectation"
            )
        row_count = seed.get("expected_row_count")
        if row_count is not None and len(expected) != row_count:
            raise SystemExit(
                f"{payload['task_id']}: trusted oracle returned {len(expected)} rows, "
                f"expected {row_count} for params {params}"
            )
        record = parse_task_payload(payload)
        store.delete(record.task_id)
        store.create(record)
        seeded.append(payload)
    for seed in ANALYTICS_STATE_SEEDS:
        params = resolve_params(olapdb, seed.get("params"))
        reference = _render_reference(seed["reference"], params)
        confirmation, checks, rules = compute_state_expectations(olapdb, reference)
        payload = dict(seed["payload"])
        payload["goal"] = payload["goal"].format(**params)
        payload["sqlalchemy_url"] = olapdb_url
        if not confirmation:
            raise SystemExit(
                f"{payload['task_id']}: confirmation oracle returned no rows for params {params}; "
                "refusing to seed a task with an empty expectation"
            )
        empty = [check["sql"] for check in checks if not check["expected_tuples"]]
        if empty:
            raise SystemExit(
                f"{payload['task_id']}: state checks returned no rows for params {params}: {empty}; "
                "refusing to seed a task with an empty expectation"
            )
        broken = [
            f"rule {rule['position']}: {rule['sql']}" for rule in rules if not rule["passed"]
        ]
        if broken:
            raise SystemExit(
                f"{payload['task_id']}: rules failed under the reference execution for params "
                f"{params}: {broken}; refusing to seed an impossible oracle"
            )
        payload["terminal_condition"] = {
            "expected_tuples": confirmation,
            "matching": dict(seed["reference"].get("matching", {})),
            "state_checks": checks,
        }
        if rules:
            payload["terminal_condition"]["rules"] = [
                {k: v for k, v in rule.items() if k != "passed"} for rule in rules
            ]
        record = parse_task_payload(payload)
        store.delete(record.task_id)
        store.create(record)
        seeded.append(payload)
    return seeded


def seed_warehouse_tasks(store: TaskCatalogStore, dsdb: Engine, dsdb_url: str) -> list[dict]:
    """Seed the warehouse (W-family) tasks against the TPC-DS retail fixture.

    All W-seeds are state seeds: reference writes execute under rollback on the
    retail fixture (its pipeline layer), mirroring the analytics state path,
    with the same refusal rules for empty oracles and broken rules.
    """
    seeded = []
    for seed in WAREHOUSE_SEEDS:
        params = resolve_params(dsdb, seed.get("params"))
        reference = _render_reference(seed["reference"], params)
        confirmation, checks, rules = compute_state_expectations(dsdb, reference)
        payload = dict(seed["payload"])
        payload["goal"] = payload["goal"].format(**params)
        payload["sqlalchemy_url"] = dsdb_url
        if not confirmation:
            raise SystemExit(
                f"{payload['task_id']}: confirmation oracle returned no rows for params {params}; "
                "refusing to seed a task with an empty expectation"
            )
        empty = [check["sql"] for check in checks if not check["expected_tuples"]]
        if empty:
            raise SystemExit(
                f"{payload['task_id']}: state checks returned no rows for params {params}: {empty}; "
                "refusing to seed a task with an empty expectation"
            )
        broken = [
            f"rule {rule['position']}: {rule['sql']}" for rule in rules if not rule["passed"]
        ]
        if broken:
            raise SystemExit(
                f"{payload['task_id']}: rules failed under the reference execution for params "
                f"{params}: {broken}; refusing to seed an impossible oracle"
            )
        payload["terminal_condition"] = {
            "expected_tuples": confirmation,
            "matching": dict(seed["reference"].get("matching", {})),
            "state_checks": checks,
        }
        if rules:
            payload["terminal_condition"]["rules"] = [
                {k: v for k, v in rule.items() if k != "passed"} for rule in rules
            ]
        record = parse_task_payload(payload)
        store.delete(record.task_id)
        store.create(record)
        seeded.append(payload)
    return seeded


def seed_retail_tasks(store: TaskCatalogStore, dsdb: Engine, dsdb_url: str) -> list[dict]:
    """Seed the retail tasks (W02 + A05-A08) against the TPC-DS fixture.

    A05-A08 are trusted-SQL seeds resolving their oracles directly from the
    fixture; W02 is a state seed executing reference writes under rollback,
    mirroring the analytics and warehouse paths with the same refusal rules.
    """
    seeded = []
    for seed in DSDB_SEEDS:
        params = resolve_params(dsdb, seed.get("params"))
        expected = compute_expected_tuples(dsdb, seed["trusted_sql"].format(**params))
        payload = dict(seed["payload"])
        payload["goal"] = payload["goal"].format(**params)
        payload["sqlalchemy_url"] = dsdb_url
        payload["terminal_condition"] = {
            "expected_tuples": expected,
            "matching": seed["matching"],
        }
        if not expected:
            raise SystemExit(
                f"{payload['task_id']}: trusted oracle returned no rows for params {params}; "
                "refusing to seed a task with an empty expectation"
            )
        row_count = seed.get("expected_row_count")
        if row_count is not None and len(expected) != row_count:
            raise SystemExit(
                f"{payload['task_id']}: trusted oracle returned {len(expected)} rows, "
                f"expected {row_count} for params {params}"
            )
        record = parse_task_payload(payload)
        store.delete(record.task_id)
        store.create(record)
        seeded.append(payload)
    for seed in DSDB_STATE_SEEDS:
        params = resolve_params(dsdb, seed.get("params"))
        reference = _render_reference(seed["reference"], params)
        confirmation, checks, rules = compute_state_expectations(dsdb, reference)
        payload = dict(seed["payload"])
        payload["goal"] = payload["goal"].format(**params)
        payload["sqlalchemy_url"] = dsdb_url
        if not confirmation:
            raise SystemExit(
                f"{payload['task_id']}: confirmation oracle returned no rows for params {params}; "
                "refusing to seed a task with an empty expectation"
            )
        empty = [check["sql"] for check in checks if not check["expected_tuples"]]
        if empty:
            raise SystemExit(
                f"{payload['task_id']}: state checks returned no rows for params {params}: {empty}; "
                "refusing to seed a task with an empty expectation"
            )
        broken = [
            f"rule {rule['position']}: {rule['sql']}" for rule in rules if not rule["passed"]
        ]
        if broken:
            raise SystemExit(
                f"{payload['task_id']}: rules failed under the reference execution for params "
                f"{params}: {broken}; refusing to seed an impossible oracle"
            )
        payload["terminal_condition"] = {
            "expected_tuples": confirmation,
            "matching": dict(seed["reference"].get("matching", {})),
            "state_checks": checks,
        }
        if rules:
            payload["terminal_condition"]["rules"] = [
                {k: v for k, v in rule.items() if k != "passed"} for rule in rules
            ]
        record = parse_task_payload(payload)
        store.delete(record.task_id)
        store.create(record)
        seeded.append(payload)
    return seeded


def seed_rag_tasks(store: TaskCatalogStore, admin: Engine, ragdb_url: str) -> list[dict]:
    """Seed the RAG-family tasks against the ragdb fixture.

    The oracle comes from the corpus's authored gold incident ids (stored in
    admindb's rag_fixture_meta by the ragdb fixture loader). The goal states the
    information need and filters only — no embedding mechanics; seeding never
    contacts an embedding endpoint.
    """
    with admin.connect() as conn:
        meta = conn.execute(
            text(
                "SELECT query_text, version, resolved_cutoff, gold_incident_ids, "
                "r03_question, r03_expected_answer, r03_gold_incident_ids "
                "FROM rag_fixture_meta WHERE id"
            )
        ).mappings().first()
        variants = conn.execute(
            text(
                "SELECT task_id, kind, question, expected_answer, gold_incident_ids, filters, "
                "key_points, reward_mode "
                "FROM rag_variant_tasks ORDER BY task_id"
            )
        ).mappings().all()
    if meta is None:
        print("rag fixture metadata missing; skipping RAG task seeding (run make seed-ragdb first)")
        return []
    gold = [str(item) for item in meta["gold_incident_ids"]]
    if not gold:
        raise SystemExit("rag_fixture_meta has no gold incidents; refusing to seed DBA-R02-001")
    payload = {
        "task_id": "DBA-R02-001",
        "goal": (
            f"Find the incident reports about {meta['query_text']} that affect version "
            f"{meta['version']}, excluding reports resolved before {meta['resolved_cutoff']}. "
            "Return all matching incidents."
        ),
        "submission": (
            f"Call submit_final_answer with exactly {len(gold)} tuples, one per matching incident in "
            "any order: [incident_id]."
        ),
        "category": "RAG",
        "task_type": "IS",
        "max_steps": 12,
        "query_timeout": 30,
        "row_limit": 50,
        "sqlalchemy_url": ragdb_url,
        "terminal_condition": {
            "expected_tuples": [[incident_id] for incident_id in gold],
            "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
        },
    }
    record = parse_task_payload(payload)
    store.delete(record.task_id)
    store.create(record)
    payloads = [payload]

    r03_expected = str(meta["r03_expected_answer"] or "")
    r03_gold = [str(item) for item in meta["r03_gold_incident_ids"] or []]
    if not r03_expected or not r03_gold:
        print("rag fixture metadata has no r03_task; skipping DBA-R03-001")
        return payloads
    r03 = {
        "task_id": "DBA-R03-001",
        "goal": (
            f"Based on the incident reports, {meta['r03_question']}. "
            "Cite the incident ids you relied on."
        ),
        "submission": (
            "Call submit_final_answer with the 'answer' argument: a natural-language explanation "
            "that cites the incident ids you relied on."
        ),
        "category": "RAG",
        "task_type": "IS",
        "max_steps": 12,
        "query_timeout": 30,
        "row_limit": 50,
        "sqlalchemy_url": ragdb_url,
        "terminal_condition": {
            "judge": {"expected": r03_expected},
        },
    }
    r03_record = parse_task_payload(r03)
    store.delete(r03_record.task_id)
    store.create(r03_record)
    payloads.append(r03)

    for variant in variants:
        gold = [str(item) for item in variant["gold_incident_ids"]]
        if not gold:
            continue
        filters = variant["filters"] or {}
        key_points = [str(point) for point in variant["key_points"] or []]
        reward_mode = variant["reward_mode"] or "sparse"
        filter_clauses = []
        if "status" in filters:
            filter_clauses.append(f"status '{filters['status']}'")
        if "version" in filters:
            filter_clauses.append(f"version {filters['version']}")
        if "versions" in filters:
            filter_clauses.append("versions " + " and ".join(filters["versions"]))
        filter_text = (", filtered to " + ", ".join(filter_clauses)) if filter_clauses else ""
        if variant["kind"] == "retrieval":
            variant_payload = {
                "task_id": variant["task_id"],
                "goal": f"{variant['question'].rstrip('.')}. Return all matching incidents{filter_text}.",
                "submission": (
                    f"Call submit_final_answer with exactly {len(gold)} tuples, one per matching "
                    "incident in any order: [incident_id]."
                ),
                "category": "RAG",
                "task_type": "IS",
                "max_steps": 12,
                "query_timeout": 30,
                "row_limit": 50,
                "sqlalchemy_url": ragdb_url,
                "terminal_condition": {
                    "expected_tuples": [[incident_id] for incident_id in gold],
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                },
            }
        else:
            judge_block = {"expected": variant["expected_answer"]}
            if key_points:
                judge_block["key_points"] = key_points
            if reward_mode != "sparse":
                judge_block["reward_mode"] = reward_mode
            variant_payload = {
                "task_id": variant["task_id"],
                "goal": f"{variant['question'].rstrip('.')}. Cite the incident ids you relied on.",
                "submission": (
                    "Call submit_final_answer with the 'answer' argument: a natural-language "
                    "explanation that cites the incident ids you relied on."
                ),
                "category": "RAG",
                "task_type": "IS",
                "max_steps": 12,
                "query_timeout": 30,
                "row_limit": 50,
                "sqlalchemy_url": ragdb_url,
                "terminal_condition": {
                    "judge": judge_block,
                },
            }
        variant_record = parse_task_payload(variant_payload)
        store.delete(variant_record.task_id)
        store.create(variant_record)
        payloads.append(variant_payload)
    return payloads


def seed_tasks(
    admin_url: str, taskdb_url: str, olapdb_url: str, dsdb_url: str, ragdb_url: str
) -> list[dict]:
    taskdb = create_engine(taskdb_url)
    olapdb = create_engine(olapdb_url)
    dsdb = create_engine(dsdb_url)
    admin = create_engine(admin_url)
    try:
        store = TaskCatalogStore(admin)
        for task_id in RETIRED_TASK_IDS:
            if store.delete(task_id):
                print("retired", task_id)
        seeded = []
        for seed in TASK_SEEDS:
            params = resolve_params(taskdb, seed.get("params"))
            expected = compute_expected_tuples(taskdb, seed["trusted_sql"].format(**params))
            payload = dict(seed["payload"])
            payload["goal"] = payload["goal"].format(**params)
            payload["sqlalchemy_url"] = taskdb_url
            payload["terminal_condition"] = {
                "expected_tuples": expected,
                "matching": seed["matching"],
            }
            if not expected:
                raise SystemExit(
                    f"{payload['task_id']}: trusted oracle returned no rows for params {params}; "
                    "refusing to seed a task with an empty expectation"
                )
            record = parse_task_payload(payload)
            store.delete(record.task_id)
            store.create(record)
            seeded.append(payload)
        for seed in STATE_SEEDS:
            params = resolve_params(taskdb, seed.get("params"))
            reference = _render_reference(seed["reference"], params)
            confirmation, checks, rules = compute_state_expectations(taskdb, reference)
            payload = dict(seed["payload"])
            payload["goal"] = payload["goal"].format(**params)
            payload["sqlalchemy_url"] = taskdb_url
            if not confirmation:
                raise SystemExit(
                    f"{payload['task_id']}: confirmation oracle returned no rows for params {params}; "
                    "refusing to seed a task with an empty expectation"
                )
            empty = [check["sql"] for check in checks if not check["expected_tuples"]]
            if empty:
                raise SystemExit(
                    f"{payload['task_id']}: state checks returned no rows for params {params}: {empty}; "
                    "refusing to seed a task with an empty expectation"
                )
            broken = [
                f"rule {rule['position']}: {rule['sql']}" for rule in rules if not rule["passed"]
            ]
            if broken:
                raise SystemExit(
                    f"{payload['task_id']}: rules failed under the reference execution for params "
                    f"{params}: {broken}; refusing to seed an impossible oracle"
                )
            payload["terminal_condition"] = {
                "expected_tuples": confirmation,
                "matching": dict(seed["reference"].get("matching", {})),
                "state_checks": checks,
            }
            if rules:
                payload["terminal_condition"]["rules"] = [
                    {k: v for k, v in rule.items() if k != "passed"} for rule in rules
                ]
            record = parse_task_payload(payload)
            store.delete(record.task_id)
            store.create(record)
            seeded.append(payload)
        seeded.extend(seed_analytics_tasks(store, olapdb, olapdb_url))
        seeded.extend(seed_warehouse_tasks(store, dsdb, dsdb_url))
        seeded.extend(seed_retail_tasks(store, dsdb, dsdb_url))
        seeded.extend(seed_rag_tasks(store, admin, ragdb_url))
        return seeded
    finally:
        taskdb.dispose()
        olapdb.dispose()
        dsdb.dispose()
        admin.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed starter DBArena tasks with trusted oracles")
    parser.add_argument("--admin-url", default=os.environ.get("ADMINDB_URL"))
    parser.add_argument("--taskdb-url", default=os.environ.get("TASKDB_URL"))
    parser.add_argument("--olapdb-url", default=os.environ.get("OLAPDB_URL"))
    parser.add_argument("--dsdb-url", default=os.environ.get("DSDB_URL"))
    parser.add_argument("--ragdb-url", default=os.environ.get("RAGDB_URL"))
    parser.add_argument("--dry-run", action="store_true", help="print payloads without writing")
    args = parser.parse_args()
    if not args.admin_url or not args.taskdb_url or not args.olapdb_url or not args.dsdb_url:
        parser.error(
            "admin, taskdb, olapdb, and dsdb URLs are required "
            "(arguments or ADMINDB_URL/TASKDB_URL/OLAPDB_URL/DSDB_URL)"
        )
    if args.dry_run:
        taskdb = create_engine(args.taskdb_url)
        olapdb = create_engine(args.olapdb_url)
        dsdb = create_engine(args.dsdb_url)
        try:
            for seed in TASK_SEEDS:
                params = resolve_params(taskdb, seed.get("params"))
                expected = compute_expected_tuples(taskdb, seed["trusted_sql"].format(**params))
                print(seed["payload"]["task_id"], "params:", params, "expected tuples:", expected)
            for seed in STATE_SEEDS:
                params = resolve_params(taskdb, seed.get("params"))
                reference = _render_reference(seed["reference"], params)
                confirmation, checks, rules = compute_state_expectations(taskdb, reference)
                print(
                    seed["payload"]["task_id"],
                    "params:",
                    params,
                    "confirmation:",
                    confirmation,
                    "state checks:",
                    [(check["sql"], check["expected_tuples"]) for check in checks],
                    "rules:",
                    [(rule.get("description"), rule["passed"]) for rule in rules],
                )
            for seed in ANALYTICS_SEEDS:
                params = resolve_params(olapdb, seed.get("params"))
                expected = compute_expected_tuples(olapdb, seed["trusted_sql"].format(**params))
                print(seed["payload"]["task_id"], "params:", params, "expected tuples:", expected)
            for seed in ANALYTICS_STATE_SEEDS:
                params = resolve_params(olapdb, seed.get("params"))
                reference = _render_reference(seed["reference"], params)
                confirmation, checks, rules = compute_state_expectations(olapdb, reference)
                print(
                    seed["payload"]["task_id"],
                    "params:",
                    params,
                    "confirmation:",
                    confirmation,
                    "state checks:",
                    [(check["sql"], check["expected_tuples"][:3], len(check["expected_tuples"])) for check in checks],
                    "rules:",
                    [(rule.get("description"), rule["passed"]) for rule in rules],
                )
            for seed in WAREHOUSE_SEEDS:
                params = resolve_params(dsdb, seed.get("params"))
                reference = _render_reference(seed["reference"], params)
                confirmation, checks, rules = compute_state_expectations(dsdb, reference)
                print(
                    seed["payload"]["task_id"],
                    "params:",
                    params,
                    "confirmation:",
                    confirmation,
                    "state checks:",
                    [(check["sql"], check["expected_tuples"][:3], len(check["expected_tuples"])) for check in checks],
                    "rules:",
                    [(rule.get("description"), rule["passed"]) for rule in rules],
                )
            for seed in DSDB_SEEDS:
                params = resolve_params(dsdb, seed.get("params"))
                expected = compute_expected_tuples(dsdb, seed["trusted_sql"].format(**params))
                print(seed["payload"]["task_id"], "params:", params, "expected tuples:", expected)
            for seed in DSDB_STATE_SEEDS:
                params = resolve_params(dsdb, seed.get("params"))
                reference = _render_reference(seed["reference"], params)
                confirmation, checks, rules = compute_state_expectations(dsdb, reference)
                print(
                    seed["payload"]["task_id"],
                    "params:",
                    params,
                    "confirmation:",
                    confirmation,
                    "state checks:",
                    [(check["sql"], check["expected_tuples"][:3], len(check["expected_tuples"])) for check in checks],
                    "rules:",
                    [(rule.get("description"), rule["passed"]) for rule in rules],
                )
            return 0
        finally:
            taskdb.dispose()
            olapdb.dispose()
            dsdb.dispose()
    seeded = seed_tasks(
        args.admin_url, args.taskdb_url, args.olapdb_url, args.dsdb_url, args.ragdb_url
    )
    for payload in seeded:
        print("seeded", payload["task_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
