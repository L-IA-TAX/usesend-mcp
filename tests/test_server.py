"""Tool behaviour: confirm/preview, safety refusals, audit fail-closed/fail-open, attribution."""

from __future__ import annotations

import pytest
from conftest import BOB_TOKEN, TOKENS, McpClient
from starlette.testclient import TestClient

from usesend_mcp.auth import parse_tokens
from usesend_mcp.client import Client
from usesend_mcp.server import Config, ConfigError, UseSendTools, create_app, sender_domain

SENDER = "News <news@example.com>"
UNSUB_HTML = '<p>Hi {{firstName}}</p><a href="{{usesend_unsubscribe_url}}">unsubscribe</a>'


@pytest.fixture
def mcp(mock, audit):
    tools = UseSendTools(Client(mock.url, "us_testkey"), audit, ("example.com", "mail.example.com"))
    app = create_app(parse_tokens(TOKENS), tools)
    with TestClient(app, base_url="http://usesend.example.com") as http:
        yield McpClient(http)


@pytest.fixture
def campaign_routes(mock):
    mock.routes[("GET", "/api/v1/campaigns/c1")] = {
        "id": "c1",
        "name": "Oct",
        "from": SENDER,
        "subject": "News",
        "status": "DRAFT",
        "contactBookId": "b1",
    }
    mock.routes[("GET", "/api/v1/contactBooks/b1")] = {"id": "b1", "name": "Customers", "_count": {"contacts": 3}}


# ─── tool list ───────────────────────────────────────────────────────────────


def test_tool_set_and_annotations(mcp):
    tools = mcp.tools()
    assert set(tools) == {
        "list_domains",
        "list_contact_books",
        "get_contact_book",
        "list_contacts",
        "list_campaigns",
        "get_campaign",
        "list_emails",
        "get_email",
        "get_analytics",
        "create_contact_book",
        "add_contact",
        "import_contacts",
        "update_contact",
        "send_email",
        "cancel_email",
        "create_campaign",
        "schedule_campaign",
        "pause_campaign",
        "resume_campaign",
    }
    for name in ("send_email", "schedule_campaign", "resume_campaign"):
        assert tools[name]["annotations"]["destructiveHint"] is True, name
        assert tools[name]["inputSchema"]["properties"]["confirm"]["default"] is False
    assert tools["pause_campaign"]["annotations"]["destructiveHint"] is False
    assert tools["list_domains"]["annotations"]["readOnlyHint"] is True
    assert "from" in tools["send_email"]["inputSchema"]["properties"]
    assert "from" in tools["create_campaign"]["inputSchema"]["required"]
    # Drafts only: no way to ask create_campaign to send or schedule.
    props = tools["create_campaign"]["inputSchema"]["properties"]
    assert not {"send_now", "sendNow", "scheduled_at", "scheduledAt"} & set(props)
    assert "example.com" in tools["send_email"]["description"]


# ─── confirm=false calls nothing ─────────────────────────────────────────────


def test_send_email_without_confirm_is_a_preview(mcp, mock, audit):
    result = mcp.call(
        "send_email", **{"from": SENDER, "to": ["alice@example.com"], "cc": ["bob@example.com"], "subject": "Hi"},
        html="<p>x</p>",
    )
    assert not result["isError"]
    preview = mcp.data(result)
    assert preview["preview"] is True
    assert preview["recipients"] == 2
    assert preview["when"] == "now"
    assert mock.requests == []
    assert audit.rows[-1]["outcome"] == "preview"


def test_schedule_campaign_without_confirm_is_a_preview(mcp, mock, audit, campaign_routes):
    result = mcp.call("schedule_campaign", id="c1", at="tomorrow 9am")
    preview = mcp.data(result)
    assert preview["contact_book"] == {"id": "b1", "name": "Customers", "contacts": 3}
    assert preview["when"] == "tomorrow 9am"
    assert mock.writes == []
    assert audit.rows[-1]["outcome"] == "preview"
    assert audit.rows[-1]["campaign_id"] == "c1"


def test_resume_campaign_without_confirm_is_a_preview(mcp, mock, audit, campaign_routes):
    preview = mcp.data(mcp.call("resume_campaign", id="c1"))
    assert preview["preview"] is True
    assert preview["campaign"]["name"] == "Oct"
    assert mock.writes == []
    assert audit.rows[-1]["outcome"] == "preview"


def test_confirmed_schedule_and_resume_call_the_api(mcp, mock, audit):
    mcp.call("schedule_campaign", id="c1", at="tomorrow 9am", batch_size=100, confirm=True)
    mcp.call("resume_campaign", id="c1", confirm=True)
    assert [(r["method"], r["path"], r["body"]) for r in mock.writes] == [
        ("POST", "/api/v1/campaigns/c1/schedule", {"scheduledAt": "tomorrow 9am", "batchSize": 100}),
        ("POST", "/api/v1/campaigns/c1/resume", {}),
    ]
    assert [r["outcome"] for r in audit.rows] == ["ok", "ok"]


def test_confirmed_send_carries_actor_and_idempotency_key(mock, audit):
    mock.replies.append((200, b'{"emailId": "e42"}'))
    tools = UseSendTools(Client(mock.url, "us_testkey"), audit, ("example.com",))
    app = create_app(parse_tokens(TOKENS), tools)
    with TestClient(app, base_url="http://usesend.example.com") as http:
        result = McpClient(http, token=BOB_TOKEN).call(
            "send_email", **{"from": SENDER}, to=["alice@example.com"], subject="Hi", text="Hello", confirm=True
        )
    assert not result["isError"]
    req = mock.writes[0]
    assert req["path"] == "/api/v1/emails"
    assert req["body"]["headers"] == {"X-MCP-Actor": "bob"}
    assert req["body"]["from"] == SENDER
    assert req["headers"]["Idempotency-Key"] == "mcp-audit-1"
    assert req["headers"]["Authorization"] == "Bearer us_testkey"
    row = audit.rows[0]
    assert row["actor"] == "bob"
    assert row["outcome"] == "ok"
    assert row["email_ids"] == ["e42"]


# ─── safety refusals ─────────────────────────────────────────────────────────


def test_create_campaign_refuses_without_unsubscribe_link(mcp, mock, audit):
    result = mcp.call(
        "create_campaign", name="Oct", **{"from": SENDER}, subject="News", book_id="b1", html="<p>no link</p>"
    )
    assert result["isError"]
    assert "usesend_unsubscribe_url" in mcp.text(result)
    assert mock.requests == []
    assert audit.rows[-1]["outcome"] == "refused"


def test_create_campaign_makes_a_draft(mcp, mock, audit):
    mock.replies.append((200, b'{"id": "c9"}'))
    result = mcp.call("create_campaign", name="Oct", **{"from": SENDER}, subject="News", book_id="b1", html=UNSUB_HTML)
    assert not result["isError"]
    body = mock.writes[0]["body"]
    assert body["contactBookId"] == "b1"
    assert "sendNow" not in body and "scheduledAt" not in body
    assert audit.rows[-1]["campaign_id"] == "c9"
    assert audit.rows[-1]["contact_book_id"] == "b1"


def test_legacy_unsubscribe_placeholder_is_accepted(mcp, mock):
    html = "<a href='{{ unsend_unsubscribe_url }}'>x</a>"
    result = mcp.call("create_campaign", name="Oct", **{"from": SENDER}, subject="News", book_id="b1", html=html)
    assert not result["isError"]


@pytest.mark.parametrize(
    "sender", ["News <news@example.org>", "news@sub.example.com", "not-an-address", "News <news@example.com.example.net>"]
)
def test_sender_outside_allow_list_is_refused(mcp, mock, audit, sender):
    result = mcp.call("send_email", **{"from": sender}, to=["alice@example.com"], subject="Hi", text="x", confirm=True)
    assert result["isError"]
    assert mock.requests == []
    assert audit.rows[-1]["outcome"] == "refused"
    result = mcp.call("create_campaign", name="Oct", **{"from": sender}, subject="News", book_id="b1", html=UNSUB_HTML)
    assert result["isError"]
    assert mock.requests == []


def test_allowed_subdomain_when_listed(mcp, mock):
    result = mcp.call("send_email", **{"from": "a@mail.example.com"}, to=["bob@example.com"], subject="Hi", text="x")
    assert not result["isError"]


def test_more_than_50_recipients_is_refused(mcp, mock, audit):
    to = [f"user{i}@example.com" for i in range(40)]
    bcc = [f"hidden{i}@example.com" for i in range(11)]
    result = mcp.call("send_email", **{"from": SENDER}, to=to, bcc=bcc, subject="Hi", text="x", confirm=True)
    assert result["isError"]
    assert "51 recipients" in mcp.text(result)
    assert mock.requests == []


# ─── audit fail-closed / fail-open ───────────────────────────────────────────


def test_audit_failure_blocks_mutating_tools(mcp, mock, audit):
    audit.fail_record = True
    for name, args in [
        ("pause_campaign", {"id": "c1"}),
        ("create_contact_book", {"name": "Customers"}),
        ("send_email", {"from": SENDER, "to": ["alice@example.com"], "subject": "Hi", "text": "x", "confirm": True}),
    ]:
        result = mcp.call(name, **args)
        assert result["isError"], name
        assert "audit" in mcp.text(result).lower()
    assert mock.requests == []


def test_audit_failure_does_not_block_read_only_tools(mcp, mock, audit):
    audit.fail_record = True
    mock.routes[("GET", "/api/v1/domains")] = [{"id": 1, "name": "example.com", "status": "SUCCESS"}]
    result = mcp.call("list_domains")
    assert not result["isError"]
    assert mcp.data(result)[0]["name"] == "example.com"
    assert len(mock.requests) == 1


def test_read_only_calls_are_audited(mcp, mock, audit):
    mcp.call("get_campaign", id="c1")
    assert audit.rows == [
        {
            "actor": "alice",
            "tool": "get_campaign",
            "mutating": False,
            "args": {"id": "c1"},
            "outcome": "ok",
            "error": None,
            "duration_ms": audit.rows[0]["duration_ms"],
            "email_ids": None,
            "campaign_id": None,
            "contact_book_id": None,
        }
    ]


def test_api_error_is_reported_and_audited(mcp, mock, audit):
    mock.replies.append((404, b'{"error": {"message": "Campaign not found"}}'))
    result = mcp.call("pause_campaign", id="nope")
    assert result["isError"]
    assert "Campaign not found" in mcp.text(result)
    assert audit.rows[-1]["outcome"] == "error"
    assert "HTTP 404" in audit.rows[-1]["error"]


# ─── contacts ────────────────────────────────────────────────────────────────


def test_import_contacts_chunks_and_redacts(mcp, mock, audit):
    csv_text = "email,firstName,city\n" + "".join(f"u{i}@example.com,U{i},Paris\n" for i in range(1500))
    result = mcp.call("import_contacts", book_id="b1", csv_text=csv_text)
    assert not result["isError"]
    assert [len(r["body"]) for r in mock.writes] == [1000, 500]
    assert mock.writes[0]["body"][0] == {"email": "u0@example.com", "firstName": "U0", "properties": {"city": "Paris"}}
    args = audit.rows[-1]["args"]
    assert args["csv_text"]["rows"] == 1500
    assert "u0@example.com" not in str(args)


def test_import_contacts_refuses_more_than_5000_rows(mcp, mock, audit):
    csv_text = "email\n" + "".join(f"u{i}@example.com\n" for i in range(5001))
    result = mcp.call("import_contacts", book_id="b1", csv_text=csv_text)
    assert result["isError"]
    assert mock.requests == []
    assert audit.rows[-1]["outcome"] == "refused"


def test_update_contact_unsubscribe(mcp, mock):
    mcp.call("update_contact", book_id="b1", contact_id="k1", subscribed=False)
    assert mock.writes[0]["method"] == "PATCH"
    assert mock.writes[0]["path"] == "/api/v1/contactBooks/b1/contacts/k1"
    assert mock.writes[0]["body"] == {"subscribed": False}


# ─── config ──────────────────────────────────────────────────────────────────

ENV = {
    "MCP_USESEND_API_KEY": "us_x",
    "USESEND_BASE_URL": "http://usesend:3000",
    "MCP_DATABASE_URL": "postgresql://mcp@db/usesend",
    "MCP_ALLOWED_FROM_DOMAINS": "Example.com, mail.example.com",
}


def test_config_from_env_defaults():
    cfg = Config.from_env(ENV)
    assert cfg.allowed_from_domains == ("example.com", "mail.example.com")
    assert cfg.audit_schema == "mcp"
    assert cfg.port == 8080


@pytest.mark.parametrize("drop", list(ENV))
def test_config_requires_each_variable(drop):
    env = {k: v for k, v in ENV.items() if k != drop}
    with pytest.raises(ConfigError, match=drop):
        Config.from_env(env)


def test_config_rejects_bad_schema():
    with pytest.raises(ConfigError, match="MCP_AUDIT_SCHEMA"):
        Config.from_env({**ENV, "MCP_AUDIT_SCHEMA": "mcp; drop"})


@pytest.mark.parametrize(
    "sender, domain",
    [
        ("News <news@Example.com>", "example.com"),
        ("news@example.com", "example.com"),
        ('"Doe, Jane" <jane@mail.example.com>', "mail.example.com"),
        ("nobody", None),
        ("@example.com", None),
    ],
)
def test_sender_domain(sender, domain):
    assert sender_domain(sender) == domain
