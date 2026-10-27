"""Client request shapes against a local http.server mock (no network)."""

from __future__ import annotations

import io

import pytest

from usesend_mcp import cli, client
from usesend_mcp.client import ApiError, Client, parse_contacts_csv


def test_send_email_request_shape(mock):
    mock.replies.append((200, b'{"emailId": "e1"}'))
    c = Client(mock.url, "us_testkey")
    body = {"from": "News <news@example.com>", "to": "alice@example.com", "subject": "Hi", "html": "<p>x</p>"}
    r = c.post("/v1/emails", body, headers={"Idempotency-Key": "k1"})
    assert r == {"emailId": "e1"}
    req = mock.requests[0]
    assert req["method"] == "POST"
    assert req["path"] == "/api/v1/emails"
    assert req["headers"]["Authorization"] == "Bearer us_testkey"
    assert req["headers"]["Content-Type"] == "application/json"
    assert req["headers"]["Idempotency-Key"] == "k1"
    assert req["body"] == body


def test_base_url_trailing_slash_and_query_drops_none(mock):
    c = Client(mock.url + "/", "k")
    c.get("/v1/campaigns", page=2, status=None, search="oct")
    assert mock.requests[0]["path"] == "/api/v1/campaigns?page=2&search=oct"


def test_429_is_retried(mock, monkeypatch):
    sleeps = []
    monkeypatch.setattr(client.time, "sleep", sleeps.append)
    mock.replies += [(429, b'{"error": "slow down"}'), (429, b"{}"), (200, b'{"ok": true}')]
    assert Client(mock.url, "k").get("/v1/domains") == {"ok": True}
    assert len(mock.requests) == 3
    assert sleeps == [1, 2]


def test_429_gives_up_after_retries(mock, monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    mock.replies += [(429, b"{}")] * 6
    with pytest.raises(ApiError, match="HTTP 429"):
        Client(mock.url, "k").get("/v1/domains")
    assert len(mock.requests) == 6


def test_error_body_json_and_html(mock):
    mock.replies.append((400, b'{"error": {"code": "BAD_REQUEST", "message": "nope"}}'))
    with pytest.raises(ApiError, match=r"POST /v1/campaigns → HTTP 400: .*nope"):
        Client(mock.url, "k").post("/v1/campaigns", {})
    mock.replies.append((502, b"<html>bad gateway</html>"))
    with pytest.raises(ApiError, match="HTTP 502: <html>bad gateway"):
        Client(mock.url, "k").get("/v1/domains")


def test_cli_bulk_import_is_chunked(mock, tmp_path, monkeypatch):
    csv_path = tmp_path / "c.csv"
    lines = ["email,firstName"] + [f"user{i}@example.com,U{i}" for i in range(2500)]
    csv_path.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("USESEND_API_KEY", "us_testkey")
    rc = cli.main(["--url", mock.url, "contacts", "import", "book1", str(csv_path)])
    assert rc == 0
    sizes = [len(r["body"]) for r in mock.requests]
    assert sizes == [1000, 1000, 500]
    assert {r["path"] for r in mock.requests} == {"/api/v1/contactBooks/book1/contacts/bulk"}


def test_parse_contacts_csv_rules():
    text = io.StringIO(
        "email,first_name,lastName,subscribed,city\n"
        "alice@example.com,Alice,A,no,Paris\n"
        ",Nobody,,,\n"
        "bob@example.com,,,,\n"
    )
    assert parse_contacts_csv(text) == [
        {
            "email": "alice@example.com",
            "firstName": "Alice",
            "lastName": "A",
            "subscribed": False,
            "properties": {"city": "Paris"},
        },
        {"email": "bob@example.com"},
    ]
