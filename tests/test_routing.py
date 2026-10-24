"""Routing with the real app: MCP at `/`, health at `/healthz`, auth in front."""

from __future__ import annotations

import pytest
from conftest import ALICE_TOKEN, TOKENS, McpClient
from starlette.testclient import TestClient

from usesend_mcp.auth import parse_tokens
from usesend_mcp.client import Client
from usesend_mcp.server import UseSendTools, create_app


@pytest.fixture
def http(mock, audit):
    tools = UseSendTools(Client(mock.url, "us_testkey"), audit, ("example.com",))
    app = create_app(parse_tokens(TOKENS), tools)
    # The public Host header a reverse proxy forwards; must not be answered with 421.
    with TestClient(app, base_url="http://usesend.example.com") as client:
        yield client


def test_post_root_without_token_is_401(http):
    r = McpClient(http, token=None).initialize()
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_unknown_token_is_401(http):
    r = McpClient(http, token="x" * 64).initialize()
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_non_bearer_scheme_is_401(http):
    r = http.post("/", json={}, headers={"Authorization": f"Basic {ALICE_TOKEN}"})
    assert r.status_code == 401


def test_healthz_is_open_and_checks_the_audit_db(http, audit):
    r = http.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    audit.fail_ping = True
    assert http.get("/healthz").status_code == 503


def test_initialize_at_root_with_public_host(http):
    r = McpClient(http).initialize()
    assert r.status_code == 200, r.text  # not 404 (path), not 421 (host check)
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["result"]["serverInfo"]["name"] == "usesend"


def test_default_mcp_path_is_not_served(http):
    r = McpClient(http).initialize(path="/mcp")
    assert r.status_code == 404


def test_request_log_never_contains_the_token(http, capsys):
    McpClient(http).initialize()
    McpClient(http, token="x" * 64).initialize()
    out = capsys.readouterr()
    logged = out.out + out.err
    assert '"event": "request"' in logged
    assert '"actor": "alice"' in logged
    assert ALICE_TOKEN not in logged
    assert "x" * 64 not in logged
