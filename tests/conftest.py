import os
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError

SEED_SQL = """
DROP TABLE IF EXISTS arena_item;
CREATE TABLE arena_item (
    id bigint PRIMARY KEY,
    name character varying(25) NOT NULL,
    amount numeric(15,2),
    created date
);
INSERT INTO arena_item (id, name, amount, created) VALUES
    (1, 'alpha', 10.50, DATE '1995-09-01'),
    (2, 'beta', NULL, DATE '1995-09-02'),
    (3, 'gamma', 42.00, DATE '1995-09-03');
"""

TEST_DB = "dbarena_test"


def _database_url(server_url: str, database: str) -> str:
    return make_url(server_url).set(database=database).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def pg_url() -> str:
    server_url = os.environ.get("TEST_PG_URL")
    if not server_url:
        pytest.skip("TEST_PG_URL not set; PostgreSQL integration tests are skipped")
    admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
    for _ in range(60):
        try:
            with admin.connect() as conn:
                conn.execute(text("SELECT 1"))
            break
        except OperationalError:
            time.sleep(1.0)
    else:
        admin.dispose()
        pytest.skip("PostgreSQL test server not reachable")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB}"))
        conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    admin.dispose()
    test_url = _database_url(server_url, TEST_DB)
    engine = create_engine(test_url)
    with engine.begin() as conn:
        conn.execute(text(SEED_SQL))
    yield test_url
    engine.dispose()
    cleanup = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with cleanup.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
    cleanup.dispose()


@pytest.fixture(scope="session")
def pg_engine(pg_url: str):
    return create_engine(pg_url)
