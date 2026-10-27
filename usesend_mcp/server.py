"""MCP server for useSend: tools, token auth middleware, audit, /healthz.

Streamable HTTP, stateless, JSON responses, MCP at `/` and health at `/healthz`.
Run with `python -m usesend_mcp.server`; configuration comes from the environment
(see README).
"""

from __future__ import annotations

import contextvars
import functools
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from usesend_mcp.audit import AuditSink, PostgresAudit, log_json, validate_schema
from usesend_mcp.auth import TokenTable, load_tokens_or_exit
from usesend_mcp.client import (
    BULK_CONTACTS_MAX,
    UNSUB_RE,
    ApiError,
    Client,
    InvalidId,
    chunks,
    drop_none,
    parse_contacts_csv,
    seg,
)
from usesend_mcp.redact import redact
from usesend_mcp.scrub import capture_tool_error, init_sentry

MAX_EMAIL_RECIPIENTS = 50
MAX_IMPORT_ROWS = 5000
ACTOR_HEADER = "X-MCP-Actor"

current_actor: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_actor", default=None)

READ_ONLY = ToolAnnotations(read_only_hint=True)
ADDITIVE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    usesend_api_key: str
    usesend_base_url: str
    database_url: str
    audit_schema: str
    allowed_from_domains: tuple[str, ...]
    port: int
    host: str = "0.0.0.0"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Config:
        missing = [
            name
            for name in ("MCP_USESEND_API_KEY", "USESEND_BASE_URL", "MCP_DATABASE_URL", "MCP_ALLOWED_FROM_DOMAINS")
            if not environ.get(name, "").strip()
        ]
        if missing:
            raise ConfigError(f"missing required environment: {', '.join(missing)}")
        domains = parse_domains(environ["MCP_ALLOWED_FROM_DOMAINS"])
        if not domains:
            raise ConfigError("MCP_ALLOWED_FROM_DOMAINS has no domains")
        try:
            schema = validate_schema(environ.get("MCP_AUDIT_SCHEMA", "mcp"))
            port = int(environ.get("MCP_PORT", "8080"))
        except ValueError as e:
            raise ConfigError(str(e)) from None
        return cls(
            usesend_api_key=environ["MCP_USESEND_API_KEY"].strip(),
            usesend_base_url=environ["USESEND_BASE_URL"].strip(),
            database_url=environ["MCP_DATABASE_URL"].strip(),
            audit_schema=schema,
            allowed_from_domains=domains,
            port=port,
            host=environ.get("MCP_HOST", "0.0.0.0").strip() or "0.0.0.0",
        )


def parse_domains(raw: str) -> tuple[str, ...]:
    return tuple(d.strip().lower().rstrip(".") for d in raw.split(",") if d.strip())


def sender_domain(sender: str) -> str | None:
    """`News <news@example.com>` or `news@example.com` → `example.com`."""
    addr = sender.strip()
    if "<" in addr:
        addr = addr[addr.rindex("<") + 1 :].split(">", 1)[0]
    local, sep, domain = addr.strip().rpartition("@")
    if not sep or not local or not domain:
        return None
    return domain.lower().rstrip(".")


class Refused(Exception):
    """A safety rule refused the call before useSend was called."""


@dataclass
class CallInfo:
    """What a tool body reports back to the audit wrapper."""

    audit_id: int | None = None
    outcome: str = "ok"
    email_ids: list[str] | None = None
    campaign_id: str | None = None
    contact_book_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _first_id(result: Any, *keys: str) -> str | None:
    if isinstance(result, dict):
        for key in keys:
            if result.get(key):
                return str(result[key])
    return None


class UseSendTools:
    """The tool bodies, plus the audit wrapper every call goes through."""

    def __init__(
        self,
        client: Client,
        audit: AuditSink,
        allowed_from_domains: tuple[str, ...],
        on_unexpected_error: Callable[[BaseException, str, str], None] | None = None,
    ):
        self.client = client
        self.audit = audit
        self.allowed_from_domains = allowed_from_domains
        self.on_unexpected_error = on_unexpected_error

    # ─── audit wrapper ─────────────────────────────────────────────────────────

    def call(self, tool: str, mutating: bool, args: dict[str, Any], body: Callable[[CallInfo], Any]) -> str:
        actor = current_actor.get() or "unknown"
        redacted = redact(args)
        started = time.monotonic()
        info = CallInfo()
        try:
            info.audit_id = self.audit.record(actor, tool, mutating, redacted)
        except Exception as e:
            log_json(event="audit_error", actor=actor, tool=tool, error=type(e).__name__)
            if mutating:
                log_json(
                    event="tool_call",
                    actor=actor,
                    tool=tool,
                    mutating=mutating,
                    args=redacted,
                    outcome="refused",
                    error="audit log unavailable",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise ToolError("The audit log is unavailable, so this action was refused. Nothing was sent.") from None
        outcome, error = "error", None
        try:
            result = body(info)
            outcome = info.outcome
            # One JSON text block, whatever the shape (a list would otherwise become one block per item).
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)
        except Refused as e:
            outcome, error = "refused", str(e)
            raise ToolError(f"Refused: {e}") from None
        except InvalidId as e:
            outcome, error = "refused", str(e)
            raise ToolError(f"Refused: {e}") from None
        except ApiError as e:
            error = str(e)
            raise ToolError(f"useSend API error: {e}") from None
        except Exception as e:
            error = type(e).__name__
            if self.on_unexpected_error:
                self.on_unexpected_error(e, tool, actor)
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            ids = {
                "email_ids": info.email_ids,
                "campaign_id": info.campaign_id,
                "contact_book_id": info.contact_book_id,
            }
            if info.audit_id is not None:
                try:
                    self.audit.finish(info.audit_id, outcome, error=error, duration_ms=duration_ms, **ids)
                except Exception as e:
                    log_json(
                        event="audit_error", actor=actor, tool=tool, audit_id=info.audit_id, error=type(e).__name__
                    )
            log_json(
                event="tool_call",
                actor=actor,
                tool=tool,
                mutating=mutating,
                args=redacted,
                outcome=outcome,
                error=error,
                duration_ms=duration_ms,
                audit_id=info.audit_id,
                **ids,
            )

    # ─── safety checks ─────────────────────────────────────────────────────────

    def check_sender(self, sender: str) -> None:
        domain = sender_domain(sender)
        if domain is None:
            raise Refused(f"'from' is not an email address: {sender!r}")
        if domain not in self.allowed_from_domains:
            allowed = ", ".join(self.allowed_from_domains)
            raise Refused(f"sender domain {domain!r} is not allowed (allowed: {allowed})")

    # ─── read-only ─────────────────────────────────────────────────────────────

    def read(self, tool: str, args: dict[str, Any], fn: Callable[[], Any]) -> str:
        return self.call(tool, False, args, lambda info: fn())

    # ─── mutating ──────────────────────────────────────────────────────────────

    def create_contact_book(self, name: str, emoji: str | None) -> str:
        def body(info: CallInfo):
            r = self.client.post("/v1/contactBooks", drop_none({"name": name, "emoji": emoji}))
            info.contact_book_id = _first_id(r, "id", "contactBookId")
            return r

        return self.call("create_contact_book", True, {"name": name, "emoji": emoji}, body)

    def add_contact(self, book_id, email, first_name, last_name, properties) -> str:
        args = {
            "book_id": book_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "properties": properties,
        }

        def body(info: CallInfo):
            info.contact_book_id = book_id
            payload = drop_none(
                {"email": email, "firstName": first_name, "lastName": last_name, "properties": properties}
            )
            return self.client.post(f"/v1/contactBooks/{seg(book_id)}/contacts", payload)

        return self.call("add_contact", True, args, body)

    def import_contacts(self, book_id: str, csv_text: str) -> str:
        def body(info: CallInfo):
            info.contact_book_id = book_id
            rows = parse_contacts_csv(csv_text.lstrip("﻿").splitlines())
            if not rows:
                raise Refused("no rows with an email found (the CSV needs an 'email' column)")
            if len(rows) > MAX_IMPORT_ROWS:
                raise Refused(f"{len(rows)} rows; at most {MAX_IMPORT_ROWS} per call")
            results = [
                self.client.post(f"/v1/contactBooks/{seg(book_id)}/contacts/bulk", part)
                for part in chunks(rows, BULK_CONTACTS_MAX)
            ]
            return {"rows": len(rows), "batches": results}

        return self.call("import_contacts", True, {"book_id": book_id, "csv_text": csv_text}, body)

    def update_contact(self, book_id, contact_id, email, first_name, last_name, properties, subscribed) -> str:
        fields = {
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "properties": properties,
            "subscribed": subscribed,
        }
        args = {"book_id": book_id, "contact_id": contact_id, **fields}

        def body(info: CallInfo):
            info.contact_book_id = book_id
            payload = drop_none(fields)
            if not payload:
                raise Refused("nothing to update")
            return self.client.patch(f"/v1/contactBooks/{seg(book_id)}/contacts/{seg(contact_id)}", payload)

        return self.call("update_contact", True, args, body)

    def send_email(self, sender, to, subject, html, text, cc, bcc, reply_to, scheduled_at, confirm) -> str:
        args = {
            "from": sender,
            "to": to,
            "subject": subject,
            "html": html,
            "text": text,
            "cc": cc,
            "bcc": bcc,
            "reply_to": reply_to,
            "scheduled_at": scheduled_at,
            "confirm": confirm,
        }

        def body(info: CallInfo):
            self.check_sender(sender)
            if not (html or text):
                raise Refused("need html or text")
            if not to:
                raise Refused("need at least one 'to' recipient")
            total = len(to) + len(cc or []) + len(bcc or [])
            if total > MAX_EMAIL_RECIPIENTS:
                raise Refused(f"{total} recipients; at most {MAX_EMAIL_RECIPIENTS} in total (to + cc + bcc)")
            if not confirm:
                info.outcome = "preview"
                return {
                    "preview": True,
                    "from": sender,
                    "subject": subject,
                    "to": to,
                    "cc": cc or [],
                    "bcc": bcc or [],
                    "recipients": total,
                    "when": scheduled_at or "now",
                    "note": "Nothing was sent. Call again with confirm=true to send.",
                }
            actor = current_actor.get() or "unknown"
            payload = drop_none(
                {
                    "from": sender,
                    "to": to,
                    "subject": subject,
                    "html": html,
                    "text": text,
                    "cc": cc,
                    "bcc": bcc,
                    "replyTo": reply_to,
                    "scheduledAt": scheduled_at,
                    "headers": {ACTOR_HEADER: actor},
                }
            )
            headers = {"Idempotency-Key": f"mcp-audit-{info.audit_id}"}
            r = self.client.post("/v1/emails", payload, headers=headers)
            email_id = _first_id(r, "emailId", "id")
            info.email_ids = [email_id] if email_id else None
            return r

        return self.call("send_email", True, args, body)

    def cancel_email(self, email_id: str) -> str:
        def body(info: CallInfo):
            info.email_ids = [email_id]
            return self.client.post(f"/v1/emails/{seg(email_id)}/cancel")

        return self.call("cancel_email", True, {"id": email_id}, body)

    def create_campaign(self, name, sender, subject, book_id, html, preview_text, reply_to) -> str:
        args = {
            "name": name,
            "from": sender,
            "subject": subject,
            "book_id": book_id,
            "html": html,
            "preview_text": preview_text,
            "reply_to": reply_to,
        }

        def body(info: CallInfo):
            info.contact_book_id = book_id
            self.check_sender(sender)
            if not UNSUB_RE.search(html or ""):
                raise Refused(
                    "the HTML has no {{usesend_unsubscribe_url}} link; useSend refuses to send a campaign without it"
                )
            payload = drop_none(
                {
                    "name": name,
                    "from": sender,
                    "subject": subject,
                    "previewText": preview_text,
                    "contactBookId": book_id,
                    "html": html,
                    "replyTo": reply_to,
                }
            )
            r = self.client.post("/v1/campaigns", payload)
            info.campaign_id = _first_id(r, "id", "campaignId")
            return r

        return self.call("create_campaign", True, args, body)

    def _campaign_preview(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.client.get(f"/v1/campaigns/{seg(campaign_id)}") or {}
        preview: dict[str, Any] = {
            "campaign": {k: campaign.get(k) for k in ("id", "name", "from", "subject", "status") if k in campaign}
        }
        book_id = campaign.get("contactBookId")
        if book_id:
            book = self.client.get(f"/v1/contactBooks/{seg(book_id)}") or {}
            preview["contact_book"] = {
                "id": book_id,
                "name": book.get("name"),
                # All contacts, including unsubscribed ones: an upper bound on recipients.
                "contacts": (book.get("_count") or {}).get("contacts"),
            }
        return preview

    def schedule_campaign(self, campaign_id: str, at: str | None, batch_size: int | None, confirm: bool) -> str:
        args = {"id": campaign_id, "at": at, "batch_size": batch_size, "confirm": confirm}

        def body(info: CallInfo):
            info.campaign_id = campaign_id
            if not confirm:
                info.outcome = "preview"
                preview = self._campaign_preview(campaign_id)
                info.contact_book_id = (preview.get("contact_book") or {}).get("id")
                return {
                    "preview": True,
                    **preview,
                    "when": at or "now",
                    "batch_size": batch_size,
                    "note": "Nothing was scheduled. Call again with confirm=true to schedule. "
                    "The contact count includes unsubscribed contacts, so it is an upper bound.",
                }
            payload = drop_none({"scheduledAt": at, "batchSize": batch_size})
            return self.client.post(f"/v1/campaigns/{seg(campaign_id)}/schedule", payload)

        return self.call("schedule_campaign", True, args, body)

    def pause_campaign(self, campaign_id: str) -> str:
        def body(info: CallInfo):
            info.campaign_id = campaign_id
            return self.client.post(f"/v1/campaigns/{seg(campaign_id)}/pause")

        return self.call("pause_campaign", True, {"id": campaign_id}, body)

    def resume_campaign(self, campaign_id: str, confirm: bool) -> str:
        def body(info: CallInfo):
            info.campaign_id = campaign_id
            if not confirm:
                info.outcome = "preview"
                preview = self._campaign_preview(campaign_id)
                info.contact_book_id = (preview.get("contact_book") or {}).get("id")
                return {
                    "preview": True,
                    **preview,
                    "note": "Nothing was resumed. Call again with confirm=true to resume sending.",
                }
            return self.client.post(f"/v1/campaigns/{seg(campaign_id)}/resume")

        return self.call("resume_campaign", True, {"id": campaign_id, "confirm": confirm}, body)


INSTRUCTIONS = """\
Drive a useSend instance (email, contact books, campaigns). Every call is recorded in an
audit log against the person whose token is used.

Safety rules:
- create_campaign only creates drafts. Review them in the useSend UI, then schedule_campaign.
- send_email, schedule_campaign and resume_campaign return a preview unless confirm=true.
  Show the preview to the user and only confirm when they say so.
- Campaign HTML must contain {{usesend_unsubscribe_url}}.
- 'from' must be on an allowed sender domain.
- pause_campaign is always allowed: it is the emergency stop.
"""


def from_alias(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Expose a `sender` parameter as `from` on the wire.

    `from` is a Python keyword, so the function names it `sender` with
    `Field(alias="from")`. The SDK publishes the alias in the schema and passes the
    argument back under the alias; this maps it back. `functools.wraps` keeps the
    original signature (via `__wrapped__`) for schema generation.
    """

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> Any:
        if "from" in kwargs:
            kwargs["sender"] = kwargs.pop("from")
        return fn(**kwargs)

    return wrapper


def build_mcp(tools: UseSendTools) -> MCPServer:
    """Register the v1 tool set on a fresh MCPServer."""
    mcp = MCPServer("usesend", instructions=INSTRUCTIONS)
    c = tools.client
    domains = ", ".join(tools.allowed_from_domains)

    # ─── read-only (audit fails open) ──────────────────────────────────────────

    @mcp.tool(annotations=READ_ONLY)
    def list_domains() -> Any:
        """List the sending domains and their verification status."""
        return tools.read("list_domains", {}, lambda: c.get("/v1/domains"))

    @mcp.tool(annotations=READ_ONLY)
    def list_contact_books() -> Any:
        """List contact books (the audiences campaigns are sent to)."""
        return tools.read("list_contact_books", {}, lambda: c.get("/v1/contactBooks"))

    @mcp.tool(annotations=READ_ONLY)
    def get_contact_book(id: str) -> Any:
        """Get one contact book, including its contact count."""
        return tools.read("get_contact_book", {"id": id}, lambda: c.get(f"/v1/contactBooks/{seg(id)}"))

    @mcp.tool(annotations=READ_ONLY)
    def list_contacts(
        book_id: str, emails: list[str] | None = None, page: int | None = None, limit: int | None = None
    ) -> Any:
        """List contacts in a book, optionally filtered by email addresses."""
        args = {"book_id": book_id, "emails": emails, "page": page, "limit": limit}
        return tools.read(
            "list_contacts",
            args,
            lambda: c.get(
                f"/v1/contactBooks/{seg(book_id)}/contacts",
                emails=",".join(emails) if emails else None,
                page=page,
                limit=limit,
            ),
        )

    @mcp.tool(annotations=READ_ONLY)
    def list_campaigns(status: str | None = None, search: str | None = None, page: int | None = None) -> Any:
        """List campaigns. status: DRAFT, SCHEDULED, RUNNING, PAUSED, SENT…"""
        args = {"status": status, "search": search, "page": page}
        return tools.read(
            "list_campaigns", args, lambda: c.get("/v1/campaigns", status=status, search=search, page=page)
        )

    @mcp.tool(annotations=READ_ONLY)
    def get_campaign(id: str) -> Any:
        """Get one campaign with its stats (sent, delivered, opened, clicked, bounced…)."""
        return tools.read("get_campaign", {"id": id}, lambda: c.get(f"/v1/campaigns/{seg(id)}"))

    @mcp.tool(annotations=READ_ONLY)
    def list_emails(
        start: str | None = None, end: str | None = None, page: int | None = None, limit: int | None = None
    ) -> Any:
        """List sent emails. start/end are ISO datetimes."""
        args = {"start": start, "end": end, "page": page, "limit": limit}
        return tools.read(
            "list_emails",
            args,
            lambda: c.get("/v1/emails", startDate=start, endDate=end, page=page, limit=limit),
        )

    @mcp.tool(annotations=READ_ONLY)
    def get_email(id: str) -> Any:
        """Get one email and its delivery status."""
        return tools.read("get_email", {"id": id}, lambda: c.get(f"/v1/emails/{seg(id)}"))

    @mcp.tool(annotations=READ_ONLY)
    def get_analytics(days: Literal[7, 30] = 7) -> Any:
        """Sending analytics: a per-day time series and reputation metrics (bounce/complaint rates)."""

        def fetch():
            return {
                "time_series": c.get("/v1/analytics/email-time-series", days=days),
                "reputation": c.get("/v1/analytics/reputation-metrics"),
            }

        return tools.read("get_analytics", {"days": days}, fetch)

    # ─── mutating (audit fails closed) ─────────────────────────────────────────

    @mcp.tool(annotations=ADDITIVE)
    def create_contact_book(name: str, emoji: str | None = None) -> Any:
        """Create a contact book."""
        return tools.create_contact_book(name, emoji)

    @mcp.tool(annotations=ADDITIVE)
    def add_contact(
        book_id: str,
        email: str,
        first_name: str | None = None,
        last_name: str | None = None,
        properties: dict[str, str] | None = None,
    ) -> Any:
        """Add one contact to a book. Properties fill {{variables}} in campaigns."""
        return tools.add_contact(book_id, email, first_name, last_name, properties)

    @mcp.tool(annotations=ADDITIVE)
    def import_contacts(book_id: str, csv_text: str) -> Any:
        """Import contacts into a book from CSV text (at most 5000 rows per call).

        Columns: `email` (required), firstName/lastName (or first_name/last_name), subscribed;
        any other column becomes a contact property.
        """
        return tools.import_contacts(book_id, csv_text)

    @mcp.tool(annotations=ADDITIVE)
    def update_contact(
        book_id: str,
        contact_id: str,
        email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        properties: dict[str, str] | None = None,
        subscribed: bool | None = None,
    ) -> Any:
        """Update a contact. subscribed=false unsubscribes it."""
        return tools.update_contact(book_id, contact_id, email, first_name, last_name, properties, subscribed)

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Send one email (at most 50 recipients across to, cc and bcc). Without confirm=true this only "
            "returns a preview and sends nothing; show it to the user and confirm only when they agree. "
            f"'from' must be on an allowed sender domain ({domains}). scheduled_at is an ISO datetime."
        ),
    )
    @from_alias
    def send_email(
        sender: Annotated[str, Field(alias="from", description="'Name <addr@domain>' or 'addr@domain'")],
        to: list[str],
        subject: str,
        html: str | None = None,
        text: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: list[str] | None = None,
        scheduled_at: str | None = None,
        confirm: bool = False,
    ) -> Any:
        return tools.send_email(sender, to, subject, html, text, cc, bcc, reply_to, scheduled_at, confirm)

    @mcp.tool(annotations=ADDITIVE)
    def cancel_email(id: str) -> Any:
        """Cancel a scheduled email."""
        return tools.cancel_email(id)

    @mcp.tool(
        annotations=ADDITIVE,
        description=(
            "Create a campaign as a DRAFT for a contact book. It is never sent or scheduled by this tool: "
            "review it in the useSend UI, then use schedule_campaign. The HTML must contain "
            "{{usesend_unsubscribe_url}}. "
            f"'from' must be on an allowed sender domain ({domains})."
        ),
    )
    @from_alias
    def create_campaign(
        name: str,
        sender: Annotated[str, Field(alias="from", description="'Name <addr@domain>' or 'addr@domain'")],
        subject: str,
        book_id: str,
        html: str,
        preview_text: str | None = None,
        reply_to: list[str] | None = None,
    ) -> Any:
        return tools.create_campaign(name, sender, subject, book_id, html, preview_text, reply_to)

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Schedule a draft campaign to send at `at` (ISO or natural language, e.g. 'tomorrow 9am'; "
            "omit to send now). Without confirm=true this only returns a preview (campaign, contact book, "
            "contact count, when) and schedules nothing; confirm only when the user agrees."
        ),
    )
    def schedule_campaign(id: str, at: str | None = None, batch_size: int | None = None, confirm: bool = False) -> Any:
        return tools.schedule_campaign(id, at, batch_size, confirm)

    @mcp.tool(annotations=ADDITIVE)
    def pause_campaign(id: str) -> Any:
        """Pause a running campaign. Always allowed: it is the emergency stop."""
        return tools.pause_campaign(id)

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Resume sending a paused campaign. Without confirm=true this only returns a preview and resumes "
            "nothing; confirm only when the user agrees."
        ),
    )
    def resume_campaign(id: str, confirm: bool = False) -> Any:
        return tools.resume_campaign(id, confirm)

    return mcp


# ─── HTTP: auth middleware, /healthz, app ──────────────────────────────────────


class AuthMiddleware:
    """Bearer token → actor. `/healthz` is open; everything else needs a known token.

    The Authorization header is stripped before the request reaches the MCP app, so
    no token can reach tools, the audit log or error reports.
    """

    def __init__(self, app, tokens: TokenTable):
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            # Nothing but HTTP is served: refuse websockets (or anything else) outright.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return None
        path = scope.get("path", "")
        status = {"code": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        actor = None
        try:
            if path == "/healthz":
                return await self.app(scope, receive, send_wrapper)
            authorization = None
            for name, value in scope.get("headers", []):
                if name == b"authorization":
                    authorization = value.decode("latin-1")
            token = None
            if authorization and authorization[:7].lower() == "bearer ":
                token = authorization[7:].strip()
            actor = self.tokens.lookup(token)
            if actor is None:
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                return await response(scope, receive, send_wrapper)
            scope = dict(scope, headers=[(k, v) for k, v in scope.get("headers", []) if k != b"authorization"])
            reset = current_actor.set(actor)
            try:
                return await self.app(scope, receive, send_wrapper)
            finally:
                current_actor.reset(reset)
        finally:
            log_json(event="request", actor=actor, method=scope.get("method"), path=path, status=status["code"])


def create_app(tokens: TokenTable, tools: UseSendTools):
    """The ASGI app: MCP at `/` (stateless, JSON), `/healthz`, behind the auth middleware."""
    mcp = build_mcp(tools)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        try:
            await anyio.to_thread.run_sync(tools.audit.ping)
        except Exception as e:
            log_json(event="healthz", ok=False, error=type(e).__name__)
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse({"ok": True})

    app = mcp.streamable_http_app(
        # Explicit: the default is /mcp, which behind a proxy stripping /mcp would land at /mcp/mcp/.
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        # Requests arrive with the public Host header: no localhost-only DNS-rebinding allow-list.
        host="0.0.0.0",
        transport_security=None,
    )
    return AuthMiddleware(app, tokens)


def main() -> None:
    tokens = load_tokens_or_exit()
    try:
        config = Config.from_env()
    except ConfigError as e:
        print(f"usesend-mcp: refusing to start: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    init_sentry(os.environ, secrets=[*tokens.secret_values(), config.usesend_api_key])
    audit = PostgresAudit(config.database_url, config.audit_schema)
    try:
        audit.open()
    except Exception as e:
        print(f"usesend-mcp: refusing to start: audit database unavailable ({type(e).__name__})", file=sys.stderr)
        sys.exit(1)
    client = Client(config.usesend_base_url, config.usesend_api_key, user_agent="usesend-mcp-server/1")
    tools = UseSendTools(client, audit, config.allowed_from_domains, on_unexpected_error=capture_tool_error)
    app = create_app(tokens, tools)
    log_json(event="startup", actors=len(tokens.names), port=config.port, audit_schema=config.audit_schema)
    try:
        uvicorn.run(app, host=config.host, port=config.port, access_log=False, log_level="warning")
    finally:
        audit.close()


if __name__ == "__main__":
    main()

