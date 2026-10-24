"""Audit log: one Postgres row per tool call, plus one JSON line on stdout.

Each call is two writes: `record()` inserts a `pending` row before anything
happens, `finish()` sets the outcome. A row stuck at `pending` means the process
died mid-call.
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

SCHEMA_RE = re.compile(r"[a-z_][a-z0-9_]*")
OUTCOMES = ("pending", "ok", "error", "refused", "preview")

DDL = """
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.audit_log (
  id              bigserial PRIMARY KEY,
  at              timestamptz NOT NULL DEFAULT now(),
  actor           text        NOT NULL,
  tool            text        NOT NULL,
  mutating        boolean     NOT NULL,
  args            jsonb       NOT NULL,
  outcome         text        NOT NULL DEFAULT 'pending'
                  CHECK (outcome IN ('pending','ok','error','refused','preview')),
  error           text,
  email_ids       text[],
  campaign_id     text,
  contact_book_id text,
  duration_ms     integer
);
CREATE INDEX IF NOT EXISTS audit_log_actor_at ON {schema}.audit_log (actor, at DESC);
CREATE INDEX IF NOT EXISTS audit_log_campaign ON {schema}.audit_log (campaign_id) WHERE campaign_id IS NOT NULL;
"""


def log_json(**fields: Any) -> None:
    """One JSON line on stdout."""
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
    print(json.dumps(line, default=str, ensure_ascii=False), file=sys.stdout, flush=True)


def validate_schema(schema: str) -> str:
    if not SCHEMA_RE.fullmatch(schema):
        raise ValueError(f"MCP_AUDIT_SCHEMA must match {SCHEMA_RE.pattern}, got {schema!r}")
    return schema


class AuditSink(Protocol):
    def record(self, actor: str, tool: str, mutating: bool, args: dict[str, Any]) -> int: ...

    def finish(
        self,
        audit_id: int,
        outcome: str,
        *,
        email_ids: list[str] | None = None,
        campaign_id: str | None = None,
        contact_book_id: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None: ...

    def ping(self) -> None: ...


class PostgresAudit:
    """Audit sink on a psycopg connection pool."""

    def __init__(self, url: str, schema: str = "mcp", *, min_size: int = 1, max_size: int = 4):
        self.schema = validate_schema(schema)
        self.pool = ConnectionPool(
            url, min_size=min_size, max_size=max_size, open=False, kwargs={"autocommit": True}, timeout=10
        )

    def open(self) -> None:
        self.pool.open(wait=True, timeout=30)
        self.bootstrap()

    def close(self) -> None:
        self.pool.close()

    def bootstrap(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(DDL.format(schema=self.schema))

    def ping(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("SELECT 1")

    def record(self, actor: str, tool: str, mutating: bool, args: dict[str, Any]) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"INSERT INTO {self.schema}.audit_log (actor, tool, mutating, args)"
                " VALUES (%s, %s, %s, %s) RETURNING id",
                (actor, tool, mutating, Jsonb(args)),
            ).fetchone()
        return row[0]

    def finish(
        self,
        audit_id: int,
        outcome: str,
        *,
        email_ids: list[str] | None = None,
        campaign_id: str | None = None,
        contact_book_id: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        with self.pool.connection() as conn:
            conn.execute(
                f"UPDATE {self.schema}.audit_log SET outcome = %s, email_ids = %s, campaign_id = %s,"
                " contact_book_id = %s, error = %s, duration_ms = %s WHERE id = %s",
                (outcome, email_ids, campaign_id, contact_book_id, error, duration_ms, audit_id),
            )
