# 001 — useSend MCP server: tools, token auth, audit log, image

Targets the [useSend](https://github.com/usesend/useSend) public API as of **v1.9.8**. Deployment
is specified elsewhere; this spec covers only what this repository builds.

## Context

useSend is a self-hostable email platform (on top of AWS SES). Its public API (`/api/v1/*`, Bearer
API key) covers:

- sending email;
- contact books and contacts;
- campaigns: create, schedule, pause and resume, with per-campaign stats;
- domains and analytics.

We want people to drive that from an MCP client (Claude Code, Claude Desktop, claude.ai, …) by
asking things like "create a campaign for book X". They shouldn't each have to run a local tool,
and **every action must be tied to a person**.

**useSend API keys can't be tied to a person** (checked at v1.9.8, schema + routers):

- `ApiKey` has `name`, `teamId`, an optional `domainId` and `lastUsed`. It has **no user or creator
  column**, and any team member can create one.
- The `permission` field (`FULL` / `SENDING`) exists but the public API **never enforces it**.
  Every key can do everything in the team.
- `Email.apiId` records which key sent a single email. `Campaign` records neither key nor user.
- Upstream has no issue or PR for per-user keys or an audit log (checked 2026-10-08).

So per-user keys give separate revocation but no attribution. This server puts identity in front
of useSend instead: it knows who is calling and records it itself.

A working standard-library CLI for the same API already exists (all v1.9.8 endpoints, 429
back-off, confirm-before-send) with a Claude skill that wraps it. Both are imported into this repo
as the starting point.

## Decision

1. **One repo, three entry points over one client:**
   - `usesend_mcp/client.py`: the HTTP client;
   - `usesend_mcp/cli.py`: the CLI;
   - `usesend_mcp/server.py`: the MCP server.
   The Claude skill lives in `.claude/skills/usesend/` and calls the CLI.
2. **MCP transport: streamable HTTP, stateless** (`stateless_http=True`, JSON responses). There is
   no session affinity to keep. The server answers MCP at `/` and health at `/healthz`. A reverse
   proxy may mount it under a path prefix, provided it strips the prefix.
3. **Auth: a static token list (v1).** `MCP_TOKENS` holds comma-separated `name:token` pairs (e.g.
   `alice:<64 hex>,bob:<64 hex>`).
   - The deployment supplies it as a secret. It is **never in source or in the image**: both are
     public.
   - Clients send `Authorization: Bearer <token>`. The middleware matches it with
     `hmac.compare_digest` against every entry and sets the **actor** (the name) for the request.
   - Unknown or missing token → 401. Adding or revoking someone = change `MCP_TOKENS` and restart.
   - Identity from an OAuth provider in front of the server is the planned successor (Known Gaps).
4. **One service API key.** The server calls useSend with a single key (`MCP_USESEND_API_KEY`), at
   `USESEND_BASE_URL`. Users never see a useSend key.
5. **Audit: a Postgres table, written for every tool call.** It lives in schema `MCP_AUDIT_SCHEMA`
   (default `mcp`), reached at `MCP_DATABASE_URL`.
   - **Recommended placement:** useSend's own database, with a dedicated role that has only
     `CONNECT` + `CREATE` on it. useSend's `prisma migrate deploy` only manages `public`, so the
     schema stays ours, and audit rows join directly to useSend's `"Email"` / `"Campaign"` tables.
     Any Postgres works.
   - **Mutating tools fail closed.** An audit row is inserted *before* the useSend call. If that
     insert fails, the tool refuses and calls nothing.
   - **Read-only tools fail open.** They log the audit error and still answer.
   - Every call also emits one JSON log line to stdout with the same fields.
   - Sent emails carry an `X-MCP-Actor: <actor>` header. Campaigns have no header field in the
     API, so the audit row is their attribution.
6. **Safety: drafts and explicit confirmation.**
   - `create_campaign` only makes drafts: no `sendNow`, no `scheduledAt`.
   - `send_email`, `schedule_campaign` and `resume_campaign` take `confirm: bool = false`. With
     `false` they return a **preview** (from, subject, recipients or book and its contact count,
     when) and call nothing; the audit outcome is `preview`. Only `confirm: true` acts.
   - These tools carry the MCP annotation `destructiveHint: true`, so clients prompt before running them.
   - **Sender allow-list.** `from` must be on a domain in `MCP_ALLOWED_FROM_DOMAINS` (required),
     otherwise the tool refuses before calling the API.
   - **v1 has no delete tools** (books, contacts, campaigns, domains) and no domain create or verify.
7. **Image: GHCR, tag-pinned.**
   - GitHub Actions builds `ghcr.io/<owner>/usesend-mcp:vX.Y.Z` on a `vX.Y.Z` git tag.
   - Never `:latest`; deployments pin the exact tag.
   - Base image `python:3.12-slim`, non-root user, no build tools left in the final image.
8. **Nothing deployment-specific is hard-coded.** No default hostnames or domains, in the server
   or the CLI: URLs, domains and names all come from configuration.

## Implementation

### Layout

```
usesend_mcp/
  __init__.py
  client.py        # Client + ApiError, 429 back-off, no argparse
  cli.py           # argparse CLI; `python -m usesend_mcp.cli`
  server.py        # FastMCP app, tools, auth middleware, /healthz
  auth.py          # MCP_TOKENS parsing + constant-time lookup
  audit.py         # psycopg pool, DDL bootstrap, record()/finish()
  redact.py        # args → audit-safe JSON
.claude/skills/usesend/SKILL.md
pyproject.toml     # deps: mcp, psycopg[binary,pool], uvicorn; extra `dev`: pytest, ruff
Dockerfile
.github/workflows/image.yml
tests/
specs/
```

The CLI must keep working with **only the standard library**, so operators can run it with just
`python3`. Only `server.py` / `audit.py` import third-party packages.

When importing the existing CLI and skill, remove every deployment-specific default: base URL,
sender addresses, domain names, key file locations tied to an org.
- The CLI reads `USESEND_BASE_URL` (or `--url`) and has **no default**; it errors if unset.
- The key comes from `--key`, `$USESEND_API_KEY`, `--key-file` or `~/.config/usesend/key`.
- The skill's examples use `example.com` placeholders.

### Configuration (environment)

| Var | Meaning |
|---|---|
| `MCP_TOKENS` | `name:token[,name:token…]`. Names match `[a-z0-9._-]+`, tokens are ≥ 32 chars. Startup fails on a malformed list or a duplicate name/token. |
| `MCP_USESEND_API_KEY` | The service useSend key (`us_…`). |
| `USESEND_BASE_URL` | useSend base URL **without** `/api` (e.g. `http://usesend:3000` on a shared Docker network). The client appends `/api`. |
| `MCP_DATABASE_URL` | Postgres URL for the audit role. |
| `MCP_AUDIT_SCHEMA` | Audit schema name, default `mcp`. |
| `MCP_ALLOWED_FROM_DOMAINS` | Comma-separated sender domains, required (e.g. `example.com,mail.example.com`). |
| `MCP_PORT` | Listen port, default `8080`. |

Server-specific names are `MCP_`-prefixed, so they can share an env file with useSend's
containers without colliding. `USESEND_BASE_URL` is the exception: it is the name useSend's
images already use.

### Tools (v1)

Read-only (audit fails open):

| Tool | API |
|---|---|
| `list_domains` | `GET /v1/domains` |
| `list_contact_books`, `get_contact_book(id)` | `GET /v1/contactBooks[/{id}]` |
| `list_contacts(book_id, emails?, page?, limit?)` | `GET /v1/contactBooks/{id}/contacts` |
| `list_campaigns(status?, search?, page?)`, `get_campaign(id)` | `GET /v1/campaigns[/{id}]` (stats come with `get`) |
| `list_emails(start?, end?, page?, limit?)`, `get_email(id)` | `GET /v1/emails[/{id}]` |
| `get_analytics(days=7\|30)` | `GET /v1/analytics/email-time-series` + `reputation-metrics` |

Mutating (audit fails closed):

| Tool | API | Notes |
|---|---|---|
| `create_contact_book(name, emoji?)` | `POST /v1/contactBooks` | |
| `add_contact(book_id, email, first_name?, last_name?, properties?)` | `POST …/contacts` | |
| `import_contacts(book_id, csv_text)` | `POST …/contacts/bulk` | Same CSV rules as the CLI (`email` column, `firstName`/`lastName`, `subscribed`, the rest → properties). Chunked by 1000, max 5000 rows per call. |
| `update_contact(book_id, contact_id, …, subscribed?)` | `PATCH …/contacts/{id}` | `subscribed=false` = unsubscribe |
| `send_email(from, to[], subject, html?, text?, cc?, bcc?, reply_to?, scheduled_at?, confirm=false)` | `POST /v1/emails` | ≤ 50 recipients in total. Adds the `X-MCP-Actor` header. `Idempotency-Key: mcp-audit-<row id>`. |
| `cancel_email(id)` | `POST /v1/emails/{id}/cancel` | |
| `create_campaign(name, from, subject, book_id, html, preview_text?, reply_to?)` | `POST /v1/campaigns` | Draft only. Refuses HTML without `{{usesend_unsubscribe_url}}` (or the legacy `{{unsend_unsubscribe_url}}`). |
| `schedule_campaign(id, at?, batch_size?, confirm=false)` | `POST /v1/campaigns/{id}/schedule` | Preview shows the book name and `_count.contacts` from `GET /v1/contactBooks/{id}` (all contacts, including unsubscribed, so it is an upper bound). `at` accepts ISO or natural language (useSend parses it). |
| `pause_campaign(id)` | `POST …/pause` | No confirm: stopping is always safe. |
| `resume_campaign(id, confirm=false)` | `POST …/resume` | |

Tool descriptions state the safety rules (drafts, confirm, the unsubscribe link, allowed sender
domains), so the model sees them.

### Audit table

`audit.py` runs this idempotent DDL at startup (`<schema>` = `MCP_AUDIT_SCHEMA`, validated as
`[a-z_][a-z0-9_]*`):

```sql
CREATE SCHEMA IF NOT EXISTS <schema>;
CREATE TABLE IF NOT EXISTS <schema>.audit_log (
  id              bigserial PRIMARY KEY,
  at              timestamptz NOT NULL DEFAULT now(),
  actor           text        NOT NULL,
  tool            text        NOT NULL,
  mutating        boolean     NOT NULL,
  args            jsonb       NOT NULL,           -- redacted, see below
  outcome         text        NOT NULL DEFAULT 'pending'
                  CHECK (outcome IN ('pending','ok','error','refused','preview')),
  error           text,
  email_ids       text[],
  campaign_id     text,
  contact_book_id text,
  duration_ms     integer
);
CREATE INDEX IF NOT EXISTS audit_log_actor_at ON <schema>.audit_log (actor, at DESC);
CREATE INDEX IF NOT EXISTS audit_log_campaign ON <schema>.audit_log (campaign_id) WHERE campaign_id IS NOT NULL;
```

Each call goes through two writes:

1. `record(actor, tool, mutating, args)` → `INSERT … RETURNING id`, status `pending`.
2. `finish(id, outcome, ids…, error, duration)` → `UPDATE`.

A row stuck at `pending` means the process died mid-call. That is visible, not silent.

**Redaction (`redact.py`):**
- `html` / `text` / `csv_text` → `{"sha256": …, "bytes": n}`. For `csv_text`, also `"rows": n`.
- Attachments → `[{filename, bytes}]`.
- Recipients, subject, from, names and ids are kept: they are what the audit is for.
- No token or API key ever reaches `args`. The middleware strips auth before tools run.

### Auth middleware

A Starlette middleware in front of the MCP app:
- `/healthz` is unauthenticated and returns `{"ok": true}` after a DB `SELECT 1`. Health depends on
  the audit DB, because mutating tools are useless without it.
- Everything else needs `Authorization: Bearer <token>` → actor goes into a `contextvars.ContextVar`,
  which tools read. Otherwise → 401 with JSON `{"error": "unauthorized"}`. No detail about which
  part failed.
- One JSON log line per request: actor, path and status. Never the header.

### Image and CI

- `Dockerfile`: a multi-stage build. `pip install .` into a venv in the builder stage, then copy it
  to a `python:3.12-slim` runtime. `USER 10001`, `EXPOSE 8080`,
  `CMD ["python", "-m", "usesend_mcp.server"]`. The runtime keeps `python` on `PATH`, so a
  `python -c` health probe works.
- `.github/workflows/image.yml`:
  - on push to `main` and on PRs: lint (`ruff`) + tests;
  - on a `v*` tag: build + push `ghcr.io/${{ github.repository_owner }}/usesend-mcp:<tag>` using
    `GITHUB_TOKEN` with `packages: write`.
- Set the GHCR package visibility to **public** once, by hand. New packages start private, and a
  public image can be pulled without credentials.

### Tests

`pytest`, no network:
- `client`: request shapes against a local `http.server` mock: send, bulk import chunking, 429
  retry, error body.
- `auth`: parsing (bad, duplicate, short), constant-time match, 401 paths.
- `redact`: bodies hashed, recipients kept.
- `server`: each mutating tool with `confirm=false` calls nothing; `create_campaign` refuses
  without the unsubscribe placeholder; a sender outside `MCP_ALLOWED_FROM_DOMAINS` is refused; an
  audit insert failure blocks a mutating tool and not a read-only one. Use a fake audit sink, or a
  throwaway Postgres via `testcontainers` if available.
- Test fixtures use `example.com` addresses and `alice`/`bob` actors only.

## Known Gaps

- **Static tokens.** No self-service, no expiry; rotation needs a restart. The successor: an OAuth
  provider in front of the server (an identity-aware proxy acting as the MCP authorization
  server) passes a signed identity assertion, which the server verifies and uses as the actor. That
  also unlocks MCP clients that can't send a static header, such as some hosted connectors.
- **No roles.** Every token can call every tool. Per-actor allow-lists (e.g. who may schedule) are
  a follow-up once actors are real identities.
- **No retention policy** on the audit table.
- **No delete tools, no batch-send tool, no templates** in v1.
- **Shared useSend rate limit.** All API keys in a useSend team share the team's rate limit
  (default 2 req/s, `Team.apiRateLimit`). The client backs off on 429. Raising the limit is a
  deployment decision.
- **useSend upgrades.** Tool shapes are checked against v1.9.8. Re-check the public API routes used
  here before moving to a newer useSend.
