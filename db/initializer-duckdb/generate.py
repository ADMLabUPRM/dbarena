"""Build-time TPC-H fixture generation for the DuckDB analytical database.

Runs inside the image build (network is available there for the `tpch`
extension download; the runtime compose network is internal). Generates
TPC-H SF 0.01 with DuckDB's dbgen and applies the TPC-H reference schema's
primary keys, which dbgen does not declare — agents inspect
`describe_table`, and the fixture should be schema-faithful.
"""

import duckdb

FIXTURE = "/opt/fixture.duckdb"

PRIMARY_KEYS = {
    "region": "(r_regionkey)",
    "nation": "(n_nationkey)",
    "supplier": "(s_suppkey)",
    "customer": "(c_custkey)",
    "part": "(p_partkey)",
    "partsupp": "(ps_partkey, ps_suppkey)",
    "orders": "(o_orderkey)",
    "lineitem": "(l_orderkey, l_linenumber)",
}


def main() -> None:
    conn = duckdb.connect(FIXTURE)
    try:
        conn.execute("INSTALL tpch")
        conn.execute("LOAD tpch")
        conn.execute("CALL dbgen(sf = 0.01)")
        for table, key in PRIMARY_KEYS.items():
            conn.execute(f"ALTER TABLE {table} ADD PRIMARY KEY {key}")
    finally:
        conn.close()
    print("[BUILD] TPC-H fixture generated with reference primary keys")


if __name__ == "__main__":
    main()
