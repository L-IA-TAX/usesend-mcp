"""HTTP client for the useSend public API (v1.9.8). Standard library only.

Shared by the CLI (`cli.py`) and the MCP server (`server.py`).
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable

BULK_CONTACTS_MAX = 1000  # API limit per bulk add/delete call
BATCH_EMAILS_MAX = 100  # API limit per /emails/batch call
# Campaigns are refused at send/schedule time without one of these (campaign-service.ts).
UNSUB_RE = re.compile(r"\{\{\s*(unsend|usesend)_unsubscribe_url\s*\}\}", re.I)


class ApiError(Exception):
    pass


class InvalidId(ApiError):
    """A caller-supplied id that can't be used as one URL path segment."""


def seg(value) -> str:
    """One URL path segment from a caller-supplied id.

    Percent-encoded with nothing kept safe, so '/', '?' and '#' can't change which
    endpoint is called. Encoding leaves '.' and '..' as they are, so those are refused.
    """
    s = str(value)
    if s in ("", ".", ".."):
        raise InvalidId(f"invalid id: {s!r}")
    return urllib.parse.quote(s, safe="")


class Client:
    def __init__(self, base_url: str, key: str | None, dry_run: bool = False, user_agent: str = "usesend-mcp-cli/1"):
        self.base = base_url.rstrip("/") + "/api"
        self.key = key
        self.dry_run = dry_run
        self.user_agent = user_agent

    def request(self, method, path, body=None, query=None, headers=None):
        # Backstop for an id interpolated without seg(): it must not reach another endpoint.
        if "?" in path or "#" in path or any(p in (".", "..") for p in path.split("/")):
            raise InvalidId(f"refusing path {path!r}: build ids with seg()")
        url = self.base + path
        if query:
            q = {k: v for k, v in query.items() if v is not None}
            if q:
                url += "?" + urllib.parse.urlencode(q, doseq=True)
        if self.dry_run:
            print(f"[dry-run] {method} {url}", file=sys.stderr)
            if body is not None:
                print(json.dumps(body, indent=2, ensure_ascii=False), file=sys.stderr)
            return None
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        # useSend Cloud rate-limits per team (2 req/s by default): back off on 429.
        for attempt in range(6):
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                if e.code == 429 and attempt < 5:
                    time.sleep(min(2**attempt, 10))
                    continue
                try:
                    detail = json.loads(raw)
                    detail = detail.get("error", detail)
                except json.JSONDecodeError:
                    # Usually an HTML login/error page from a proxy in front of the API.
                    detail = raw[:300]
                raise ApiError(f"{method} {path} → HTTP {e.code}: {detail}") from None
            except urllib.error.URLError as e:
                raise ApiError(f"{method} {path} → {e.reason}") from None
        raise ApiError(f"{method} {path} → still rate-limited after retries")

    def get(self, path, **query):
        return self.request("GET", path, query=query)

    def post(self, path, body=None, headers=None):
        return self.request("POST", path, body=body if body is not None else {}, headers=headers)

    def patch(self, path, body):
        return self.request("PATCH", path, body=body)

    def put(self, path, body=None):
        return self.request("PUT", path, body=body)

    def delete(self, path, body=None):
        return self.request("DELETE", path, body=body)


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def parse_contacts_csv(lines: Iterable[str]) -> list[dict]:
    """CSV with an `email` column → bulk-contact items.

    firstName/lastName (or first_name/last_name) map to fields, `subscribed` to a
    bool, every other non-empty column to a property. Rows without an email are skipped.
    """
    rows = []
    for r in csv.DictReader(lines):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        email = r.pop("email", "")
        if not email:
            continue
        item = {"email": email}
        first = r.pop("firstName", "") or r.pop("first_name", "")
        last = r.pop("lastName", "") or r.pop("last_name", "")
        r.pop("first_name", None), r.pop("last_name", None)
        if first:
            item["firstName"] = first
        if last:
            item["lastName"] = last
        sub = r.pop("subscribed", "")
        if sub:
            item["subscribed"] = sub.lower() in ("1", "true", "yes", "sim")
        props = {k: v for k, v in r.items() if k and v}
        if props:
            item["properties"] = props
        rows.append(item)
    return rows
