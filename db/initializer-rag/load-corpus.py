"""Phase-1 ragdb fixture loader: incident reports and an empty embeddings table.

Runs as the one-shot `ragdb-fixture` Compose service. The authored incident
corpus (`corpus.json`) is baked into the image at build time; this loader never
contacts an embedding endpoint. It:

1. loads `documents` (incident_id, title, body, product_version, status,
   resolved_date),
2. creates the dimension-configured empty passage-level `embeddings` table
   (PRIMARY KEY (incident_id, chunk_index)) + HNSW cosine index,
3. validates and stores the R02 task parameters (query text, version filter,
   resolved-date cutoff, gold incident ids — authored in the corpus) into the
   ADMIN database (`rag_fixture_meta`); the agent never sees that table.

Idempotent: a second run against a seeded database is a no-op that still
verifies cardinalities and refreshes the fixture metadata. A legacy documents
shape (pre-incident corpora) is dropped and reloaded.
"""

import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

FIXTURE = os.environ.get("RAG_CORPUS_FILE", "/opt/corpus.json")
MIN_DOCUMENTS = 40

EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))


def _connect_pg():
    return psycopg.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        row_factory=dict_row,
    )


def _connect_admin():
    url = os.environ.get("ADMINDB_URL", "")
    if not url:
        return None
    return psycopg.connect(url, row_factory=dict_row)


def _load_corpus():
    with open(FIXTURE, encoding="utf-8") as handle:
        corpus = json.load(handle)
    incidents = corpus["incidents"]
    if len(incidents) < MIN_DOCUMENTS:
        raise SystemExit(f"[SEED] FAILED: corpus has {len(incidents)} incidents, expected >= {MIN_DOCUMENTS}")
    ids = [i["incident_id"] for i in incidents]
    if len(ids) != len(set(ids)):
        raise SystemExit("[SEED] FAILED: duplicate incident ids in corpus")
    for incident in incidents:
        if incident["status"] not in ("open", "resolved"):
            raise SystemExit(f"[SEED] FAILED: invalid status for {incident['incident_id']}")
        if incident["status"] == "open" and incident["resolved_date"]:
            raise SystemExit(f"[SEED] FAILED: open incident {incident['incident_id']} has a resolved_date")
    return corpus


def _validate_task_params(corpus, by_id):
    task = corpus.get("r02_task")
    if not task:
        raise SystemExit("[SEED] FAILED: corpus declares no r02_task parameters")
    gold = list(task.get("gold", []))
    if not gold:
        raise SystemExit("[SEED] FAILED: r02_task declares no gold incidents")
    version = task["version"]
    cutoff = task["resolved_cutoff"]
    for incident_id in gold:
        _validate_gold_incident(incident_id, by_id, version, cutoff)
    r03 = _validate_r03_params(corpus, by_id)
    return {
        "query_text": task["query_text"],
        "version": version,
        "resolved_cutoff": cutoff,
        "gold_incident_ids": json.dumps(gold),
        "r03_question": r03["question"],
        "r03_expected_answer": r03["expected_answer"],
        "r03_gold_incident_ids": json.dumps(r03["gold"]),
    }


def _validate_r03_params(corpus, by_id):
    task = corpus.get("r03_task")
    if not task:
        raise SystemExit("[SEED] FAILED: corpus declares no r03_task parameters")
    expected = task.get("expected_answer", "")
    if not isinstance(expected, str) or not expected.strip():
        raise SystemExit("[SEED] FAILED: r03_task declares no expected answer")
    gold = list(task.get("gold", []))
    if not gold:
        raise SystemExit("[SEED] FAILED: r03_task declares no gold citations")
    for incident_id in gold:
        if incident_id not in by_id:
            raise SystemExit(f"[SEED] FAILED: r03 gold incident {incident_id} missing from corpus")
        if incident_id not in expected:
            raise SystemExit(
                f"[SEED] FAILED: r03 expected answer does not cite gold incident {incident_id}"
            )
    return {
        "question": task["question"],
        "expected_answer": expected,
        "gold": gold,
    }


def _validate_variants(corpus, by_id):
    """Validate the variant task list; returns rows for admindb rag_variant_tasks."""
    variants = corpus.get("variant_tasks", [])
    rows = []
    seen = set()
    for variant in variants:
        task_id = variant.get("task_id", "")
        kind = variant.get("kind", "")
        question = variant.get("question", "")
        gold = list(variant.get("gold", []))
        filters = variant.get("filters", {})
        expected = variant.get("expected_answer", "")
        key_points = [str(point) for point in variant.get("key_points", [])]
        reward_mode = variant.get("reward_mode", "sparse")
        if task_id in seen:
            raise SystemExit(f"[SEED] FAILED: duplicate variant task id {task_id}")
        seen.add(task_id)
        if kind not in ("retrieval", "judged"):
            raise SystemExit(f"[SEED] FAILED: variant {task_id} has invalid kind {kind!r}")
        if reward_mode not in ("sparse", "dense"):
            raise SystemExit(f"[SEED] FAILED: variant {task_id} has invalid reward_mode {reward_mode!r}")
        if reward_mode == "dense" and not key_points:
            raise SystemExit(f"[SEED] FAILED: dense variant {task_id} declares no key_points")
        if not question.strip():
            raise SystemExit(f"[SEED] FAILED: variant {task_id} declares no question")
        if not gold:
            raise SystemExit(f"[SEED] FAILED: variant {task_id} declares no gold incidents")
        for incident_id in gold:
            incident = by_id.get(incident_id)
            if incident is None:
                raise SystemExit(f"[SEED] FAILED: variant {task_id} gold {incident_id} missing from corpus")
            if "status" in filters and incident["status"] != filters["status"]:
                raise SystemExit(
                    f"[SEED] FAILED: variant {task_id} gold {incident_id} violates status filter"
                )
            if "version" in filters and incident["product_version"] != filters["version"]:
                raise SystemExit(
                    f"[SEED] FAILED: variant {task_id} gold {incident_id} violates version filter"
                )
            if "versions" in filters and incident["product_version"] not in filters["versions"]:
                raise SystemExit(
                    f"[SEED] FAILED: variant {task_id} gold {incident_id} violates versions filter"
                )
        if kind == "judged":
            if not expected.strip():
                raise SystemExit(f"[SEED] FAILED: judged variant {task_id} declares no expected answer")
            for incident_id in gold:
                if incident_id not in expected:
                    raise SystemExit(
                        f"[SEED] FAILED: judged variant {task_id} expected answer does not cite {incident_id}"
                    )
        for key_point in key_points:
            if not any(incident_id in key_point for incident_id in gold):
                raise SystemExit(
                    f"[SEED] FAILED: dense variant {task_id} key point cites no gold incident: {key_point!r}"
                )
        rows.append(
            {
                "task_id": task_id,
                "kind": kind,
                "question": question,
                "expected_answer": expected,
                "gold_incident_ids": json.dumps(gold),
                "filters": json.dumps(filters),
                "key_points": json.dumps(key_points),
                "reward_mode": reward_mode,
            }
        )
    return rows


def _validate_gold_incident(incident_id, by_id, version, cutoff):
    incident = by_id.get(incident_id)
    if incident is None:
        raise SystemExit(f"[SEED] FAILED: gold incident {incident_id} missing from corpus")
    if incident["product_version"] != version:
        raise SystemExit(
            f"[SEED] FAILED: gold incident {incident_id} targets version "
            f"{incident['product_version']}, expected {version}"
        )
    if incident["status"] == "resolved" and incident["resolved_date"] < cutoff:
        raise SystemExit(
            f"[SEED] FAILED: gold incident {incident_id} resolved {incident['resolved_date']} "
            f"before the cutoff {cutoff}"
        )


def main() -> int:
    corpus = _load_corpus()
    params = _validate_task_params(corpus, {i["incident_id"]: i for i in corpus["incidents"]})
    variant_rows = _validate_variants(corpus, {i["incident_id"]: i for i in corpus["incidents"]})
    admin = _connect_admin()

    with _connect_pg() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            # legacy corpora used a different shape; embeddings are derived data
            legacy = cur.execute(
                "SELECT count(*) AS n FROM pg_attribute "
                "WHERE attrelid = to_regclass('documents') AND attname = 'source_id'"
            ).fetchone()["n"]
            if legacy:
                cur.execute("DROP TABLE IF EXISTS embeddings")
                cur.execute("DROP TABLE documents")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    incident_id text PRIMARY KEY,
                    title text NOT NULL,
                    body text NOT NULL,
                    product_version text NOT NULL,
                    status text NOT NULL,
                    resolved_date date
                )
                """
            )
            conn.commit()
            cur.execute("SELECT count(*) AS n FROM documents")
            existing = cur.fetchone()["n"]
            if existing == 0:
                rows = [
                    (
                        i["incident_id"],
                        i["title"],
                        i["body"],
                        i["product_version"],
                        i["status"],
                        i["resolved_date"],
                    )
                    for i in corpus["incidents"]
                ]
                cur.executemany(
                    "INSERT INTO documents (incident_id, title, body, product_version, status, resolved_date) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    rows,
                )
                conn.commit()
                print(f"[SEED] Loaded {len(rows)} incident reports.")
            else:
                print(f"[SEED] Documents already present ({existing}); verifying.")

            cur.execute("SELECT count(*) AS n FROM documents")
            total = cur.fetchone()["n"]
            if total < MIN_DOCUMENTS:
                print(f"[SEED] FAILED: documents table has {total} rows, expected >= {MIN_DOCUMENTS}")
                return 1

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS embeddings (
                    incident_id text NOT NULL REFERENCES documents(incident_id) ON DELETE CASCADE,
                    chunk_index integer NOT NULL,
                    model_version text NOT NULL,
                    embedding vector({EMBEDDING_DIM}) NOT NULL,
                    PRIMARY KEY (incident_id, chunk_index)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS embeddings_hnsw "
                "ON embeddings USING hnsw (embedding vector_cosine_ops)"
            )
            conn.commit()
            cur.execute("SELECT count(*) AS n FROM embeddings")
            embedded = cur.fetchone()["n"]
            print(f"[SEED] Embeddings table ready (dimension {EMBEDDING_DIM}); passage rows: {embedded}.")

    if admin is not None:
        with admin.cursor() as cur:
            legacy_meta = cur.execute(
                "SELECT count(*) AS n FROM information_schema.columns "
                "WHERE table_name = 'rag_fixture_meta' AND column_name = 'topic_tag'"
            ).fetchone()["n"]
            if legacy_meta:
                cur.execute("DROP TABLE rag_fixture_meta")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_fixture_meta (
                    id boolean PRIMARY KEY DEFAULT true CHECK (id),
                    query_text text NOT NULL,
                    version text NOT NULL,
                    resolved_cutoff date NOT NULL,
                    gold_incident_ids jsonb NOT NULL,
                    r03_question text NOT NULL DEFAULT '',
                    r03_expected_answer text NOT NULL DEFAULT '',
                    r03_gold_incident_ids jsonb NOT NULL DEFAULT '[]'::jsonb
                )
                """
            )
            for _column, ddl in (
                ("r03_question", "ALTER TABLE rag_fixture_meta ADD COLUMN IF NOT EXISTS r03_question text"),
                (
                    "r03_expected_answer",
                    "ALTER TABLE rag_fixture_meta ADD COLUMN IF NOT EXISTS r03_expected_answer text",
                ),
                (
                    "r03_gold_incident_ids",
                    "ALTER TABLE rag_fixture_meta ADD COLUMN IF NOT EXISTS r03_gold_incident_ids jsonb",
                ),
            ):
                cur.execute(ddl)
            cur.execute("DELETE FROM rag_fixture_meta")
            cur.execute(
                "INSERT INTO rag_fixture_meta (query_text, version, resolved_cutoff, gold_incident_ids, "
                "r03_question, r03_expected_answer, r03_gold_incident_ids) "
                "VALUES (%s, %s, %s, CAST(%s AS jsonb), %s, %s, CAST(%s AS jsonb))",
                (
                    params["query_text"],
                    params["version"],
                    params["resolved_cutoff"],
                    params["gold_incident_ids"],
                    params["r03_question"],
                    params["r03_expected_answer"],
                    params["r03_gold_incident_ids"],
                ),
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_variant_tasks (
                    task_id text PRIMARY KEY,
                    kind text NOT NULL,
                    question text NOT NULL,
                    expected_answer text NOT NULL DEFAULT '',
                    gold_incident_ids jsonb NOT NULL,
                    filters jsonb NOT NULL DEFAULT '{}'::jsonb,
                    key_points jsonb NOT NULL DEFAULT '[]'::jsonb,
                    reward_mode text NOT NULL DEFAULT 'sparse'
                )
                """
            )
            cur.execute("ALTER TABLE rag_variant_tasks ADD COLUMN IF NOT EXISTS key_points jsonb")
            cur.execute("ALTER TABLE rag_variant_tasks ADD COLUMN IF NOT EXISTS reward_mode text")
            cur.execute("DELETE FROM rag_variant_tasks")
            for row in variant_rows:
                cur.execute(
                    "INSERT INTO rag_variant_tasks (task_id, kind, question, expected_answer, "
                    "gold_incident_ids, filters, key_points, reward_mode) "
                    "VALUES (%s, %s, %s, %s, CAST(%s AS jsonb), CAST(%s AS jsonb), CAST(%s AS jsonb), %s)",
                    (
                        row["task_id"],
                        row["kind"],
                        row["question"],
                        row["expected_answer"],
                        row["gold_incident_ids"],
                        row["filters"],
                        row["key_points"],
                        row["reward_mode"],
                    ),
                )
        admin.commit()
        admin.close()
        print(
            f"[SEED] Fixture metadata stored: {len(json.loads(params['gold_incident_ids']))} gold incidents "
            f"for version {params['version']}."
        )
    else:
        print("[SEED] ADMINDB_URL not set; fixture metadata not stored (task seeding will skip RAG).")
    print("[SEED] Phase 1 complete (no embeddings were generated).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
