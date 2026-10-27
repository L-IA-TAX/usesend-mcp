"""The audit sink against a real, throwaway Postgres (pgserver; no Docker, no network)."""

from __future__ import annotations

import psycopg
import pytest
from conftest import TOKENS, McpClient
from starlette.testclient import TestClient

from usesend_mcp.audit import PostgresAudit
from usesend_mcp.auth import parse_tokens
from usesend_mcp.client import Client
from usesend_mcp.server import UseSendTools, create_app

pgserver = pytest.importorskip("pgserver")


@pytest.fixture(scope="module")
def pg_url(tmp_path_factory):
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
def pg_audit(pg_url, request):
    schema = "t_" + request.node.name.lower().replace("[", "_").replace("]", "").replace("-", "_")[:40]
    audit = PostgresAudit(pg_url, schema)
    audit.open()
    yield audit
    audit.close()
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def rows(pg_url, schema):
    with psycopg.connect(pg_url) as conn:
        return conn.execute(
            f"SELECT actor, tool, mutating, args, outcome, error, email_ids, campaign_id, contact_book_id, duration_ms"
            f" FROM {schema}.audit_log ORDER BY id"
        ).fetchall()


def test_bootstrap_is_idempotent(pg_audit):
    pg_audit.bootstrap()
    pg_audit.bootstrap()
    pg_audit.ping()


def test_record_then_finish(pg_audit, pg_url):
    audit_id = pg_audit.record("alice", "send_email", True, {"to": ["bob@example.com"], "html": {"bytes": 3}})
    assert rows(pg_url, pg_audit.schema)[0][4] == "pending"
    pg_audit.finish(audit_id, "ok", email_ids=["e1"], duration_ms=7)
    args = {"to": ["bob@example.com"], "html": {"bytes": 3}}
    assert rows(pg_url, pg_audit.schema) == [("alice", "send_email", True, args, "ok", None, ["e1"], None, None, 7)]


def test_outcome_check_constraint(pg_audit, pg_url):
    audit_id = pg_audit.record("alice", "list_domains", False, {})
    with psycopg.connect(pg_url, autocommit=True) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(f"UPDATE {pg_audit.schema}.audit_log SET outcome = 'maybe' WHERE id = %s", (audit_id,))


def test_tool_calls_land_in_postgres(pg_audit, pg_url, mock):
    tools = UseSendTools(Client(mock.url, "us_testkey"), pg_audit, ("example.com",))
    with TestClient(create_app(parse_tokens(TOKENS), tools), base_url="http://usesend.example.com") as http:
        mcp = McpClient(http)
        mcp.call("schedule_campaign", id="c1")
        mcp.call("pause_campaign", id="c1")
        mcp.call("import_contacts", book_id="b1", csv_text="email\nalice@example.com\n")
        mcp.call("get_campaign", id="c1")
    got = [(r[0], r[1], r[2], r[4], r[7]) for r in rows(pg_url, pg_audit.schema)]
    assert got == [
        ("alice", "schedule_campaign", True, "preview", "c1"),
        ("alice", "pause_campaign", True, "ok", "c1"),
        ("alice", "import_contacts", True, "ok", None),
        ("alice", "get_campaign", False, "ok", None),
    ]
    csv_args = rows(pg_url, pg_audit.schema)[2][3]
    assert csv_args["csv_text"]["rows"] == 1
    assert "alice@example.com" not in str(csv_args)


def test_database_outage_fails_closed_for_mutating_tools(pg_audit, mock):
    tools = UseSendTools(Client(mock.url, "us_testkey"), pg_audit, ("example.com",))
    with TestClient(create_app(parse_tokens(TOKENS), tools), base_url="http://usesend.example.com") as http:
        pg_audit.close()  # the pool is gone: every audit write fails
        mcp = McpClient(http)
        assert mcp.call("pause_campaign", id="c1")["isError"]
        assert mock.requests == []
        assert not mcp.call("get_campaign", id="c1")["isError"]
        assert len(mock.requests) == 1
        assert http.get("/healthz").status_code == 503
