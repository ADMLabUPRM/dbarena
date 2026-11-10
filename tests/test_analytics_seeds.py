import importlib.util
import os
import pathlib

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dbarena.catalog.records import TaskRecord
from dbarena.catalog.validation import validate_task_record


def _load_seed_tasks():
    path = pathlib.Path(__file__).resolve().parents[1] / "db" / "seed_tasks.py"
    spec = importlib.util.spec_from_file_location("seed_tasks_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_tasks = _load_seed_tasks()
seed_analytics_tasks = seed_tasks.seed_analytics_tasks
ANALYTICS_SEEDS = seed_tasks.ANALYTICS_SEEDS
ANALYTICS_STATE_SEEDS = seed_tasks.ANALYTICS_STATE_SEEDS

SEED_SQL = """
CREATE TABLE customer (c_custkey bigint PRIMARY KEY, c_mktsegment varchar, c_acctbal decimal(15,2));
CREATE TABLE orders (
    o_orderkey bigint PRIMARY KEY,
    o_custkey bigint,
    o_orderstatus varchar,
    o_orderdate date,
    o_shippriority integer
);
CREATE TABLE part (p_partkey bigint PRIMARY KEY, p_type varchar);
CREATE TABLE lineitem (
    l_orderkey bigint,
    l_linenumber bigint,
    l_partkey bigint,
    l_extendedprice decimal(15,2),
    l_discount decimal(15,2),
    l_quantity integer,
    l_shipdate date
);
-- ten BUILDING customers with open orders before the pinned 1995-07-15 cutoff
INSERT INTO customer SELECT i, 'BUILDING', 1000.00 FROM range(1, 11) t(i);
INSERT INTO orders
    SELECT i, i, 'O', DATE '1995-06-01' + INTERVAL (i) DAY, 0 FROM range(1, 11) t(i);
INSERT INTO lineitem
    SELECT i, 1, 1, 1000.00 + i, 0.05, 10, DATE '1995-06-01' + INTERVAL (i) DAY
    FROM range(1, 11) t(i);
-- September 1995 shipments: one promo line and one regular line of equal revenue
INSERT INTO part VALUES (1, 'PROMO ANODIZED STEEL'), (2, 'STANDARD POLISHED TIN');
INSERT INTO lineitem VALUES (11, 1, 1, 1000.00, 0.10, 10, DATE '1995-09-05');
INSERT INTO lineitem VALUES (12, 1, 2, 1000.00, 0.10, 10, DATE '1995-09-06');
-- 1994 shipment for the Q6 window
INSERT INTO lineitem VALUES (13, 1, 1, 1000.00, 0.06, 10, DATE '1994-03-01');
-- the remaining TPC-H tables (the NAV oracles count rows across all eight)
CREATE TABLE nation (n_nationkey bigint PRIMARY KEY, n_name varchar, n_regionkey bigint, n_comment varchar);
CREATE TABLE region (r_regionkey bigint PRIMARY KEY, r_name varchar, r_comment varchar);
CREATE TABLE partsupp (ps_partkey bigint, ps_suppkey bigint, ps_availqty integer);
CREATE TABLE supplier (s_suppkey bigint PRIMARY KEY, s_name varchar);
INSERT INTO nation VALUES (1, 'ALGERIA', 0, 'haggle'), (2, 'ARGENTINA', 1, 'foxes');
INSERT INTO region VALUES (0, 'AFRICA', 'deposits'), (1, 'AMERICA', 'uses');
INSERT INTO partsupp VALUES (1, 1, 500);
INSERT INTO supplier VALUES (1, 'Supplier#000000001');
"""


class RecordingStore:
    def __init__(self):
        self.created: list[TaskRecord] = []

    def delete(self, task_id: str) -> bool:
        return False

    def create(self, record: TaskRecord) -> TaskRecord:
        self.created.append(record)
        return record


@pytest.fixture(scope="module")
def analyticsdb_url(tmp_path_factory) -> str:
    path = os.path.join(tmp_path_factory.mktemp("analyticsseed"), "fixture.duckdb")
    engine = create_engine(f"duckdb:///{path}")
    with engine.begin() as conn:
        for statement in SEED_SQL.split(";"):
            if statement.strip():
                conn.execute(text(statement))
    engine.dispose()
    return f"duckdb:///{path}"


@pytest.fixture(scope="module")
def analyticsdb(analyticsdb_url: str) -> Engine:
    return create_engine(analyticsdb_url)


def test_seeds_all_three_analytics_tasks(analyticsdb, analyticsdb_url):
    store = RecordingStore()
    seeded = seed_analytics_tasks(store, analyticsdb, analyticsdb_url)
    assert [payload["task_id"] for payload in seeded][:3] == [
        "DBA-A02-001",
        "DBA-A03-001",
        "DBA-A04-001",
    ]
    trusted = [r for r in store.created if r.task_id in ("DBA-A02-001", "DBA-A03-001", "DBA-A04-001")]
    assert len(trusted) == 3
    for record in trusted:
        assert record.category == "ANALYTICS"
        assert record.task_type == "IS"
        assert record.sqlalchemy_url == analyticsdb_url
        assert validate_task_record(record) == []


def test_a02_ranks_ten_unshipped_orders(analyticsdb, analyticsdb_url):
    store = RecordingStore()
    seed_analytics_tasks(store, analyticsdb, analyticsdb_url)
    record = [r for r in store.created if r.task_id == "DBA-A02-001"][0]
    tuples = record.terminal_condition.expected_tuples
    assert len(tuples) == 10
    revenues = [float(t[1]) for t in tuples]
    assert revenues == sorted(revenues, reverse=True)
    assert all(t[2] < "1995-07-15" for t in tuples)


def test_a03_promo_percentage(analyticsdb, analyticsdb_url):
    store = RecordingStore()
    seed_analytics_tasks(store, analyticsdb, analyticsdb_url)
    record = [r for r in store.created if r.task_id == "DBA-A03-001"][0]
    # one promo line and one regular line, equal revenue: 50.00 percent
    assert record.terminal_condition.expected_tuples == [[50.0]]


def test_a04_recovered_revenue(analyticsdb, analyticsdb_url):
    store = RecordingStore()
    seed_analytics_tasks(store, analyticsdb, analyticsdb_url)
    record = [r for r in store.created if r.task_id == "DBA-A04-001"][0]
    # round() over a DECIMAL sum stays DECIMAL in DuckDB, so _cell renders it as a string
    assert record.terminal_condition.expected_tuples == [["60.00"]]


def test_empty_oracle_refuses_seeding(analyticsdb, analyticsdb_url, monkeypatch):
    a02 = [seed for seed in ANALYTICS_SEEDS if seed["payload"]["task_id"] == "DBA-A02-001"][0]
    # a cutoff before every order in the mini fixture empties the oracle
    monkeypatch.setattr(
        seed_tasks,
        "ANALYTICS_SEEDS",
        [dict(a02, params={"segment": "SELECT 'BUILDING'", "cutoff_date": "SELECT DATE '1990-01-01'"})],
    )
    with pytest.raises(SystemExit, match="empty expectation"):
        seed_analytics_tasks(RecordingStore(), analyticsdb, analyticsdb_url)


def _state_seeds_with(threshold="SELECT 500"):
    """ANALYTICS_STATE_SEEDS with the threshold lowered to fit the mini fixture."""
    return [
        dict(
            seed,
            params={**seed["params"], **({"threshold": threshold} if "threshold" in seed["params"] else {})},
        )
        for seed in ANALYTICS_STATE_SEEDS
    ]


@pytest.fixture(autouse=True)
def mini_state_seeds(monkeypatch):
    # the real A12 threshold empties on the mini fixture; default every test
    # in this module to the lowered threshold unless it overrides the seeds
    monkeypatch.setattr(seed_tasks, "ANALYTICS_STATE_SEEDS", _state_seeds_with())


def test_seeds_all_six_analytics_tasks(analyticsdb, analyticsdb_url):
    store = RecordingStore()
    seeded = seed_analytics_tasks(store, analyticsdb, analyticsdb_url)
    assert [payload["task_id"] for payload in seeded] == [
        "DBA-A02-001",
        "DBA-A03-001",
        "DBA-A04-001",
        "DBA-A01-001",
        "DBA-A10-001",
        "DBA-A11-001",
        "DBA-A09-001",
        "DBA-A12-001",
        "DBA-A13-001",
    ]
    types = {r.task_id: r.task_type for r in store.created}
    assert types["DBA-A01-001"] == "NAV" and types["DBA-A10-001"] == "NAV" and types["DBA-A11-001"] == "NAV"
    assert types["DBA-A09-001"] == "CC" and types["DBA-A12-001"] == "CC" and types["DBA-A13-001"] == "MIX"
    for record in store.created:
        assert record.category == "ANALYTICS"
        assert record.sqlalchemy_url == analyticsdb_url
        assert validate_task_record(record) == []


def test_a09_summary_confirmation(analyticsdb, analyticsdb_url):
    store = RecordingStore()
    seed_analytics_tasks(store, analyticsdb, analyticsdb_url)
    record = [r for r in store.created if r.task_id == "DBA-A09-001"][0]
    # two September ship dates in the mini fixture, one 900.00 revenue line each
    assert record.terminal_condition.expected_tuples == [[2, "1800.00"]]
    checks = record.terminal_condition.state_checks
    assert list(checks[0].expected_tuples) == [
        ["1995-09-05", "900.00"],
        ["1995-09-06", "900.00"],
    ]


def test_a12_threshold_segment_content(analyticsdb, analyticsdb_url):
    store = RecordingStore()
    seed_analytics_tasks(store, analyticsdb, analyticsdb_url)
    record = [r for r in store.created if r.task_id == "DBA-A12-001"][0]
    # every mini-fixture customer (revenue ~950-1005) clears the lowered threshold
    assert record.terminal_condition.expected_tuples == [[10]]
    assert len(record.terminal_condition.rules) == 3


def test_a13_top_five_ranking(analyticsdb, analyticsdb_url):
    store = RecordingStore()
    seed_analytics_tasks(store, analyticsdb, analyticsdb_url)
    record = [r for r in store.created if r.task_id == "DBA-A13-001"][0]
    tuples = record.terminal_condition.expected_tuples
    assert len(tuples) == 5
    assert [t[0] for t in tuples] == [10, 9, 8, 7, 6]
    revenues = [float(t[1]) for t in tuples]
    assert revenues == sorted(revenues, reverse=True)
    # the state check mirrors the ranked confirmation
    assert list(record.terminal_condition.state_checks[0].expected_tuples) == tuples


def test_empty_confirmation_refuses_seeding(analyticsdb, analyticsdb_url, monkeypatch):
    a09 = [seed for seed in ANALYTICS_STATE_SEEDS if seed["payload"]["task_id"] == "DBA-A09-001"][0]
    empty = [
        dict(
            a09,
            params={
                "summary_table": "SELECT 'ship_revenue_summary'",
                "month_start": "SELECT DATE '2100-01-01'",
                "month_end_exclusive": "SELECT DATE '2100-02-01'",
            },
        )
    ]
    monkeypatch.setattr(seed_tasks, "ANALYTICS_STATE_SEEDS", empty)
    with pytest.raises(SystemExit, match="empty expectation"):
        seed_analytics_tasks(RecordingStore(), analyticsdb, analyticsdb_url)


def test_empty_state_check_refuses_seeding(analyticsdb, analyticsdb_url, monkeypatch):
    a12 = [seed for seed in ANALYTICS_STATE_SEEDS if seed["payload"]["task_id"] == "DBA-A12-001"][0]
    narrowed = dict(
        a12,
        params={"segment_table": "SELECT 'high_value_customers'", "threshold": "SELECT 500"},
    )
    narrowed["reference"] = dict(
        a12["reference"],
        state_checks=[
            {
                "sql": "SELECT c_custkey, total_revenue FROM {segment_table} WHERE c_custkey > 99999",
                "matching": {"order_sensitive": True, "numeric_tolerance": 0.01, "null_handling": "exact"},
            }
        ],
    )
    monkeypatch.setattr(seed_tasks, "ANALYTICS_STATE_SEEDS", [narrowed])
    with pytest.raises(SystemExit, match="state checks returned no rows"):
        seed_analytics_tasks(RecordingStore(), analyticsdb, analyticsdb_url)


def test_failing_rule_refuses_seeding(analyticsdb, analyticsdb_url, monkeypatch):
    a12 = [seed for seed in ANALYTICS_STATE_SEEDS if seed["payload"]["task_id"] == "DBA-A12-001"][0]
    broken = dict(
        a12,
        params={"segment_table": "SELECT 'high_value_customers'", "threshold": "SELECT 500"},
    )
    broken["reference"] = dict(
        a12["reference"],
        rules=[{"description": "impossible", "sql": "SELECT 1 = 2"}],
    )
    monkeypatch.setattr(seed_tasks, "ANALYTICS_STATE_SEEDS", [broken])
    with pytest.raises(SystemExit, match="impossible oracle"):
        seed_analytics_tasks(RecordingStore(), analyticsdb, analyticsdb_url)
