FROM python:3.12-slim

# TPC-DS retail fixture generator for the DuckDB retail database (change
# add-tpcds-fixture-and-tasks). The version pin MUST match pyproject.toml:
# the backend opens the file produced here, and DuckDB storage formats are
# only guaranteed compatible within a version.
#
# The fixture is generated at BUILD time, not runtime: the compose network is
# internal (no outbound internet), and the `tpcds` extension is downloaded on
# INSTALL. Baking the file into the image keeps the runtime step offline,
# deterministic (dsdgen output is fixed for a pinned DuckDB release), and fast.
RUN pip install --no-cache-dir duckdb==1.4.5

COPY generate.py /opt/generate.py
RUN python /opt/generate.py

COPY load-tpcds.py /opt/load-tpcds.py

# Same uid as the backend service: the fixture file and its volume directory
# must be writable by the backend, which opens the DuckDB file read-write.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin dbarena \
    && mkdir -p /data && chown dbarena /data
USER 10001:10001

ENTRYPOINT ["python", "/opt/load-tpcds.py"]
