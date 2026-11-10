FROM python:3.12-slim

# RAG fixture initializer (phase 1 of change add-rag-fixture-and-embed-tool).
# The authored incident-report corpus (`corpus.json`) is committed in-repo and
# baked into the image at build time; phase-1 seeding is fully offline and
# never contacts an embedding endpoint.
RUN pip install --no-cache-dir "psycopg[binary]==3.2.13"

COPY corpus.json load-corpus.py /opt/

ENTRYPOINT ["python", "/opt/load-corpus.py"]
