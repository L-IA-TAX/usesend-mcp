#!/usr/bin/env python3
"""CLI for the useSend public API.

Covers what the API exposes as of useSend v1.9.8: emails (send / batch / list /
get / reschedule / cancel), contact books, contacts (add / upsert / CSV import /
unsubscribe / delete), campaigns (create / schedule / pause / resume / delete),
domains (read + verify) and analytics. Standard library only — run it with any
python3: `python3 -m usesend_mcp.cli …`.

API key (a useSend key, `us_…`, created in the UI under Developer settings → API
keys), first match wins:
  --key KEY  |  $USESEND_API_KEY  |  --key-file PATH  |  ~/.config/usesend/key
Base URL (without /api): --url  |  $USESEND_BASE_URL. There is no default.

Anything that sends mail or deletes asks for confirmation; `--yes` skips the
prompt, `--dry-run` prints the request and sends nothing.

Examples:
  cli emails send --from 'News <news@example.com>' --to alice@example.com \\
      --subject Hello --html-file body.html
  cli books create --name 'Customers'
  cli contacts import <bookId> contacts.csv               # email,firstName,lastName,…
  cli campaigns create --name 'Oct/26' --from 'News <news@example.com>' \\
      --subject 'News' --book <bookId> --html-file news.html
  cli campaigns schedule <campaignId> --at 'tomorrow 9am'
  cli campaigns get <campaignId>                          # sent/opened/clicked…
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_KEY_FILE = Path.home() / ".config" / "usesend" / "key"
BULK_CONTACTS_MAX = 1000  # API limit per bulk add/delete call
BATCH_EMAILS_MAX = 100  # API limit per /emails/batch call
# Campaigns are refused at send/schedule time without one of these (campaign-service.ts).
UNSUB_RE = re.compile(r"\{\{\s*(unsend|usesend)_unsubscribe_url\s*\}\}", re.I)


class ApiError(Exception):
    pass


# ─── HTTP ────────────────────────────────────────────────────────────────────


class Client:
    def __init__(self, base_url: str, key: str | None, dry_run: bool = False):
        self.base = base_url.rstrip("/") + "/api"
        self.key = key
        self.dry_run = dry_run

    def request(self, method, path, body=None, query=None, headers=None):
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
            "User-Agent": "usesend-mcp-cli/1",
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


# ─── helpers ─────────────────────────────────────────────────────────────────


def resolve_key(args) -> str | None:
    if args.key:
        return args.key
    if os.environ.get("USESEND_API_KEY"):
        return os.environ["USESEND_API_KEY"]
    path = Path(args.key_file).expanduser() if args.key_file else DEFAULT_KEY_FILE
    if path.is_file():
        return path.read_text().strip()
    return None


def confirm(args, what: str) -> None:
    if args.yes or args.dry_run:
        return
    if not sys.stdin.isatty():
        sys.exit(f"refusing to {what} without --yes (stdin is not a terminal)")
    ans = input(f"About to {what}. Type 'yes' to continue: ")
    if ans.strip().lower() != "yes":
        sys.exit("aborted")


def out(obj) -> None:
    if obj is not None:
        print(json.dumps(obj, indent=2, ensure_ascii=False))


def read_text(inline: str | None, file: str | None) -> str | None:
    if file:
        return Path(file).read_text(encoding="utf-8")
    return inline


def as_list(values):
    """argparse `append` → None / single string / list, as the API accepts."""
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def parse_vars(pairs) -> dict | None:
    if not pairs:
        return None
    result = {}
    for p in pairs:
        k, sep, v = p.partition("=")
        if not sep:
            sys.exit(f"--var expects key=value, got {p!r}")
        result[k] = v
    return result


def attachments(paths):
    if not paths:
        return None
    return [
        {"filename": Path(p).name, "content": base64.b64encode(Path(p).read_bytes()).decode()}
        for p in paths
    ]


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ─── emails ──────────────────────────────────────────────────────────────────


def emails_send(c, a):
    html = read_text(a.html, a.html_file)
    text = read_text(a.text, a.text_file)
    if not (html or text):
        sys.exit("need --html/--html-file or --text/--text-file")
    if not (a.subject or a.template_id):
        sys.exit("need --subject (or --template-id)")
    body = drop_none(
        {
            "from": a.sender,
            "to": as_list(a.to),
            "subject": a.subject,
            "templateId": a.template_id,
            "variables": parse_vars(a.var),
            "html": html,
            "text": text,
            "replyTo": as_list(a.reply_to),
            "cc": as_list(a.cc),
            "bcc": as_list(a.bcc),
            "attachments": attachments(a.attach),
            "scheduledAt": a.schedule,
        }
    )
    n = len(a.to) + len(a.cc or []) + len(a.bcc or [])
    confirm(a, f"send '{a.subject or a.template_id}' to {n} recipient(s)")
    hdrs = {"Idempotency-Key": a.idempotency_key} if a.idempotency_key else None
    out(c.post("/v1/emails", body, headers=hdrs))


def emails_batch(c, a):
    items = json.loads(Path(a.file).read_text(encoding="utf-8"))
    if not isinstance(items, list):
        sys.exit("batch file must be a JSON array of email objects (same shape as /v1/emails)")
    confirm(a, f"send {len(items)} email(s) in batches of {BATCH_EMAILS_MAX}")
    ids = []
    for part in chunks(items, BATCH_EMAILS_MAX):
        r = c.post("/v1/emails/batch", part)
        if r:
            ids.extend(r.get("data", []))
    out({"data": ids} if not a.dry_run else None)


def emails_list(c, a):
    out(
        c.get(
            "/v1/emails",
            page=a.page,
            limit=a.limit,
            startDate=a.start,
            endDate=a.end,
            domainId=a.domain,
        )
    )


def emails_get(c, a):
    out(c.get(f"/v1/emails/{a.id}"))


def emails_reschedule(c, a):
    out(c.patch(f"/v1/emails/{a.id}", {"scheduledAt": a.at}))


def emails_cancel(c, a):
    out(c.post(f"/v1/emails/{a.id}/cancel"))


# ─── contact books ───────────────────────────────────────────────────────────


def books_list(c, a):
    out(c.get("/v1/contactBooks"))


def books_get(c, a):
    out(c.get(f"/v1/contactBooks/{a.id}"))


def books_create(c, a):
    out(c.post("/v1/contactBooks", drop_none({"name": a.name, "emoji": a.emoji})))


def books_update(c, a):
    body = drop_none({"name": a.name, "emoji": a.emoji})
    if not body:
        sys.exit("nothing to update (--name/--emoji)")
    out(c.patch(f"/v1/contactBooks/{a.id}", body))


def books_delete(c, a):
    confirm(a, f"DELETE contact book {a.id} and all its contacts")
    out(c.delete(f"/v1/contactBooks/{a.id}"))


# ─── contacts ────────────────────────────────────────────────────────────────


def contacts_list(c, a):
    out(
        c.get(
            f"/v1/contactBooks/{a.book}/contacts",
            emails=",".join(a.email) if a.email else None,
            page=a.page,
            limit=a.limit,
        )
    )


def contacts_get(c, a):
    out(c.get(f"/v1/contactBooks/{a.book}/contacts/{a.id}"))


def _contact_body(a):
    return drop_none(
        {
            "email": getattr(a, "email", None),
            "firstName": a.first_name,
            "lastName": a.last_name,
            "properties": parse_vars(a.prop),
            "subscribed": a.subscribed,
        }
    )


def contacts_add(c, a):
    out(c.post(f"/v1/contactBooks/{a.book}/contacts", _contact_body(a)))


def contacts_update(c, a):
    body = _contact_body(a)
    if not body:
        sys.exit("nothing to update")
    out(c.patch(f"/v1/contactBooks/{a.book}/contacts/{a.id}", body))


def contacts_unsubscribe(c, a):
    out(c.patch(f"/v1/contactBooks/{a.book}/contacts/{a.id}", {"subscribed": False}))


def contacts_delete(c, a):
    confirm(a, f"delete contact {a.id}")
    out(c.delete(f"/v1/contactBooks/{a.book}/contacts/{a.id}"))


def contacts_import(c, a):
    """CSV with an `email` column; firstName/lastName (or first_name/last_name)
    map to fields, `subscribed` to a bool, every other column to a property."""
    rows = []
    with open(a.csv, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
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
    if not rows:
        sys.exit("no rows with an email found")
    print(f"{len(rows)} contact(s) → book {a.book}", file=sys.stderr)
    results = []
    for part in chunks(rows, BULK_CONTACTS_MAX):
        results.append(c.post(f"/v1/contactBooks/{a.book}/contacts/bulk", part))
    out(results if not a.dry_run else None)


# ─── campaigns ───────────────────────────────────────────────────────────────


def campaigns_list(c, a):
    out(c.get("/v1/campaigns", page=a.page, status=a.status, search=a.search))


def campaigns_get(c, a):
    out(c.get(f"/v1/campaigns/{a.id}"))


def campaigns_create(c, a):
    html = read_text(a.html, a.html_file)
    if not html:
        sys.exit("need --html or --html-file")
    if not UNSUB_RE.search(html):
        msg = "html has no {{usesend_unsubscribe_url}} link — useSend refuses to send/schedule it"
        if a.send_now or a.schedule:
            sys.exit(msg)
        print(f"warning: {msg}", file=sys.stderr)
    body = drop_none(
        {
            "name": a.name,
            "from": a.sender,
            "subject": a.subject,
            "previewText": a.preview_text,
            "contactBookId": a.book,
            "html": html,
            "replyTo": as_list(a.reply_to),
            "cc": as_list(a.cc),
            "bcc": as_list(a.bcc),
            "sendNow": True if a.send_now else None,
            "scheduledAt": a.schedule,
            "batchSize": a.batch_size,
        }
    )
    if a.send_now:
        confirm(a, f"create AND SEND NOW campaign '{a.name}' to every subscribed contact of book {a.book}")
    elif a.schedule:
        confirm(a, f"create campaign '{a.name}' scheduled for {a.schedule} (book {a.book})")
    out(c.post("/v1/campaigns", body))


def campaigns_schedule(c, a):
    when = "now" if not a.at else a.at
    confirm(a, f"schedule campaign {a.id} to send {when}")
    out(c.post(f"/v1/campaigns/{a.id}/schedule", drop_none({"scheduledAt": a.at, "batchSize": a.batch_size})))


def campaigns_pause(c, a):
    out(c.post(f"/v1/campaigns/{a.id}/pause"))


def campaigns_resume(c, a):
    confirm(a, f"resume sending campaign {a.id}")
    out(c.post(f"/v1/campaigns/{a.id}/resume"))


def campaigns_delete(c, a):
    confirm(a, f"delete campaign {a.id}")
    out(c.delete(f"/v1/campaigns/{a.id}"))


# ─── domains / analytics ─────────────────────────────────────────────────────


def domains_list(c, a):
    out(c.get("/v1/domains"))


def domains_get(c, a):
    out(c.get(f"/v1/domains/{a.id}"))


def domains_verify(c, a):
    out(c.put(f"/v1/domains/{a.id}/verify"))


def analytics_series(c, a):
    out(c.get("/v1/analytics/email-time-series", days=a.days, domainId=a.domain))


def analytics_reputation(c, a):
    out(c.get("/v1/analytics/reputation-metrics", domainId=a.domain))


# ─── argparse ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m usesend_mcp.cli",
        description="useSend public API CLI (emails, contacts, campaigns).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1],
    )
    p.add_argument("--key", help="API key (else $USESEND_API_KEY, --key-file, ~/.config/usesend/key)")
    p.add_argument("--key-file", help="file holding the API key")
    p.add_argument("--url", default=os.environ.get("USESEND_BASE_URL"), help="base URL without /api (else $USESEND_BASE_URL)")
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompts")
    p.add_argument("-n", "--dry-run", action="store_true", help="print requests, send nothing")
    top = p.add_subparsers(dest="group", required=True)

    def group(name, help_):
        return top.add_parser(name, help=help_).add_subparsers(dest="cmd", required=True)

    def cmd(sub, name, fn, help_=None):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    def recipients(sp):
        sp.add_argument("--reply-to", action="append")
        sp.add_argument("--cc", action="append")
        sp.add_argument("--bcc", action="append")

    # emails
    g = group("emails", "transactional emails")
    s = cmd(g, "send", emails_send, "send one email")
    s.add_argument("--from", dest="sender", required=True, help="'Name <addr@example.com>'")
    s.add_argument("--to", action="append", required=True, help="repeatable")
    s.add_argument("--subject")
    s.add_argument("--html")
    s.add_argument("--html-file")
    s.add_argument("--text")
    s.add_argument("--text-file")
    s.add_argument("--template-id")
    s.add_argument("--var", action="append", help="template variable key=value (repeatable)")
    s.add_argument("--attach", action="append", help="file path (repeatable, max 10)")
    s.add_argument("--schedule", help="ISO 8601 with offset, e.g. 2026-10-07T09:00:00+00:00")
    s.add_argument("--idempotency-key")
    recipients(s)
    s = cmd(g, "batch", emails_batch, "send a JSON array of emails (chunked by 100)")
    s.add_argument("file")
    s = cmd(g, "list", emails_list)
    s.add_argument("--page")
    s.add_argument("--limit")
    s.add_argument("--start", help="ISO datetime")
    s.add_argument("--end", help="ISO datetime")
    s.add_argument("--domain", action="append")
    cmd(g, "get", emails_get).add_argument("id")
    s = cmd(g, "reschedule", emails_reschedule)
    s.add_argument("id")
    s.add_argument("--at", required=True, help="ISO datetime")
    cmd(g, "cancel", emails_cancel, "cancel a scheduled email").add_argument("id")

    # contact books
    g = group("books", "contact books (campaign audiences)")
    cmd(g, "list", books_list)
    cmd(g, "get", books_get).add_argument("id")
    s = cmd(g, "create", books_create)
    s.add_argument("--name", required=True)
    s.add_argument("--emoji")
    s = cmd(g, "update", books_update)
    s.add_argument("id")
    s.add_argument("--name")
    s.add_argument("--emoji")
    cmd(g, "delete", books_delete).add_argument("id")

    # contacts
    g = group("contacts", "contacts inside a book")

    def contact_fields(sp):
        sp.add_argument("--first-name")
        sp.add_argument("--last-name")
        sp.add_argument("--prop", action="append", help="property key=value (repeatable)")
        sp.add_argument("--subscribed", type=lambda v: v.lower() in ("1", "true", "yes"), default=None)

    s = cmd(g, "list", contacts_list)
    s.add_argument("book")
    s.add_argument("--email", action="append")
    s.add_argument("--page")
    s.add_argument("--limit")
    s = cmd(g, "get", contacts_get)
    s.add_argument("book")
    s.add_argument("id")
    s = cmd(g, "add", contacts_add)
    s.add_argument("book")
    s.add_argument("email")
    contact_fields(s)
    s = cmd(g, "update", contacts_update)
    s.add_argument("book")
    s.add_argument("id")
    contact_fields(s)
    s = cmd(g, "unsubscribe", contacts_unsubscribe)
    s.add_argument("book")
    s.add_argument("id")
    s = cmd(g, "delete", contacts_delete)
    s.add_argument("book")
    s.add_argument("id")
    s = cmd(g, "import", contacts_import, "bulk-add from CSV (email[,firstName,lastName,subscribed,…props])")
    s.add_argument("book")
    s.add_argument("csv")

    # campaigns
    g = group("campaigns", "marketing campaigns to a contact book")
    s = cmd(g, "list", campaigns_list)
    s.add_argument("--page")
    s.add_argument("--status", help="DRAFT, SCHEDULED, RUNNING, PAUSED, SENT…")
    s.add_argument("--search")
    cmd(g, "get", campaigns_get, "details + stats").add_argument("id")
    s = cmd(g, "create", campaigns_create, "create a campaign (draft unless --send-now/--schedule)")
    s.add_argument("--name", required=True)
    s.add_argument("--from", dest="sender", required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--book", required=True, help="contact book id")
    s.add_argument("--html")
    s.add_argument("--html-file")
    s.add_argument("--preview-text")
    s.add_argument("--send-now", action="store_true")
    s.add_argument("--schedule", help="ISO 8601 or natural language ('tomorrow 9am')")
    s.add_argument("--batch-size", type=int)
    recipients(s)
    s = cmd(g, "schedule", campaigns_schedule, "send a draft now, or at --at")
    s.add_argument("id")
    s.add_argument("--at", help="ISO 8601 or natural language; omit = now")
    s.add_argument("--batch-size", type=int)
    cmd(g, "pause", campaigns_pause).add_argument("id")
    cmd(g, "resume", campaigns_resume).add_argument("id")
    cmd(g, "delete", campaigns_delete).add_argument("id")

    # domains (read + verify only: creating or deleting a sending domain is left to the UI)
    g = group("domains", "sending domains (read-only + verify)")
    cmd(g, "list", domains_list)
    cmd(g, "get", domains_get).add_argument("id")
    cmd(g, "verify", domains_verify).add_argument("id")

    g = group("analytics", "sending analytics")
    s = cmd(g, "series", analytics_series, "sent/delivered/opened… per day")
    s.add_argument("--days", choices=["7", "30"])
    s.add_argument("--domain")
    s = cmd(g, "reputation", analytics_reputation, "bounce/complaint rates")
    s.add_argument("--domain")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.url:
        sys.exit("no base URL: pass --url or set USESEND_BASE_URL")
    key = resolve_key(args)
    if not key and not args.dry_run:
        sys.exit("no API key: pass --key, set USESEND_API_KEY, or write it to ~/.config/usesend/key")
    try:
        args.fn(Client(args.url, key, args.dry_run), args)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
