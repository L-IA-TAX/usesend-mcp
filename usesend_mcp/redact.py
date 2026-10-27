"""Tool arguments → audit-safe JSON.

Bodies and CSV content are replaced by a hash and a size; attachments by their
name and size. Recipients, subject, sender, names and ids are kept: they are what
the audit is for.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
from typing import Any

HASHED_FIELDS = ("html", "text", "csv_text")


def digest(value: str) -> dict[str, Any]:
    data = value.encode()
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def csv_rows(value: str) -> int:
    """Data rows (excluding the header) in a CSV document."""
    return max(sum(1 for _ in csv.reader(io.StringIO(value))) - 1, 0)


def attachment_size(attachment: dict) -> int:
    content = attachment.get("content") or ""
    try:
        return len(base64.b64decode(content, validate=True))
    except (binascii.Error, ValueError):
        return len(content.encode())


def redact(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key in HASHED_FIELDS and isinstance(value, str):
            out[key] = digest(value)
            if key == "csv_text":
                out[key]["rows"] = csv_rows(value)
        elif key == "attachments" and isinstance(value, list):
            out[key] = [
                {"filename": a.get("filename"), "bytes": attachment_size(a)} for a in value if isinstance(a, dict)
            ]
        else:
            out[key] = value
    return out
