"""Caller-supplied ids stay one URL path segment: they can't reach another endpoint.

Regression for an id such as "c2/schedule#" turning `pause_campaign` into
POST /campaigns/c2/schedule, which would send a campaign without confirm.
"""

from __future__ import annotations

import urllib.parse

import pytest
from conftest import TOKENS, McpClient
from starlette.testclient import TestClient

from usesend_mcp import cli
from usesend_mcp.auth import parse_tokens
from usesend_mcp.client import Client, InvalidId, seg
from usesend_mcp.server import UseSendTools, create_app

HOSTILE = ["c2/schedule#", "c2/resume?x=1", "../campaigns/c2/schedule#", "a b%2F"]

# tool, arguments (X = the id under test), the one path useSend must see ({} = the id, encoded)
CASES = [
    ("get_contact_book", {"id": "X"}, "GET", "/api/v1/contactBooks/{}"),
    ("list_contacts", {"book_id": "X"}, "GET", "/api/v1/contactBooks/{}/contacts"),
    ("get_campaign", {"id": "X"}, "GET", "/api/v1/campaigns/{}"),
    ("get_email", {"id": "X"}, "GET", "/api/v1/emails/{}"),
    ("add_contact", {"book_id": "X", "email": "alice@example.com"}, "POST", "/api/v1/contactBooks/{}/contacts"),
    (
        "import_contacts",
        {"book_id": "X", "csv_text": "email\nalice@example.com"},
        "POST",
        "/api/v1/contactBooks/{}/contacts/bulk",
    ),
    (
        "update_contact",
        {"book_id": "X", "contact_id": "k1", "subscribed": False},
        "PATCH",
        "/api/v1/contactBooks/{}/contacts/k1",
    ),
    (
        "update_contact",
        {"book_id": "b1", "contact_id": "X", "subscribed": False},
        "PATCH",
        "/api/v1/contactBooks/b1/contacts/{}",
    ),
    ("cancel_email", {"id": "X"}, "POST", "/api/v1/emails/{}/cancel"),
    ("schedule_campaign", {"id": "X"}, "GET", "/api/v1/campaigns/{}"),
    ("schedule_campaign", {"id": "X", "confirm": True}, "POST", "/api/v1/campaigns/{}/schedule"),
    ("pause_campaign", {"id": "X"}, "POST", "/api/v1/campaigns/{}/pause"),
    ("resume_campaign", {"id": "X"}, "GET", "/api/v1/campaigns/{}"),
    ("resume_campaign", {"id": "X", "confirm": True}, "POST", "/api/v1/campaigns/{}/resume"),
]


@pytest.fixture
def mcp(mock, audit):
    tools = UseSendTools(Client(mock.url, "us_testkey"), audit, ("example.com",))
    with TestClient(create_app(parse_tokens(TOKENS), tools), base_url="http://usesend.example.com") as http:
        yield McpClient(http)


@pytest.mark.parametrize("hostile", HOSTILE)
@pytest.mark.parametrize(("tool", "args", "method", "path"), CASES, ids=lambda v: v if isinstance(v, str) else None)
def test_tool_ids_are_one_path_segment(mcp, mock, tool, args, method, path, hostile):
    mcp.call(tool, **{k: hostile if v == "X" else v for k, v in args.items()})
    expected = path.format(urllib.parse.quote(hostile, safe=""))
    assert [(r["method"], r["path"]) for r in mock.requests] == [(method, expected)]


@pytest.mark.parametrize("bad", ["", ".", ".."])
def test_dot_and_empty_ids_are_refused_before_any_request(mcp, mock, audit, bad):
    result = mcp.call("pause_campaign", id=bad)
    assert result["isError"]
    assert "Refused" in mcp.text(result)
    assert mock.requests == []
    assert audit.rows[-1]["outcome"] == "refused"


def test_seg_encodes_everything_but_unreserved():
    assert seg("c2/schedule#") == "c2%2Fschedule%23"
    assert seg("a?b=c") == "a%3Fb%3Dc"
    assert seg(42) == "42"
    for bad in ("", ".", ".."):
        with pytest.raises(InvalidId):
            seg(bad)


@pytest.mark.parametrize("path", ["/v1/campaigns/c2/schedule#", "/v1/emails/x?y=1", "/v1/emails/../campaigns/c2"])
def test_client_refuses_a_path_built_without_seg(mock, path):
    with pytest.raises(InvalidId):
        Client(mock.url, "us_testkey").post(path)
    assert mock.requests == []


def test_cli_ids_are_one_path_segment(mock, monkeypatch, capsys):
    monkeypatch.setenv("USESEND_API_KEY", "us_testkey")
    assert cli.main(["--url", mock.url, "campaigns", "pause", "c2/schedule#"]) == 0
    assert [(r["method"], r["path"]) for r in mock.requests] == [("POST", "/api/v1/campaigns/c2%2Fschedule%23/pause")]
