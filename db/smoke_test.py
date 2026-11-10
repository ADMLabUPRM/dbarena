import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SMOKE_API_BASE", "http://127.0.0.1:8000")
MCP_BASE = os.environ.get("SMOKE_MCP_BASE", "http://mcp:8090")

MCP_TOOL_NAMES = [
    "dbarena_health",
    "get_interface",
    "get_session",
    "get_task",
    "list_task_classifications",
    "list_tasks",
    "reset_session",
    "start_session",
    "step_session",
]

failures: list[str] = []


def call(method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body) if body else None


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def _mcp_json(result):
    if result.isError:
        raise RuntimeError(f"MCP tool error: {result.content}")
    return json.loads(result.content[0].text)


def check_mcp_round_trip(expected_tuples: list) -> None:
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def scenario():
        async with streamablehttp_client(f"{MCP_BASE}/mcp") as (read, write, _session_id):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                listing = await session.list_tools()
                started = await session.call_tool("start_session", {"task_id": "DBA-O06-001"})
                started_body = _mcp_json(started)
                session_id = started_body["session_id"]
                schema = await session.call_tool(
                    "step_session", {"session_id": session_id, "tool": "describe_schema", "arguments": {}}
                )
                submitted = await session.call_tool(
                    "step_session",
                    {"session_id": session_id, "tool": "submit_final_answer", "arguments": {"tuples": expected_tuples}},
                )
                return init, listing, started_body, _mcp_json(schema), _mcp_json(submitted)

    try:
        init, listing, started_body, schema_body, submitted_body = anyio.run(scenario)
    except Exception as exc:  # noqa: BLE001 - smoke tests report any failure via check()
        check("mcp server handshake", False, repr(exc))
        check("mcp tool list", False, "skipped: no connection")
        check("mcp session round trip", False, "skipped: no connection")
        return

    check("mcp server handshake", init.serverInfo.name == "dbarena", str(init.serverInfo))
    names = sorted(tool.name for tool in listing.tools)
    check("mcp tool list", names == MCP_TOOL_NAMES, str(names))
    check(
        "mcp session round trip",
        started_body["steps"][0]["observation"]["type"] == "schema_description"
        and schema_body["step_number"] == 1
        and schema_body["observation"]["type"] == "schema_description"
        and submitted_body["terminated"] is True
        and submitted_body["reward"] == 1.0,
        str({"started": started_body.get("steps"), "schema": schema_body, "submitted": submitted_body}),
    )


def check_cc_episode() -> None:
    taskdb_url = os.environ.get(
        "TASKDB_URL", "postgresql+psycopg://dbarena:tarzandb@taskdb:5432/taskdb"
    )
    task = {
        "task_id": "smoke-cc",
        "goal": "temporary smoke test state-changing task",
        "submission": "Call submit_final_answer with one tuple: [result].",
        "category": "OLTP",
        "task_type": "CC",
        "max_steps": 8,
        "sqlalchemy_url": taskdb_url,
        "terminal_condition": {
            "expected_tuples": [["ok"]],
            "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
            "state_checks": [
                {
                    "sql": "SELECT count(*) FROM smoke_cc_item",
                    "expected_tuples": [["1"]],
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                }
            ],
            "rules": [
                {
                    "sql": "SELECT count(*) = 1 FROM smoke_cc_item",
                    "description": "table has exactly one row",
                }
            ],
        },
        "query_timeout": 10,
        "row_limit": None,
    }
    status, _ = call("DELETE", "/api/tasks/smoke-cc")
    status, body = call("POST", "/api/tasks", task)
    check("cc task create", status == 201, f"status={status} body={body}")
    if status != 201:
        return

    setup_sqls = (
        "CREATE TABLE IF NOT EXISTS smoke_cc_item (id integer PRIMARY KEY)",
        "DELETE FROM smoke_cc_item",
        "INSERT INTO smoke_cc_item VALUES (1)",
    )

    # successful CC episode: perform the writes, then submit the confirmation
    status, session = call("POST", "/api/sessions", {"task_id": "smoke-cc"})
    sid = session["session_id"]
    for sql in setup_sqls:
        status, written = call("POST", f"/api/sessions/{sid}/step", {"tool": "run_sql", "arguments": {"sql": sql}})
        check("cc episode write", status == 200 and written["observation"]["type"] == "query_result", str(written))
    status, submitted = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"tuples": [["ok"]]}},
    )
    check(
        "cc episode success",
        status == 200
        and submitted["terminated"] is True
        and submitted["reward"] == 1.0
        and submitted["observation"]["payload"]["matched"] is True,
        str(submitted),
    )

    # wrong confirmation: state passes, confirmation fails → failure names the stage
    status, session = call("POST", "/api/sessions", {"task_id": "smoke-cc"})
    sid = session["session_id"]
    for sql in setup_sqls:
        call("POST", f"/api/sessions/{sid}/step", {"tool": "run_sql", "arguments": {"sql": sql}})
    status, wrong = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"tuples": [["wrong"]]}},
    )
    check(
        "cc episode confirmation failure",
        status == 200
        and wrong["terminated"] is True
        and wrong["reward"] == 0.0
        and "confirmation failed" in wrong["observation"]["payload"]["message"],
        str(wrong),
    )

    # pristine fixture: recreate the table (no-op if a previous episode leaked) and count
    status, session = call("POST", "/api/sessions", {"task_id": "smoke-cc"})
    sid = session["session_id"]
    call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "run_sql", "arguments": {"sql": "CREATE TABLE IF NOT EXISTS smoke_cc_item (id integer PRIMARY KEY)"}},
    )
    status, counted = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "run_sql", "arguments": {"sql": "SELECT count(*) FROM smoke_cc_item"}},
    )
    rows = counted["observation"]["payload"].get("rows")
    check(
        "cc episode rollback leaves fixture pristine",
        status == 200 and rows == [[0]],
        str(counted["observation"]["payload"]),
    )
    call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"tuples": [["ok"]]}},
    )
    call("DELETE", "/api/tasks/smoke-cc")


def check_nav_and_mix_round_trips() -> None:
    for task_id, name in (("DBA-O09-001", "nav task round trip"), ("DBA-O12-001", "mix task round trip")):
        status, task = call("GET", f"/api/tasks/{task_id}")
        if status != 200:
            check(name, False, f"task fetch failed: {task}")
            continue
        expected = task["terminal_condition"]["expected_tuples"]
        status, session = call("POST", "/api/sessions", {"task_id": task_id})
        sid = session["session_id"]
        status, submitted = call(
            "POST",
            f"/api/sessions/{sid}/step",
            {"tool": "submit_final_answer", "arguments": {"tuples": expected}},
        )
        check(
            name,
            status == 200
            and submitted["terminated"] is True
            and submitted["reward"] == 1.0
            and submitted["observation"]["payload"]["matched"] is True,
            str(submitted),
        )


def check_olap_round_trip() -> None:
    status, task = call("GET", "/api/tasks/DBA-A03-001")
    if status != 200:
        check("olap task round trip", False, f"task fetch failed: {task}")
        return
    expected = task["terminal_condition"]["expected_tuples"]
    status, session = call("POST", "/api/sessions", {"task_id": "DBA-A03-001"})
    sid = session["session_id"]
    status, queried = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {
            "tool": "run_sql",
            "arguments": {
                "sql": (
                    "SELECT round(100.00 * "
                    "sum(CASE WHEN p.p_type LIKE 'PROMO%' "
                    "THEN l.l_extendedprice * (1 - l.l_discount) ELSE 0 END) / "
                    "sum(l.l_extendedprice * (1 - l.l_discount)), 2) "
                    "FROM lineitem l JOIN part p ON p.p_partkey = l.l_partkey "
                    "WHERE l.l_shipdate >= DATE '1995-09-01' "
                    "AND l.l_shipdate < DATE '1995-10-01'"
                )
            },
        },
    )
    rows = queried["observation"]["payload"].get("rows")
    check(
        "olap duckdb query",
        status == 200
        and queried["observation"]["type"] == "query_result"
        and rows is not None
        and len(rows) == 1
        and abs(float(rows[0][0]) - float(expected[0][0])) < 0.01,
        str(queried["observation"]["payload"]),
    )
    status, submitted = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"tuples": expected}},
    )
    check(
        "olap task round trip",
        status == 200
        and submitted["terminated"] is True
        and submitted["reward"] == 1.0
        and submitted["observation"]["payload"]["matched"] is True,
        str(submitted),
    )


def check_olap_nav_round_trip() -> None:
    status, task = call("GET", "/api/tasks/DBA-A01-001")
    if status != 200:
        check("olap nav task round trip", False, f"task fetch failed: {task}")
        return
    expected = task["terminal_condition"]["expected_tuples"]
    status, session = call("POST", "/api/sessions", {"task_id": "DBA-A01-001"})
    sid = session["session_id"]
    status, submitted = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"tuples": expected}},
    )
    check(
        "olap nav task round trip",
        status == 200
        and submitted["terminated"] is True
        and submitted["reward"] == 1.0
        and submitted["observation"]["payload"]["matched"] is True,
        str(submitted),
    )


def check_olap_cc_episode() -> None:
    olapdb_url = os.environ.get("OLAPDB_URL", "duckdb:////data/olap-tpch.duckdb")
    task = {
        "task_id": "smoke-olap-cc",
        "goal": "temporary smoke test state-changing DuckDB task",
        "submission": "Call submit_final_answer with one tuple: [result].",
        "category": "OLAP",
        "task_type": "CC",
        "max_steps": 8,
        "sqlalchemy_url": olapdb_url,
        "terminal_condition": {
            "expected_tuples": [["ok"]],
            "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
            "state_checks": [
                {
                    "sql": "SELECT count(*) FROM smoke_olap_cc_item",
                    "expected_tuples": [["1"]],
                    "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
                }
            ],
        },
        "query_timeout": 10,
        "row_limit": None,
    }
    status, _ = call("DELETE", "/api/tasks/smoke-olap-cc")
    status, body = call("POST", "/api/tasks", task)
    check("olap cc task create", status == 201, f"status={status} body={body}")
    if status != 201:
        return

    # successful CC episode on DuckDB: create the table, insert, submit
    status, session = call("POST", "/api/sessions", {"task_id": "smoke-olap-cc"})
    sid = session["session_id"]
    for sql in (
        "CREATE TABLE smoke_olap_cc_item (id integer PRIMARY KEY)",
        "INSERT INTO smoke_olap_cc_item VALUES (1)",
    ):
        status, written = call("POST", f"/api/sessions/{sid}/step", {"tool": "run_sql", "arguments": {"sql": sql}})
        check("olap cc episode write", status == 200 and written["observation"]["type"] == "query_result", str(written))
    status, submitted = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"tuples": [["ok"]]}},
    )
    check(
        "olap cc episode success",
        status == 200
        and submitted["terminated"] is True
        and submitted["reward"] == 1.0
        and submitted["observation"]["payload"]["matched"] is True,
        str(submitted),
    )

    # wrong confirmation: state passes, confirmation fails
    status, session = call("POST", "/api/sessions", {"task_id": "smoke-olap-cc"})
    sid = session["session_id"]
    call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "run_sql", "arguments": {"sql": "CREATE TABLE smoke_olap_cc_item (id integer PRIMARY KEY)"}},
    )
    call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "run_sql", "arguments": {"sql": "INSERT INTO smoke_olap_cc_item VALUES (1)"}},
    )
    status, wrong = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"tuples": [["wrong"]]}},
    )
    check(
        "olap cc episode confirmation failure",
        status == 200
        and wrong["terminated"] is True
        and wrong["reward"] == 0.0
        and "confirmation failed" in wrong["observation"]["payload"]["message"],
        str(wrong),
    )

    # rollback proof: the mid-episode CREATE TABLE is gone in a fresh episode
    status, session = call("POST", "/api/sessions", {"task_id": "smoke-olap-cc"})
    sid = session["session_id"]
    status, counted = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {
            "tool": "run_sql",
            "arguments": {"sql": "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'smoke_olap_cc_item'"},
        },
    )
    rows = counted["observation"]["payload"].get("rows")
    check(
        "olap cc episode rollback leaves fixture pristine",
        status == 200 and rows == [[0]],
        str(counted["observation"]["payload"]),
    )
    call("DELETE", "/api/tasks/smoke-olap-cc")


def check_rag_round_trip() -> None:
    status, task = call("GET", "/api/tasks/DBA-R02-001")
    if status != 200:
        check("rag task round trip", False, f"task fetch failed: {task}")
        return
    status, embed_status = call("GET", "/api/embeddings/status")
    if embed_status and embed_status.get("state") != "ready":
        check(
            "rag task round trip",
            False,
            f"embedding lifecycle is {embed_status.get('state')!r}; run make reembed before the smoke test",
        )
        return
    expected = task["terminal_condition"]["expected_tuples"]
    status, session = call("POST", "/api/sessions", {"task_id": "DBA-R02-001"})
    if status != 201:
        check("rag task round trip", False, f"session start failed: {session}")
        return
    sid = session["session_id"]
    status, queried = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {
            "tool": "run_rag_query",
            "arguments": {
                "sql": (
                    "SELECT d.incident_id FROM documents d "
                    "JOIN embeddings e ON e.incident_id = d.incident_id "
                    "WHERE d.product_version = '4.2.0' "
                    "AND (d.resolved_date IS NULL OR d.resolved_date >= DATE '2024-06-01') "
                    "ORDER BY e.embedding <=> :query_vector LIMIT 10"
                ),
                "text": "connection pool exhaustion under load",
            },
        },
    )
    rows = queried["observation"]["payload"].get("rows") if status == 200 else None
    check(
        "rag run_rag_query retrieval",
        status == 200
        and queried["observation"]["type"] == "query_result"
        and rows is not None
        and len(rows) > 0
        and all("[" not in str(row[0]) for row in rows),
        str(queried["observation"]["payload"])[:200],
    )
    status, submitted = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"tuples": expected}},
    )
    check(
        "rag task round trip",
        status == 200
        and submitted["terminated"] is True
        and submitted["reward"] == 1.0
        and submitted["observation"]["payload"]["matched"] is True,
        str(submitted),
    )


def check_judged_round_trip() -> None:
    status, task = call("GET", "/api/tasks/DBA-R03-001")
    if status != 200:
        check("judged task round trip", False, f"task fetch failed: {task}")
        return
    status, embed_status = call("GET", "/api/embeddings/status")
    if embed_status and embed_status.get("state") != "ready":
        check(
            "judged task round trip",
            False,
            f"embedding lifecycle is {embed_status.get('state')!r}; run make reembed before the smoke test",
        )
        return
    expected = task["terminal_condition"]["judge"]["expected"]

    status, session = call("POST", "/api/sessions", {"task_id": "DBA-R03-001"})
    if status != 201:
        check("judged task round trip", False, f"session start failed: {session}")
        return
    sid = session["session_id"]
    status, judged = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"answer": expected}},
    )
    check(
        "judged task round trip (pass)",
        status == 200
        and judged["terminated"] is True
        and judged["reward"] == 1.0
        and "pass" in judged["observation"]["payload"]["message"].lower(),
        str(judged),
    )

    status, session = call("POST", "/api/sessions", {"task_id": "DBA-R03-001"})
    sid = session["session_id"]
    status, judged = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {
            "tool": "submit_final_answer",
            "arguments": {"answer": "The pools exhaust because of disk usage; fixed by restarting the server."},
        },
    )
    check(
        "judged task round trip (fail)",
        status == 200
        and judged["terminated"] is True
        and judged["reward"] == 0.0
        and "fail" in judged["observation"]["payload"]["message"].lower(),
        str(judged),
    )


VARIANT_WRITES_TEMPLATE = (
    (
        "INSERT INTO oorder (o_id, o_d_id, o_w_id, o_c_id, o_entry_d, o_carrier_id, o_ol_cnt, o_all_local) "
        "VALUES ({o_id}, {d_id}, {w_id}, {c_id}, TIMESTAMP '2026-10-15 12:00:00', NULL, 2, 1)"
    ),
    (
        "INSERT INTO order_line (ol_w_id, ol_d_id, ol_o_id, ol_number, ol_i_id, ol_supply_w_id, "
        "ol_delivery_d, ol_quantity, ol_amount, ol_dist_info) "
        "SELECT {w_id}, {d_id}, {o_id}, 1, {i1}, {w_id}, NULL, {q1}, "
        "round({q1} * i.i_price, 2), s.s_dist_01 "
        "FROM item i JOIN stock s ON s.s_i_id = i.i_id "
        "WHERE i.i_id = {i1} AND s.s_w_id = {w_id}"
    ),
    (
        "INSERT INTO order_line (ol_w_id, ol_d_id, ol_o_id, ol_number, ol_i_id, ol_supply_w_id, "
        "ol_delivery_d, ol_quantity, ol_amount, ol_dist_info) "
        "SELECT {w_id}, {d_id}, {o_id}, 2, {i2}, {w_id}, NULL, {q2}, "
        "round({q2} * i.i_price, 2), s.s_dist_01 "
        "FROM item i JOIN stock s ON s.s_i_id = i.i_id "
        "WHERE i.i_id = {i2} AND s.s_w_id = {w_id}"
    ),
    "INSERT INTO new_order (no_w_id, no_d_id, no_o_id) VALUES ({w_id}, {d_id}, {o_id})",
    "UPDATE district SET d_next_o_id = d_next_o_id + 1 WHERE d_w_id = {w_id} AND d_id = {d_id}",
)


def _variant_params(sid: str) -> dict:
    queries = {
        "w_id": "SELECT min(w_id) FROM warehouse",
        "d_id": "SELECT min(d_id) FROM district",
        "c_id": (
            "SELECT min(c_id) FROM customer "
            "WHERE c_w_id = (SELECT min(w_id) FROM warehouse) "
            "AND c_d_id = (SELECT min(d_id) FROM district)"
        ),
        "i1": "SELECT min(i_id) FROM item",
        "o_id": (
            "SELECT d_next_o_id FROM district "
            "WHERE d_w_id = (SELECT min(w_id) FROM warehouse) "
            "AND d_id = (SELECT min(d_id) FROM district)"
        ),
    }
    params: dict = {}
    for name, sql in queries.items():
        _, result = call("POST", f"/api/sessions/{sid}/step", {"tool": "run_sql", "arguments": {"sql": sql}})
        rows = result["observation"]["payload"].get("rows") or []
        if not rows:
            raise RuntimeError(f"variant param {name} unresolved: {result}")
        params[name] = rows[0][0]
    params["i2"] = params["i1"] + 7
    params["q1"] = 5
    params["q2"] = 2
    return params


def _run_variant_writes(sid: str, writes) -> bool:
    ok = True
    for sql in writes:
        status, written = call("POST", f"/api/sessions/{sid}/step", {"tool": "run_sql", "arguments": {"sql": sql}})
        ok = ok and status == 200 and written["observation"]["type"] == "query_result"
    return ok


def check_isolation_variant_episodes() -> None:
    task_id = "DBA-O03-002"
    status, task = call("GET", f"/api/tasks/{task_id}")
    if status != 200:
        check("isolation variant task fetch", False, f"task fetch failed: {task}")
        return
    checks = (task.get("terminal_condition") or {}).get("state_checks") or []
    check(
        "isolation variant terminal condition",
        len(checks) == 14,
        f"state_checks={len(checks)}",
    )

    # clean episode: exactly the reference writes, then submit the order id
    status, session = call("POST", "/api/sessions", {"task_id": task_id})
    sid = session["session_id"]
    params = _variant_params(sid)
    writes = [sql.format(**params) for sql in VARIANT_WRITES_TEMPLATE]
    writes_ok = _run_variant_writes(sid, writes)
    status, submitted = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"tuples": [[params["o_id"]]]}},
    )
    payload = submitted["observation"]["payload"]
    check(
        "isolation variant clean episode succeeds",
        writes_ok
        and status == 200
        and submitted["terminated"] is True
        and submitted["reward"] == 1.0
        and payload.get("matched") is True,
        str(submitted),
    )

    # stray episode: touch a table the task does not declare, then do the correct writes
    status, session = call("POST", "/api/sessions", {"task_id": task_id})
    sid = session["session_id"]
    params = _variant_params(sid)
    stray = (
        "UPDATE stock SET s_quantity = s_quantity - 1 "
        f"WHERE s_w_id = {params['w_id']} AND s_i_id = {params['i1']}"
    )
    status, strayed = call("POST", f"/api/sessions/{sid}/step", {"tool": "run_sql", "arguments": {"sql": stray}})
    writes = [sql.format(**params) for sql in VARIANT_WRITES_TEMPLATE]
    _run_variant_writes(sid, writes)
    status, submitted = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"tuples": [[params["o_id"]]]}},
    )
    payload = submitted["observation"]["payload"]
    check(
        "isolation variant stray write episode fails",
        strayed["observation"]["type"] == "query_result"
        and status == 200
        and submitted["terminated"] is True
        and submitted["reward"] == 0.0
        and "state check" in payload.get("message", ""),
        str(submitted),
    )


def main() -> int:
    status, health = call("GET", "/api/health")
    check(
        "stack health",
        status == 200 and health.get("status") == "ok" and health.get("database") is True,
        str(health),
    )

    task = {
        "task_id": "smoke-crud",
        "goal": "temporary smoke test task",
        "submission": "Call submit_final_answer with one tuple: [value].",
        "category": "OLTP",
        "task_type": "IS",
        "max_steps": 4,
        "sqlalchemy_url": "postgresql+psycopg://dbarena:dbarena@taskdb:5432/taskdb",
        "terminal_condition": {
            "expected_tuples": [["1"]],
            "matching": {"order_sensitive": False, "numeric_tolerance": None, "null_handling": "exact"},
        },
        "query_timeout": 10,
        "row_limit": None,
    }
    status, body = call("POST", "/api/tasks", task)
    created = status == 201
    if not created and isinstance(body, dict) and body.get("error", {}).get("code") == "validation_error":
        created = True
    check("task create", status == 201, f"status={status} body={body}")

    status, fetched = call("GET", "/api/tasks/smoke-crud")
    check("task read", status == 200 and fetched.get("task_id") == "smoke-crud")
    status, updated = call("PUT", "/api/tasks/smoke-crud", dict(task, max_steps=5))
    check("task update", status == 200 and updated.get("max_steps") == 5)
    status, _ = call("DELETE", "/api/tasks/smoke-crud")
    status_after, _ = call("GET", "/api/tasks/smoke-crud")
    check("task delete", status == 204 and status_after == 404)

    status, session = call("POST", "/api/sessions", {"task_id": "DBA-O06-001"})
    check("session start", status == 201, str(session))
    sid = session["session_id"]

    initial = session["steps"][0]["observation"]
    initial_meta = initial.get("meta", {})
    task_block = initial.get("task") or {}
    interface_complete = (
        initial["type"] == "schema_description"
        and [a.get("tool") for a in initial_meta.get("available_actions", [])]
        == [
            "run_sql",
            "describe_table",
            "describe_tables",
            "describe_schema",
            "submit_final_answer",
            "run_rag_query",
        ]
        and [e.get("type") for e in initial_meta.get("observation_space", [])]
        == ["query_result", "table_description", "schema_description", "episode_result", "error"]
        and initial_meta.get("last_action", "missing") is None
        and task_block.get("task_id") == "DBA-O06-001"
        and bool(task_block.get("goal"))
        and bool(task_block.get("submission"))
        and task_block.get("category") == "OLTP"
        and task_block.get("task_type") == "IS"
    )
    check(
        "interface metadata at reset",
        interface_complete,
        str({"task": task_block, "last_action": initial_meta.get("last_action")}),
    )

    status, classifications = call("GET", "/api/task-classifications")
    check(
        "classification listing",
        status == 200
        and classifications.get("categories") == ["OLTP", "OLAP", "ANALYTICS", "RAG"]
        and classifications.get("task_types") == ["NAV", "IS", "CC", "MIX"],
        str(classifications),
    )

    status, filtered = call("GET", "/api/tasks?category=OLTP&task_type=IS")
    check(
        "classification filter",
        status == 200
        and isinstance(filtered, list)
        and all(t.get("category") == "OLTP" and t.get("task_type") == "IS" for t in filtered)
        and len(filtered) > 0,
        str(filtered)[:120] if isinstance(filtered, list) else str(filtered),
    )

    status, olap_tasks = call("GET", "/api/tasks?category=OLAP")
    check(
        "warehouse tasks seeded",
        status == 200
        and isinstance(olap_tasks, list)
        and {t.get("task_id") for t in olap_tasks}
        == {
            "DBA-W01-001",
            "DBA-W02-001",
            "DBA-W03-001",
            "DBA-W04-001",
            "DBA-W05-001",
            "DBA-W06-001",
        },
        str(olap_tasks)[:160] if isinstance(olap_tasks, list) else str(olap_tasks),
    )

    status, schema = call("POST", f"/api/sessions/{sid}/step", {"tool": "describe_schema", "arguments": {}})
    table_names = [t["name"] for t in schema["observation"]["payload"]["tables"]]
    check(
        "seeded schema visible",
        status == 200 and "oorder" in table_names and "stock" in table_names,
        str(table_names),
    )

    status, counted = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "run_sql", "arguments": {"sql": "SELECT count(*) AS oorder_rows FROM oorder"}},
    )
    rows = counted["observation"]["payload"]["rows"]
    check("seeded TPC-C query", status == 200 and len(rows) == 1 and rows[0][0] > 0, str(rows))

    status, task_body = call("GET", "/api/tasks/DBA-O06-001")
    expected = task_body["terminal_condition"]["expected_tuples"]
    status, submitted = call(
        "POST",
        f"/api/sessions/{sid}/step",
        {"tool": "submit_final_answer", "arguments": {"tuples": expected}},
    )
    check(
        "playground submit round trip",
        status == 200
        and submitted["terminated"] is True
        and submitted["reward"] == 1.0
        and submitted["observation"]["payload"]["matched"] is True,
        str(submitted),
    )

    admin_url = os.environ.get("ADMINDB_URL")
    persisted = False
    if admin_url:
        from sqlalchemy import create_engine, text

        engine = create_engine(admin_url)
        with engine.connect() as conn:
            episode = conn.execute(
                text("SELECT status, reward FROM episodes WHERE episode_id = CAST(:id AS uuid)"),
                {"id": sid},
            ).first()
            step_count = conn.execute(
                text("SELECT count(*) FROM episode_steps WHERE episode_id = CAST(:id AS uuid)"),
                {"id": sid},
            ).scalar()
        engine.dispose()
        persisted = episode is not None and episode[0] == "submitted" and float(episode[1]) == 1.0 and step_count >= 4
        check("episode history persisted", persisted, f"episode={episode} steps={step_count}")

    check_mcp_round_trip(expected)

    check_cc_episode()

    check_isolation_variant_episodes()

    check_nav_and_mix_round_trips()

    check_olap_round_trip()

    check_olap_nav_round_trip()

    check_olap_cc_episode()

    check_rag_round_trip()

    check_judged_round_trip()

    if failures:
        print(f"SMOKE FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SMOKE PASSED: all checks succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
