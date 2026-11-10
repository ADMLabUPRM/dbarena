"""Generate the TPC-DS retail fixture at build time (change add-tpcds-fixture-and-tasks).

Runs in the initializer image build: the pinned DuckDB TPC-DS extension generates
the SF 0.01 dataset, deterministic for a pinned extension release. The fixture is
exactly the dsdgen output - no extension tables are added (decided in the
add-warehouse-tasks change: the catalog runs on TPC-DS as it comes). The compose
runtime network is offline, so generation and the extension download happen here,
at build time.

The output path defaults to /opt/fixture.duckdb (baked into the image) and can be
overridden with DSDB_GEN_PATH (used by the test suite to build scratch fixtures).
"""

import os

import duckdb


def main() -> None:
    fixture = os.environ.get("DSDB_GEN_PATH", "/opt/fixture.duckdb")
    conn = duckdb.connect(fixture)
    try:
        conn.execute("INSTALL tpcds")
        conn.execute("LOAD tpcds")
        conn.execute("CALL dsdgen(sf = 0.01)")
        print(f"[GEN] store_sales = {conn.execute('SELECT count(*) FROM store_sales').fetchone()[0]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
