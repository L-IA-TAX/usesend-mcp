"""Optional Sentry error reporting, with a scrubber so nothing sensitive leaves the process.

Enabled only when MCP_SENTRY_DSN is set to something other than `none`. The
scrubber removes tool arguments (bodies, recipients, CSV content), tokens, API keys
and request headers. It keeps what helps debugging: the tool name, the actor, the
exception type and the stack trace (function names and line numbers). Free text that
could echo an argument (exception messages, breadcrumb messages, interpolated log
messages) is dropped rather than pattern-scrubbed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration

FILTERED = "[Filtered]"

# Keys whose values never leave the process, wherever they appear.
SENSITIVE_KEYS = frozenset(
    k.lower()
    for k in (
        "html",
        "text",
        "csv_text",
        "csvText",
        "to",
        "cc",
        "bcc",
        "from",
        "sender",
        "reply_to",
        "replyTo",
        "subject",
        "preview_text",
        "previewText",
        "email",
        "emails",
        "first_name",
        "last_name",
        "firstName",
        "lastName",
        "properties",
        "attachments",
        "args",
        "arguments",
        "params",
        "body",
        "data",
        "headers",
        "cookies",
        "authorization",
        "token",
        "tokens",
        "api_key",
        "apikey",
        "key",
        "password",
        "secret",
        "dsn",
        "env",
        "vars",
        "query_string",
    )
)

# Keys kept verbatim: identifiers and the stack trace shape.
KEEP_KEYS = frozenset(
    (
        "tool",
        "actor",
        "type",
        "module",
        "function",
        "filename",
        "abs_path",
        "lineno",
        "colno",
        "in_app",
        "trace_id",
        "span_id",
        "parent_span_id",
        "op",
        "status",
        "mechanism",
    )
)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")
USESEND_KEY_RE = re.compile(r"\bus_[A-Za-z0-9_-]+")
LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")


class Scrubber:
    def __init__(self, secrets: Iterable[str] = ()):
        # Longest first, so a secret containing another is replaced whole.
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, FILTERED)
        value = BEARER_RE.sub("Bearer " + FILTERED, value)
        value = USESEND_KEY_RE.sub(FILTERED, value)
        value = EMAIL_RE.sub("[email]", value)
        return LONG_TOKEN_RE.sub(FILTERED, value)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            out = {}
            for k, v in value.items():
                key = str(k).lower()
                if key in KEEP_KEYS and isinstance(v, str | int | float | bool | None):
                    out[k] = v
                elif key in SENSITIVE_KEYS:
                    out[k] = FILTERED
                else:
                    out[k] = self.value(v)
            return out
        if isinstance(value, list | tuple):
            return [self.value(v) for v in value]
        return value

    def frames(self, stacktrace: dict | None) -> None:
        for frame in (stacktrace or {}).get("frames") or []:
            frame.pop("vars", None)

    def event(self, event: dict, hint: dict | None = None) -> dict:
        request = event.get("request")
        if request:
            url = request.get("url")
            event["request"] = {"method": request.get("method"), "url": url.split("?", 1)[0] if url else None}
        event.pop("user", None)
        for section in ("extra", "contexts", "tags"):
            if section in event:
                event[section] = self.value(event[section])
        if "breadcrumbs" in event:
            crumbs = event["breadcrumbs"]
            values = crumbs.get("values", []) if isinstance(crumbs, dict) else crumbs
            scrubbed = [self.breadcrumb(c) for c in values]
            event["breadcrumbs"] = {"values": scrubbed} if isinstance(crumbs, dict) else scrubbed
        if isinstance(event.get("message"), str):
            event["message"] = self.text(event["message"])
        if isinstance(event.get("logentry"), dict):
            # Keep the format template (written in code); drop the interpolated arguments.
            template = event["logentry"].get("message")
            event["logentry"] = {"message": self.text(template) if isinstance(template, str) else None}
        for exc in (event.get("exception") or {}).get("values") or []:
            # Exception messages can echo inputs (e.g. validation errors quoting a body): keep only the type.
            if "value" in exc:
                exc["value"] = FILTERED
            self.frames(exc.get("stacktrace"))
        for thread in (event.get("threads") or {}).get("values") or []:
            self.frames(thread.get("stacktrace"))
        return event

    def breadcrumb(self, crumb: dict, hint: dict | None = None) -> dict:
        # Only the shape of what happened: messages and data can carry arguments.
        return {k: crumb[k] for k in ("type", "category", "level", "timestamp") if k in crumb}


def sentry_enabled(environ: Mapping[str, str]) -> bool:
    dsn = environ.get("MCP_SENTRY_DSN", "").strip()
    return bool(dsn) and dsn.lower() != "none"


def init_sentry(environ: Mapping[str, str], secrets: Iterable[str] = ()) -> bool:
    """Initialise Sentry if MCP_SENTRY_DSN is set (and not `none`). Returns whether it did."""
    if not sentry_enabled(environ):
        return False
    scrubber = Scrubber(secrets)
    sentry_sdk.init(
        dsn=environ["MCP_SENTRY_DSN"].strip(),
        environment=environ.get("MCP_SENTRY_ENVIRONMENT") or None,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=scrubber.event,
        before_breadcrumb=scrubber.breadcrumb,
        # Log records become breadcrumbs only; tool errors are captured explicitly with tags.
        integrations=[LoggingIntegration(level=logging.INFO, event_level=None)],
    )
    return True


def capture_tool_error(exc: BaseException, tool: str, actor: str) -> None:
    """Report an unexpected tool exception, tagged with the tool and actor. A no-op when disabled."""
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("tool", tool)
        scope.set_tag("actor", actor)
        sentry_sdk.capture_exception(exc)
