"""Install the baked TPC-DS retail fixture into the dsdb volume (idempotent).

The image carries the TPC-DS SF 0.01 DuckDB file generated at build time (see
Dockerfile / generate.py). At runtime this script copies it to the target path on
the volume - atomically, via a temp file + rename - and verifies the exact table
cardinalities AND the total table count, so a partial, mismatched, or stale
superset fixture (e.g. one carrying removed extension tables) is refused and
replaced. Re-runs against a verified file are no-ops.
"""

import os
import shutil
import sys

import duckdb

FIXTURE = "/opt/fixture.duckdb"
TARGET = os.environ.get("DSDB_PATH", "/data/tpcds.duckdb")

# Exact cardinalities for the pinned TPC-DS SF 0.01 generation. The fixture is
# exactly the dsdgen output - the 24 benchmark tables, nothing else.
EXACT_CARDINALITIES = {
    "call_center": 1,
    "catalog_page": 11718,
    "catalog_returns": 1358,
    "catalog_sales": 14313,
    "customer": 1000,
    "customer_address": 500,
    "customer_demographics": 19208,
    "date_dim": 73049,
    "household_demographics": 7200,
    "income_band": 20,
    "inventory": 23490,
    "item": 180,
    "promotion": 3,
    "reason": 1,
    "ship_mode": 20,
    "store": 1,
    "store_returns": 2810,
    "store_sales": 28810,
    "time_dim": 86400,
    "warehouse": 1,
    "web_page": 1,
    "web_returns": 679,
    "web_sales": 7212,
    "web_site": 1,
}


def verify(conn: duckdb.DuckDBPyConnection) -> bool:
    """True when the fixture matches the pinned cardinalities and table count."""
    total_tables = conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchone()[0]
    if total_tables != len(EXACT_CARDINALITIES):
        print(
            f"[SEED] FAILED: fixture has {total_tables} tables, "
            f"expected exactly {len(EXACT_CARDINALITIES)} (stale superset?)"
        )
        return False
    for table, expected in EXACT_CARDINALITIES.items():
        actual = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if actual != expected:
            print(f"[SEED] FAILED: {table} has {actual} rows, expected {expected}")
            return False
    return True


def seeded(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        conn = duckdb.connect(path, read_only=True)
        try:
            return verify(conn)
        finally:
            conn.close()
    except Exception:
        return False


def main() -> int:
    if seeded(TARGET):
        print("[SEED] TPC-DS fixture already present; nothing to do.")
        return 0
    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    tmp = TARGET + ".tmp"
    shutil.copyfile(FIXTURE, tmp)
    os.replace(tmp, TARGET)

    conn = duckdb.connect(TARGET, read_only=True)
    try:
        if not verify(conn):
            return 1
        counts = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in EXACT_CARDINALITIES
        }
    finally:
        conn.close()
    print("[SEED] Done (cardinalities verified):", " ".join(f"{t}={n}" for t, n in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
