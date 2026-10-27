"""Audit argument redaction."""

from __future__ import annotations

import base64
import hashlib

from usesend_mcp.redact import redact


def test_bodies_hashed_recipients_kept():
    html = "<p>Hello {{firstName}}</p>"
    args = {
        "from": "News <news@example.com>",
        "to": ["alice@example.com", "bob@example.com"],
        "cc": ["carol@example.com"],
        "subject": "Hello",
        "html": html,
        "text": "Hello",
        "confirm": True,
    }
    out = redact(args)
    assert out["html"] == {"sha256": hashlib.sha256(html.encode()).hexdigest(), "bytes": len(html.encode())}
    assert out["text"]["bytes"] == 5
    for key in ("from", "to", "cc", "subject", "confirm"):
        assert out[key] == args[key]
    assert "Hello {{firstName}}" not in str(out)


def test_csv_hashed_with_row_count():
    csv_text = "email,firstName\nalice@example.com,Alice\nbob@example.com,Bob\n"
    out = redact({"book_id": "b1", "csv_text": csv_text})
    assert out["book_id"] == "b1"
    assert out["csv_text"]["rows"] == 2
    assert out["csv_text"]["bytes"] == len(csv_text)
    assert "alice@example.com" not in str(out)


def test_attachments_reduced_to_name_and_size():
    content = base64.b64encode(b"x" * 10).decode()
    out = redact({"attachments": [{"filename": "a.pdf", "content": content}]})
    assert out == {"attachments": [{"filename": "a.pdf", "bytes": 10}]}


def test_none_bodies_left_alone():
    assert redact({"html": None, "text": None}) == {"html": None, "text": None}
