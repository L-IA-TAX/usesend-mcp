"""Sentry scrubber and opt-in initialisation."""

from __future__ import annotations

import copy
import json

import pytest

from usesend_mcp import scrub
from usesend_mcp.scrub import Scrubber, init_sentry

TOKEN = "c0ffee" * 10 + "abcd"  # 64 chars
API_KEY = "us_live_k3y_0123456789"
HTML = "<p>Secret launch body</p>"
TEXT = "plain secret body"
CSV = "email,firstName\ncarol@example.com,Carol\n"
RECIPIENTS = ["alice@example.com", "bob@example.com", "dave@example.com", "erin@example.com"]
SENDER = "News <news@example.com>"

SECRETS = [TOKEN, API_KEY, HTML, TEXT, "Secret launch", "carol@example.com", "news@example.com", *RECIPIENTS]

TOOL_ARGS = {
    "html": HTML,
    "text": TEXT,
    "csv_text": CSV,
    "to": RECIPIENTS[:1],
    "cc": RECIPIENTS[1:2],
    "bcc": RECIPIENTS[2:3],
    "from": SENDER,
    "reply_to": RECIPIENTS[3:],
}


def synthetic_event() -> dict:
    return {
        "event_id": "0123456789abcdef0123456789abcdef",
        "level": "error",
        "tags": {"tool": "send_email", "actor": "alice"},
        "user": {"id": "alice", "ip_address": "203.0.113.7", "email": "alice@example.com"},
        "request": {
            "method": "POST",
            "url": f"https://usesend.example.com/?token={TOKEN}",
            "headers": {"Authorization": f"Bearer {TOKEN}", "Host": "usesend.example.com"},
            "data": json.dumps({"params": {"arguments": TOOL_ARGS}}),
            "query_string": f"token={TOKEN}",
            "cookies": {"session": TOKEN},
            "env": {"REMOTE_ADDR": "203.0.113.7"},
        },
        "extra": {"arguments": TOOL_ARGS, "note": f"called with key {API_KEY} by alice@example.com", **TOOL_ARGS},
        "contexts": {
            "trace": {"trace_id": "a" * 32, "span_id": "b" * 16, "op": "http.server"},
            "tool": {"name": "send_email", "arguments": TOOL_ARGS, "authorization": f"Bearer {TOKEN}"},
        },
        "breadcrumbs": {
            "values": [
                {
                    "category": "httplib",
                    "type": "http",
                    "level": "info",
                    "message": f"POST https://usesend.example.com/api/v1/emails Authorization: Bearer {API_KEY}",
                    "data": {"url": "https://usesend.example.com/api/v1/emails", "body": TOOL_ARGS},
                },
                {"category": "log", "level": "info", "message": f"sending {HTML} to {RECIPIENTS[0]} token={TOKEN}"},
            ]
        },
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": f"boom for {RECIPIENTS[0]} with {API_KEY} and Bearer {TOKEN}: {HTML}",
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "usesend_mcp/server.py",
                                "function": "send_email",
                                "module": "usesend_mcp.server",
                                "lineno": 321,
                                "in_app": True,
                                "vars": {"html": HTML, "to": RECIPIENTS, "token": TOKEN},
                            }
                        ]
                    },
                }
            ]
        },
        "logentry": {"message": "tool %s failed", "params": [TOOL_ARGS], "formatted": f"tool failed {TEXT}"},
    }


def test_scrubbed_event_carries_no_arguments_tokens_or_keys():
    event = Scrubber([TOKEN, API_KEY]).event(synthetic_event())
    dumped = json.dumps(event)
    for secret in SECRETS:
        assert secret not in dumped, secret
    assert "Authorization" not in dumped
    assert "203.0.113.7" not in dumped


def test_scrubber_works_without_known_secrets():
    """Pattern rules alone catch bearer tokens, us_ keys and long tokens."""
    dumped = json.dumps(Scrubber().event(synthetic_event()))
    for secret in SECRETS:
        assert secret not in dumped, secret


def test_scrubbed_event_keeps_what_helps_debugging():
    event = Scrubber([TOKEN, API_KEY]).event(synthetic_event())
    assert event["tags"] == {"tool": "send_email", "actor": "alice"}
    assert event["event_id"] == "0123456789abcdef0123456789abcdef"
    assert event["contexts"]["trace"]["trace_id"] == "a" * 32
    exc = event["exception"]["values"][0]
    assert exc["type"] == "RuntimeError"
    frame = exc["stacktrace"]["frames"][0]
    assert (frame["function"], frame["lineno"], frame["module"]) == ("send_email", 321, "usesend_mcp.server")
    assert "vars" not in frame
    assert event["request"] == {"method": "POST", "url": "https://usesend.example.com/"}
    assert event["breadcrumbs"]["values"][0]["category"] == "httplib"


def test_breadcrumb_hook_drops_data():
    crumb = {"category": "httplib", "level": "info", "data": {"body": TOOL_ARGS}, "message": f"Bearer {TOKEN}"}
    out = Scrubber().breadcrumb(copy.deepcopy(crumb))
    assert "data" not in out
    assert TOKEN not in json.dumps(out)


@pytest.mark.parametrize("env", [{}, {"MCP_SENTRY_DSN": "none"}, {"MCP_SENTRY_DSN": "None"}, {"MCP_SENTRY_DSN": " "}])
def test_disabled_sentry_is_not_initialised(monkeypatch, env):
    calls = []
    monkeypatch.setattr(scrub.sentry_sdk, "init", lambda **kw: calls.append(kw))
    assert init_sentry(env) is False
    assert calls == []


def test_enabled_sentry_options(monkeypatch):
    calls = []
    monkeypatch.setattr(scrub.sentry_sdk, "init", lambda **kw: calls.append(kw))
    env = {"MCP_SENTRY_DSN": "https://public@example.com/1", "MCP_SENTRY_ENVIRONMENT": "production"}
    assert init_sentry(env, secrets=[TOKEN]) is True
    (kw,) = calls
    assert kw["dsn"] == "https://public@example.com/1"
    assert kw["environment"] == "production"
    assert kw["send_default_pii"] is False
    assert kw["include_local_variables"] is False
    assert kw["max_request_body_size"] == "never"
    # The hooks are the scrubber's.
    event = kw["before_send"](synthetic_event(), {})
    assert TOKEN not in json.dumps(event)
    assert "data" not in kw["before_breadcrumb"]({"category": "x", "data": {"to": RECIPIENTS}}, {})


def test_unexpected_tool_errors_reach_the_error_hook(audit):
    from usesend_mcp.client import Client
    from usesend_mcp.server import UseSendTools, current_actor

    reported = []
    tools = UseSendTools(
        Client("http://127.0.0.1:1", "us_x"), audit, ("example.com",), on_unexpected_error=lambda *a: reported.append(a)
    )

    def broken(info):
        raise RuntimeError("bug")

    reset = current_actor.set("alice")
    try:
        with pytest.raises(RuntimeError):
            tools.call("get_campaign", False, {"id": "c1"}, broken)
    finally:
        current_actor.reset(reset)
    ((exc, tool, actor),) = reported
    assert (type(exc), tool, actor) == (RuntimeError, "get_campaign", "alice")
    assert audit.rows[-1]["outcome"] == "error"
    assert audit.rows[-1]["error"] == "RuntimeError"
