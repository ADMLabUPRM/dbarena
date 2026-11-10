import importlib.util
import os
import pathlib

import pytest
from sqlalchemy import create_engine

from dbarena.catalog.records import TaskRecord
from dbarena.catalog.validation import validate_task_record


def _load_module(name: str, relative: str):
    path = pathlib.Path(__file__).resolve().parents[1] / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_tasks = _load_module("seed_tasks_under_test", os.path.join("db", "seed_tasks.py"))
generate_tpcds = _load_module(
    "generate_tpcds_under_test",
    os.path.join("db", "initializer-duckdb-tpcds", "generate.py"),
)

DSDB_SEEDS = seed_tasks.DSDB_SEEDS
DSDB_STATE_SEEDS = seed_tasks.DSDB_STATE_SEEDS
WAREHOUSE_SEEDS = seed_tasks.WAREHOUSE_SEEDS
seed_retail_tasks = seed_tasks.seed_retail_tasks
seed_warehouse_tasks = seed_tasks.seed_warehouse_tasks

DSDB_URL = "duckdb:////ds/tpcds.duckdb"


class RecordingStore:
    def __init__(self):
        self.created: list[TaskRecord] = []

    def delete(self, task_id: str) -> bool:
        return False

    def create(self, record: TaskRecord) -> TaskRecord:
        self.created.append(record)
        return record


@pytest.fixture(scope="module")
def dsdb(tmp_path_factory):
    path = os.path.join(str(tmp_path_factory.mktemp("dsdb")), "fixture.duckdb")
    os.environ["DSDB_GEN_PATH"] = path
    try:
        generate_tpcds.main()
    finally:
        os.environ.pop("DSDB_GEN_PATH", None)
    return create_engine(f"duckdb:///{path}")


def _record(store: RecordingStore, task_id: str) -> TaskRecord:
    return [r for r in store.created if r.task_id == task_id][0]


def test_seeds_all_five_retail_tasks(dsdb):
    store = RecordingStore()
    seeded = seed_retail_tasks(store, dsdb, DSDB_URL)
    assert [payload["task_id"] for payload in seeded] == [
        "DBA-A05-001",
        "DBA-A06-001",
        "DBA-A07-001",
        "DBA-A08-001",
        "DBA-W02-001",
    ]
    types = {r.task_id: r.task_type for r in store.created}
    for task_id in ("DBA-A05-001", "DBA-A06-001", "DBA-A07-001", "DBA-A08-001"):
        assert types[task_id] == "IS"
        assert _record(store, task_id).category == "ANALYTICS"
    assert types["DBA-W02-001"] == "CC"
    assert _record(store, "DBA-W02-001").category == "OLAP"
    for record in store.created:
        assert record.sqlalchemy_url == DSDB_URL
        assert validate_task_record(record) == []


def test_a05_item_sales_report_with_class_contribution(dsdb):
    store = RecordingStore()
    seed_retail_tasks(store, dsdb, DSDB_URL)
    record = _record(store, "DBA-A05-001")
    expected = record.terminal_condition.expected_tuples
    assert len(expected) == 10
    assert expected[0] == ["Women", "dresses", "AAAAAAAACAAAAAAA", "15930.99", 100.0]
    assert expected[1] == ["Books", "travel", "AAAAAAAALCAAAAAA", "15012.57", 74.62]
    revenues = [float(row[3]) for row in expected]
    assert revenues == sorted(revenues, reverse=True)


def test_a06_contributions_reconcile_to_overall(dsdb):
    store = RecordingStore()
    seed_retail_tasks(store, dsdb, DSDB_URL)
    record = _record(store, "DBA-A06-001")
    expected = record.terminal_condition.expected_tuples
    assert len(expected) == 11
    assert expected[0] == ["Women", "-48350.41"]
    assert expected[-1][0] == "ALL"
    category_sum = round(sum(float(row[1]) for row in expected[:-1]), 2)
    assert category_sum == float(expected[-1][1])


def test_a07_cohort_repeat_rates(dsdb):
    store = RecordingStore()
    seed_retail_tasks(store, dsdb, DSDB_URL)
    record = _record(store, "DBA-A07-001")
    assert record.terminal_condition.expected_tuples == [
        ["1998-09", 53, 4, 7.55],
        ["1998-10", 38, 4, 10.53],
    ]


def test_a08_experiment_comparison(dsdb):
    store = RecordingStore()
    seed_retail_tasks(store, dsdb, DSDB_URL)
    record = _record(store, "DBA-A08-001")
    assert record.terminal_condition.expected_tuples == [
        [73, 11, 15.07, 157, 19, 12.1, 2.97, 24.51]
    ]


def test_w02_summary_confirmation_and_rules(dsdb):
    store = RecordingStore()
    seed_retail_tasks(store, dsdb, DSDB_URL)
    record = _record(store, "DBA-W02-001")
    assert record.terminal_condition.expected_tuples == [[53, "92578.31", "9044.62", "83533.69"]]
    checks = record.terminal_condition.state_checks
    assert list(checks[1].expected_tuples) == [[53]]
    assert len(checks[0].expected_tuples) == 53
    rules = record.terminal_condition.rules
    assert len(rules) == 6
    assert len(record.terminal_condition.expected_tuples[0]) == 4


def test_seeds_all_five_warehouse_tasks(dsdb):
    store = RecordingStore()
    seeded = seed_warehouse_tasks(store, dsdb, DSDB_URL)
    assert [payload["task_id"] for payload in seeded] == [
        "DBA-W01-001",
        "DBA-W03-001",
        "DBA-W04-001",
        "DBA-W05-001",
        "DBA-W06-001",
    ]
    types = {r.task_id: r.task_type for r in store.created}
    assert types["DBA-W01-001"] == "CC" and types["DBA-W03-001"] == "CC" and types["DBA-W04-001"] == "CC"
    assert types["DBA-W05-001"] == "MIX" and types["DBA-W06-001"] == "MIX"
    for record in store.created:
        assert record.category == "OLAP"
        assert record.sqlalchemy_url == DSDB_URL
        assert validate_task_record(record) == []


def test_w01_registration_partitions_the_batch(dsdb):
    store = RecordingStore()
    seed_warehouse_tasks(store, dsdb, DSDB_URL)
    record = _record(store, "DBA-W01-001")
    assert record.terminal_condition.expected_tuples == [[791, 14]]
    checks = record.terminal_condition.state_checks
    quarantined = checks[0].expected_tuples
    assert len(quarantined) == 14
    assert all(row[2] == "missing_customer_identifier" for row in quarantined)
    assert list(checks[1].expected_tuples) == [[791]]


def test_w03_backfill_counts(dsdb):
    store = RecordingStore()
    seed_warehouse_tasks(store, dsdb, DSDB_URL)
    record = _record(store, "DBA-W03-001")
    assert record.terminal_condition.expected_tuples == [[222, "475739.72"]]
    checks = record.terminal_condition.state_checks
    assert len(checks[0].expected_tuples) == 17
    assert checks[0].expected_tuples[0] == ["1998-01-02", "12361.35"]
    assert list(checks[1].expected_tuples) == [[222]]
    assert len(record.terminal_condition.rules) == 5


def test_w04_scd2_versions(dsdb):
    store = RecordingStore()
    seed_warehouse_tasks(store, dsdb, DSDB_URL)
    record = _record(store, "DBA-W04-001")
    assert record.terminal_condition.expected_tuples == [[6, 23]]
    checks = record.terminal_condition.state_checks
    versions = checks[0].expected_tuples
    assert len(versions) == 23
    assert versions[0] == [43, "139.23", "1998-01-06", "1998-01-06"]
    assert list(checks[1].expected_tuples) == [[23]]
    assert len(record.terminal_condition.rules) == 5


def test_w05_recovery_completes_the_batch(dsdb):
    store = RecordingStore()
    seed_warehouse_tasks(store, dsdb, DSDB_URL)
    record = _record(store, "DBA-W05-001")
    assert record.terminal_condition.expected_tuples == [[17, "421068.16", "1998-01-30"]]
    checks = record.terminal_condition.state_checks
    assert len(checks[0].expected_tuples) == 17
    assert checks[0].expected_tuples[0] == ["1998-01-02", "10708.38"]
    assert list(checks[1].expected_tuples) == [["revenue_daily", "1998-01-30"]]
    assert len(record.terminal_condition.rules) == 5


def test_w06_reconciliation(dsdb):
    store = RecordingStore()
    seed_warehouse_tasks(store, dsdb, DSDB_URL)
    record = _record(store, "DBA-W06-001")
    assert record.terminal_condition.expected_tuples == [[15, "20788.75"]]
    checks = record.terminal_condition.state_checks
    assert len(checks[0].expected_tuples) == 15
    assert checks[0].expected_tuples[0] == ["1998-01-02", "129.22"]
    assert list(checks[1].expected_tuples) == [[15]]
    assert len(record.terminal_condition.rules) == 5


def test_empty_trusted_oracle_refuses_seeding(tmp_path, monkeypatch):
    path = os.path.join(str(tmp_path), "fixture.duckdb")
    os.environ["DSDB_GEN_PATH"] = path
    try:
        generate_tpcds.main()
    finally:
        os.environ.pop("DSDB_GEN_PATH", None)
    a05 = [seed for seed in DSDB_SEEDS if seed["payload"]["task_id"] == "DBA-A05-001"][0]
    empty = dict(
        a05,
        params={
            **a05["params"],
            "start_date": "SELECT DATE '2020-01-01'",
            "end_date": "SELECT DATE '2020-01-02'",
        },
    )
    monkeypatch.setattr(seed_tasks, "DSDB_SEEDS", [empty])
    with pytest.raises(SystemExit, match="empty expectation"):
        seed_retail_tasks(
            RecordingStore(), create_engine(f"duckdb:///{path}"), DSDB_URL
        )


def test_wrong_row_count_refuses_seeding(tmp_path, monkeypatch):
    path = os.path.join(str(tmp_path), "fixture.duckdb")
    os.environ["DSDB_GEN_PATH"] = path
    try:
        generate_tpcds.main()
    finally:
        os.environ.pop("DSDB_GEN_PATH", None)
    a07 = [seed for seed in DSDB_SEEDS if seed["payload"]["task_id"] == "DBA-A07-001"][0]
    narrowed = dict(a07, expected_row_count=3)
    monkeypatch.setattr(seed_tasks, "DSDB_SEEDS", [narrowed])
    with pytest.raises(SystemExit, match="expected 3 for params"):
        seed_retail_tasks(
            RecordingStore(), create_engine(f"duckdb:///{path}"), DSDB_URL
        )


def test_failing_rule_refuses_seeding(tmp_path, monkeypatch):
    path = os.path.join(str(tmp_path), "fixture.duckdb")
    os.environ["DSDB_GEN_PATH"] = path
    try:
        generate_tpcds.main()
    finally:
        os.environ.pop("DSDB_GEN_PATH", None)
    w02 = [seed for seed in DSDB_STATE_SEEDS if seed["payload"]["task_id"] == "DBA-W02-001"][0]
    broken = dict(
        w02,
        reference={
            **w02["reference"],
            "rules": [{"description": "impossible", "sql": "SELECT 1 = 2"}],
        },
    )
    monkeypatch.setattr(seed_tasks, "DSDB_STATE_SEEDS", [broken])
    with pytest.raises(SystemExit, match="impossible oracle"):
        seed_retail_tasks(
            RecordingStore(), create_engine(f"duckdb:///{path}"), DSDB_URL
        )


def test_failing_warehouse_rule_refuses_seeding(tmp_path, monkeypatch):
    path = os.path.join(str(tmp_path), "fixture.duckdb")
    os.environ["DSDB_GEN_PATH"] = path
    try:
        generate_tpcds.main()
    finally:
        os.environ.pop("DSDB_GEN_PATH", None)
    w06 = [seed for seed in WAREHOUSE_SEEDS if seed["payload"]["task_id"] == "DBA-W06-001"][0]
    broken = dict(
        w06,
        reference={
            **w06["reference"],
            "rules": [{"description": "impossible", "sql": "SELECT 1 = 2"}],
        },
    )
    monkeypatch.setattr(seed_tasks, "WAREHOUSE_SEEDS", [broken])
    with pytest.raises(SystemExit, match="impossible oracle"):
        seed_warehouse_tasks(
            RecordingStore(), create_engine(f"duckdb:///{path}"), DSDB_URL
        )
