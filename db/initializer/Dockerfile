FROM postgres:17.11-alpine

# TPC-C data generator: in-repo deterministic loader (design D3 of change
# migrate-oltp-fixture-to-tpc-c). Pinned by definition (versioned in this
# repo, fixed seed 20261008), emits pipe-delimited CSVs for psql \copy,
# python3-only image delta, no third-party license burden. Selected over
# the official TPC-C kit (EULA click-through zip, not automatable in a
# Docker build) and BenchmarkSQL (JDK/ant image, JDBC loader) which fail
# the pinning and small-image criteria. Oracle correctness does not depend
# on data distributions (task params resolve from the live fixture at seed
# time); FK integrity and TPC-C cardinalities are asserted after load.
RUN apk add --no-cache python3

COPY tpcc-schema.sql /opt/tpcc-schema.sql
COPY tpcc_gen.py /opt/tpcc_gen.py
COPY load-tpcc.sh /usr/local/bin/load-tpcc.sh
RUN chmod +x /usr/local/bin/load-tpcc.sh

ENTRYPOINT ["load-tpcc.sh"]
