#!/bin/sh
set -e

# Legacy TPC-H tables (region, nation, supplier, part, partsupp, orders,
# lineitem) plus any partial TPC-C tables: dropped before a fresh load so a
# failed run never leaves mixed state. TPC-H's "customer"/"orders" and
# TPC-C's "customer" cannot coexist by name, so the swap is unconditional.
DROP_TABLES="region nation supplier part partsupp orders lineitem order_line new_order oorder history customer stock item district warehouse"

# Residue from pytest integration runs that pointed TEST_PG_URL at this
# database (conftest's arena_item seed plus the admin catalog DDL): never
# legitimate in taskdb, so dropped unconditionally on every seed run —
# including no-op runs — to keep the agent-visible schema TPC-C-only.
echo "[SEED] Dropping stray test/admin tables if present..."
for table in arena_item tasks episodes episode_steps; do
    psql -q -c "DROP TABLE IF EXISTS $table CASCADE"
done

CONSISTENCY_SQL="SELECT (SELECT count(*) FROM district) = 10 \
    AND (SELECT count(*) FROM item) = 100000 \
    AND (SELECT count(*) FROM stock) = 100000 \
    AND (SELECT count(*) FROM customer) = 30000 \
    AND (SELECT count(*) FROM history) = 30000 \
    AND (SELECT count(*) FROM oorder) = 30000 \
    AND (SELECT count(*) FROM new_order) = 9000 \
    AND (SELECT count(*) FROM order_line) = (SELECT sum(o_ol_cnt) FROM oorder) \
    AND (SELECT count(*) FROM oorder WHERE o_carrier_id IS NULL) = (SELECT count(*) FROM new_order)"

consistent=$(psql -tAc "$CONSISTENCY_SQL" 2>/dev/null || echo f)
if [ "$consistent" = "t" ]; then
    echo "[SEED] TPC-C already loaded and consistent; nothing to do."
    exit 0
fi
if [ "$(psql -tAc "SELECT to_regclass('warehouse') IS NOT NULL" 2>/dev/null)" = "t" ]; then
    echo "[SEED] Partial or inconsistent TPC-C state detected; reloading from scratch."
fi

echo "[SEED] Dropping legacy/partial tables..."
for table in $DROP_TABLES; do
    psql -q -c "DROP TABLE IF EXISTS $table CASCADE"
done

echo "[SEED] Applying TPC-C schema..."
psql -q -v ON_ERROR_STOP=1 -f /opt/tpcc-schema.sql

echo "[SEED] Generating TPC-C data (1 warehouse)..."
mkdir -p /tmp/tpcc
python3 /opt/tpcc_gen.py /tmp/tpcc

echo "[SEED] Loading tables into $PGDATABASE..."
for table in warehouse district item stock customer history oorder new_order order_line; do
    psql -q -v ON_ERROR_STOP=1 -c "\\copy $table FROM '/tmp/tpcc/$table.csv' WITH (FORMAT csv, DELIMITER '|', NULL '')"
    echo "[SEED]   $table loaded"
done

consistent=$(psql -tAc "SELECT (SELECT count(*) FROM district) = 10 \
    AND (SELECT count(*) FROM item) = 100000 \
    AND (SELECT count(*) FROM stock) = 100000 \
    AND (SELECT count(*) FROM customer) = 30000 \
    AND (SELECT count(*) FROM history) = 30000 \
    AND (SELECT count(*) FROM oorder) = 30000 \
    AND (SELECT count(*) FROM new_order) = 9000 \
    AND (SELECT count(*) FROM order_line) = (SELECT sum(o_ol_cnt) FROM oorder) \
    AND (SELECT count(*) FROM oorder WHERE o_carrier_id IS NULL) = (SELECT count(*) FROM new_order)")
if [ "$consistent" != "t" ]; then
    echo "[SEED] FAILED: post-load consistency checks failed: $consistent"
    exit 1
fi

counts=$(psql -tAc "SELECT 'item=' || (SELECT count(*) FROM item) || ' customer=' || (SELECT count(*) FROM customer) || ' oorder=' || (SELECT count(*) FROM oorder) || ' order_line=' || (SELECT count(*) FROM order_line)")
echo "[SEED] Done (FKs enforced at load, cardinalities verified): $counts"
