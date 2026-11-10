"""Install the baked TPC-H fixture into the olapdb volume (idempotent).

The image carries a pre-generated TPC-H SF 0.01 DuckDB file (see Dockerfile).
At runtime this script copies it to the target path on the volume — atomically,
via a temp file + rename, so a target file is never seen partially written —
and verifies the TPC-H cardinalities. Re-runs against a seeded file are no-ops.
"""

import os
import shutil
import sys

import duckdb

FIXTURE = "/opt/fixture.duckdb"
TARGET = os.environ.get("OLAPDB_PATH", "/data/olap-tpch.duckdb")

EXACT_CARDINALITIES = {
    "region": 5,
    "nation": 25,
    "supplier": 100,
    "customer": 1500,
    "part": 2000,
    "partsupp": 8000,
    "orders": 15000,
}
MIN_LINEITEM_ROWS = 60_000  # dbgen emits ~60,175 order lines at SF 0.01


def seeded(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        conn = duckdb.connect(path, read_only=True)
        try:
            return conn.execute("SELECT count(*) FROM lineitem").fetchone()[0] > 0
        finally:
            conn.close()
    except Exception:
        return False


def main() -> int:
    if seeded(TARGET):
        print("[SEED] TPC-H fixture already present; nothing to do.")
        return 0
    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    tmp = TARGET + ".tmp"
    shutil.copyfile(FIXTURE, tmp)
    os.replace(tmp, TARGET)

    conn = duckdb.connect(TARGET, read_only=True)
    try:
        counts = {}
        for table, expected in EXACT_CARDINALITIES.items():
            counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if counts[table] != expected:
                print(f"[SEED] FAILED: {table} has {counts[table]} rows, expected {expected}")
                return 1
        counts["lineitem"] = conn.execute("SELECT count(*) FROM lineitem").fetchone()[0]
        if counts["lineitem"] < MIN_LINEITEM_ROWS:
            print(f"[SEED] FAILED: lineitem has {counts['lineitem']} rows, expected ~60175")
            return 1
    finally:
        conn.close()
    print("[SEED] Done (cardinalities verified):", " ".join(f"{t}={n}" for t, n in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
