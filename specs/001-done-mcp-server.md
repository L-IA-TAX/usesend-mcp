Status: done
Branch: spec-001-mcp-server
Issue:  private tracker

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
   - **`/` must be set explicitly.** FastMCP's `streamable_http_path` defaults to `/mcp`; behind a
     proxy that strips `/mcp`, the default would land the endpoint at `/mcp/mcp/`. Construct
     FastMCP with `streamable_http_path="/"`.
   - **Don't let host checks reject proxied requests.** Requests arrive with the public `Host`
     header. Construct FastMCP with `host="0.0.0.0"` and no `transport_security` allow-list, so
     DNS-rebinding protection (meant for servers bound to localhost) doesn't answer them with 421.
     Pin `mcp` to a minor range (`mcp>=1.20,<1.21` or whatever is current at build time) so a
     default change can't slip in unnoticed.
3. **Auth: a static token list (v1).** The list is comma-separated `name:token` pairs (e.g.
   `alice:<64 hex>,bob:<64 hex>`), read **once at startup** from one of two sources:
   - `MCP_TOKENS_FILE`: the path to a file holding the list (surrounding whitespace and a trailing
     newline are ignored). This is the deployment's normal path: a secret mounted as a file.
   - `MCP_TOKENS`: the list itself, in the environment. Fine for development.
   - **Both set → refuse to start** (ambiguous). Neither set → refuse to start.
   - Also refuse to start if the file is missing or unreadable, or if the list is empty, malformed,
     has a duplicate name or token, or contains the literal placeholder `CHANGE-ME` anywhere.
     Refusing means a non-zero exit and one clear log line naming the problem (and, for a
     duplicate, the name). **No token value is ever logged.**
   - There is no hot reload. Rotation = edit the secret and restart the container.
   - The deployment supplies the list as a secret. It is **never in source or in the image**: both
     are public.
   - Clients send `Authorization: Bearer <token>`. The middleware matches it with
     `hmac.compare_digest` against every entry and sets the **actor** (the name) for the request.
   - Unknown or missing token → 401. Adding or revoking someone = change the list and restart.
   - Identity from an OAuth provider in front of the server is the planned successor (Known Gaps).
4. **One service API key.** The server calls useSend with a single key (`MCP_USESEND_API_KEY`), at
   `USESEND_BASE_URL`. Users never see a useSend key.
   - **No API rate limit when self-hosted.** At v1.9.8, useSend skips its public-API rate limiter
     entirely when it isn't the cloud edition (`apps/web/src/server/public-api/hono.ts`:
     `if (isSelfHosted() || …) return next();`). The effective ceiling is the email provider's
     sending quota. The client keeps its 429 back-off anyway: it is harmless, and it covers
     useSend Cloud (2 req/s per team by default).
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
   - Base image `python:3.12-slim`, no build tools left in the final image.
   - **The server process never runs as root; the entrypoint briefly does.** A deployment may
     bind-mount the token file read-only with host ownership (`root:root`, mode `0600`), which uid
     10001 can't read. So the image starts as root in `entrypoint.sh`, which reads the file and
     then drops privileges for good (details under *Image and CI*).
8. **Nothing deployment-specific is hard-coded.** No default hostnames or domains, in the server
   or the CLI: URLs, domains and names all come from configuration.
9. **Optional Sentry error reporting.** Enabled only when `MCP_SENTRY_DSN` is set to something other
   than `none` (unset or `none` = disabled; `none` exists because some secret stores can't hold an
   empty value). `MCP_SENTRY_ENVIRONMENT` sets the environment tag.
   - `sentry-sdk` is a server dependency only; the CLI stays stdlib-only.
   - Nothing sensitive leaves the process: `send_default_pii=False`,
     `include_local_variables=False` (frame locals would carry tool arguments),
     `max_request_body_size="never"`, and a `before_send` / `before_breadcrumb` scrubber
     (`scrub.py`) that removes tool arguments (bodies, recipients, CSV content), tokens, API keys
     and request headers. The `Authorization` header is never sent, whatever else changes.
   - The scrubber keeps what's useful for debugging: the tool name, the actor, the exception type
     and the stack trace (function names and line numbers).

## Implementation

### Layout

```
usesend_mcp/
  __init__.py
  client.py        # Client + ApiError, 429 back-off, no argparse
  cli.py           # argparse CLI; `python -m usesend_mcp.cli`
  server.py        # FastMCP app, tools, auth middleware, /healthz
  auth.py          # token list loading (MCP_TOKENS / MCP_TOKENS_FILE) + constant-time lookup
  audit.py         # psycopg pool, DDL bootstrap, record()/finish()
  redact.py        # args → audit-safe JSON
  scrub.py         # Sentry init + before_send / before_breadcrumb scrubber
.claude/skills/usesend/SKILL.md
pyproject.toml     # deps: mcp (pinned minor), psycopg[binary,pool], uvicorn, sentry-sdk;
                   # extra `dev`: pytest, ruff, pgserver
entrypoint.sh
Dockerfile
.github/workflows/image.yml
dev/
  run.py           # local harness: Postgres + fake useSend + token file + server
  fake_usesend.py  # stdlib http.server answering the v1 routes the tools use
tests/
specs/
```

The CLI must keep working with **only the standard library**, so operators can run it with just
`python3`. Only `server.py`, `audit.py` and `scrub.py` import third-party packages.

**Starting point.** `usesend_mcp/cli.py` and `.claude/skills/usesend/SKILL.md` are already in the
repo: the existing CLI, imported as one file and already stripped of deployment-specific defaults
(`USESEND_BASE_URL` or `--url` with no default; key from `--key`, `$USESEND_API_KEY`, `--key-file`
or `~/.config/usesend/key`; `example.com` placeholders). The build extracts `Client` and
`ApiError` from it into `client.py` and makes `cli.py` import them, with no change to the CLI's
behaviour.

### Configuration (environment)

| Var | Meaning |
|---|---|
| `MCP_TOKENS_FILE` | Path to a file holding the token list. The usual way to supply it. |
| `MCP_TOKENS` | The token list itself: `name:token[,name:token…]`. Names match `[a-z0-9._-]+`, tokens are ≥ 32 chars. Set exactly one of `MCP_TOKENS` / `MCP_TOKENS_FILE`; startup fails otherwise, and on any list problem listed in Decision 3. |
| `MCP_USESEND_API_KEY` | The service useSend key (`us_…`). |
| `USESEND_BASE_URL` | useSend base URL **without** `/api` (e.g. `http://usesend:3000` on a shared Docker network). The client appends `/api`. |
| `MCP_DATABASE_URL` | Postgres URL for the audit role. |
| `MCP_AUDIT_SCHEMA` | Audit schema name, default `mcp`. |
| `MCP_ALLOWED_FROM_DOMAINS` | Comma-separated sender domains, required (e.g. `example.com,mail.example.com`). |
| `MCP_PORT` | Listen port, default `8080`. |
| `MCP_SENTRY_DSN` | Sentry DSN. Unset or `none` = Sentry disabled. |
| `MCP_SENTRY_ENVIRONMENT` | Sentry environment tag (e.g. `production`). Ignored when Sentry is disabled. |

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
  to a `python:3.12-slim` runtime. Create user and group `10001`, `EXPOSE 8080`,
  `ENTRYPOINT ["/entrypoint.sh"]`, `CMD ["python", "-m", "usesend_mcp.server"]`. **No `USER`
  line**: the entrypoint drops privileges itself. The runtime keeps `python` on `PATH`, so a
  `python -c` health probe works. `setpriv` comes with the base image (util-linux).
- `entrypoint.sh` (POSIX `sh`, `set -eu`):
  1. **Not root** (e.g. the container was run with `--user`): `exec "$@"` unchanged. The server
     then reads `MCP_TOKENS_FILE` itself, so the file must be readable by that user.
  2. **Root, `MCP_TOKENS_FILE` set:** if `MCP_TOKENS` is also set, print the "both set" error and
     exit 1. If the file isn't a readable regular file, print an error naming the path and exit 1.
     Otherwise `MCP_TOKENS="$(cat "$MCP_TOKENS_FILE")"`, `export MCP_TOKENS`,
     `unset MCP_TOKENS_FILE`. The server's own parser then does the full validation (empty,
     malformed, duplicates, `CHANGE-ME`), so the rules live in one place.
  3. **Root:** `exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"`.

  The token list ends up in the server's environment. Only root and uid 10001 can read
  `/proc/<pid>/environ`, and `docker inspect` doesn't show it (it isn't in the container config).
  Acceptance: with a `root:root 0600` token file bind-mounted read-only, the container starts,
  `/healthz` answers 200, and `ps -o uid,cmd` inside the container shows the server as uid
  10001.
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
- `auth`: parsing (bad, duplicate, short), constant-time match, 401 paths. Token loading: a good
  file; missing file; empty file; a file containing `CHANGE-ME`; a malformed file; both
  `MCP_TOKENS` and `MCP_TOKENS_FILE` set; neither set. Each failure exits non-zero, and captured
  logs contain none of the token values from the input.
- `routing`: with the real app, `POST /` without a token → 401 and `GET /healthz` → 200; with a
  valid token, an MCP `initialize` POST to `/` → 200 (**not** 404, and **not** 421 with
  `Host: usesend.example.com`); `POST /mcp` with a valid token → 404.
- `redact`: bodies hashed, recipients kept.
- `scrub`: a synthetic Sentry event carrying tool arguments (html, text, csv_text, to/cc/bcc,
  from), an `Authorization` header, a token and a `us_…` key comes out with none of them, anywhere
  in the event (request, extra, contexts, breadcrumbs, exception values); the tool name, actor and
  stack frames survive. `MCP_SENTRY_DSN` unset or `none` → `sentry_sdk.init` is not called.
- `entrypoint`: a shell test (run in CI inside the built image) for the three branches. As root
  with a `root:root 0600` file: the server runs as uid 10001. Both variables set: exit 1. File
  missing: exit 1.
- `server`: each mutating tool with `confirm=false` calls nothing; `create_campaign` refuses
  without the unsubscribe placeholder; a sender outside `MCP_ALLOWED_FROM_DOMAINS` is refused; an
  audit insert failure blocks a mutating tool and not a read-only one. Use a fake audit sink, or a
  throwaway Postgres via `testcontainers` if available.
- Test fixtures use `example.com` addresses and `alice`/`bob` actors only.

### Local dev harness

`dev/run.py` brings up a complete local instance with no Docker and no real useSend, so the server
can be exercised end to end:

1. Starts Postgres with `pgserver` (a pip package bundling Postgres, no root needed), data in
   `.dev/pg/`.
2. Starts `dev/fake_usesend.py` on `127.0.0.1:3999`. It is a stdlib `http.server` that answers the
   `/api/v1/*` routes the tools call with canned JSON (one domain, one contact book with 3
   contacts, one draft campaign) and appends every request it receives to `.dev/usesend.log`.
3. Writes `.dev/tokens` (`dev:<64 hex>`, mode 0600) on first run, keeping it across runs.
4. Runs the server on `127.0.0.1:8080` with `MCP_TOKENS_FILE=.dev/tokens`,
   `USESEND_BASE_URL=http://127.0.0.1:3999`, `MCP_DATABASE_URL` from pgserver,
   `MCP_ALLOWED_FROM_DOMAINS=example.com` and a dummy `MCP_USESEND_API_KEY`.
5. Stops Postgres and the fake on exit (Ctrl-C / SIGTERM).

`.dev/` is gitignored. The harness and the fake are dev-only; nothing in `usesend_mcp/` imports
them.

## Known Gaps

- **Static tokens.** No self-service, no expiry; rotation needs a restart. The successor: an OAuth
  provider in front of the server (an identity-aware proxy acting as the MCP authorization
  server) passes a signed identity assertion, which the server verifies and uses as the actor. That
  also unlocks MCP clients that can't send a static header, such as some hosted connectors.
- **No roles.** Every token can call every tool. Per-actor allow-lists (e.g. who may schedule) are
  a follow-up once actors are real identities.
- **No retention policy** on the audit table.
- **No delete tools, no batch-send tool, no templates** in v1.
- **useSend upgrades.** Tool shapes are checked against v1.9.8. Re-check the public API routes used
  here before moving to a newer useSend.

## Implementation Notes

Built as specified. The differences:

- **`mcp` 2.3, not FastMCP 1.x.** The current SDK at build time was 2.3.0. There, FastMCP is
  `mcp.server.mcpserver.MCPServer`, and the HTTP settings (`streamable_http_path="/"`,
  `stateless_http`, `json_response`, `host="0.0.0.0"`, `transport_security=None`) are passed to
  `streamable_http_app(...)` instead of the constructor. Same settings, different call site. Pinned
  `mcp>=2.3,<2.4`.
- **The `from` parameter.** `from` is a Python keyword, so `send_email` and `create_campaign` declare
  `sender: Annotated[str, Field(alias="from")]`. A small `from_alias` wrapper maps the aliased
  keyword the SDK passes back. The wire schema says `from`.
- **Tool results** are one JSON text block (the SDK would otherwise split a list into one block per
  item).
- **The scrubber is stricter than written.** Exception messages, breadcrumb messages and
  interpolated log arguments are dropped rather than pattern-scrubbed, since any of them can echo a
  body or a recipient. The exception type, frames (function, line), tool, actor and trace ids are
  kept. Sentry's logging integration only adds breadcrumbs; unexpected tool errors are captured
  explicitly, tagged with `tool` and `actor`.
- **The auth middleware** strips `Authorization` before the MCP app runs, lets only `lifespan` bypass
  it, and closes websockets (1008).
- **Startup** also refuses (exit 1, one line) on a missing required variable, an invalid
  `MCP_AUDIT_SCHEMA`, or an unreachable audit database. The audit DDL runs before serving.
- **Added:** `MCP_HOST` (default `0.0.0.0`; the dev harness uses `127.0.0.1`) and a `.dockerignore`.
  `client.py` also holds the CSV row rules (`parse_contacts_csv`), shared by the CLI and
  `import_contacts`.
- **CI:** the GHCR image name lowercases the owner (`${GITHUB_REPOSITORY_OWNER,,}`), because GHCR
  rejects uppercase. The entrypoint test (`tests/entrypoint_test.sh`) runs in the `image-test` job
  against the built image and Postgres 16, checks the uid with both `docker top` and `/proc/1/status`,
  and also covers the non-root pass-through and that the token is absent from `docker inspect`.
- **Tests:** the audit is also exercised against a real Postgres via `pgserver` (no Docker), as well as
  through a fake sink.
- **Upstream checks** at v1.9.8: custom email `headers`, `Idempotency-Key` on `POST /v1/emails`, the
  campaign `id`/`contactBookId`, and `_count.contacts` on `GET /v1/contactBooks/{id}`.
- **Deferred:**
  - report audit-DB write failures to Sentry (they are logged to stdout only);
  - set `serverInfo.version` from package metadata (currently empty).
- **Manual, once:** set the GHCR package visibility to public after the first `v*` tag.

**Change division.** There is no `CONTRIBUTING.md`, so this used the CLAUDE.md conventions. One concern
per commit, in a single PR stacked on the deploy-requirements PR. One divergence: the server commit
left one ruff line-length error in a test file, fixed in the next commit (tests passed at every
commit). It was already pushed, so it is reported here rather than rewritten.
