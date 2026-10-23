# usesend-mcp

An [MCP](https://modelcontextprotocol.io) server and CLI for a self-hosted
[useSend](https://github.com/usesend/useSend). With it, a team can send email and create,
schedule and track marketing campaigns by asking an AI assistant. Every action is recorded against
the person who asked for it.

> **Status: specified, not built yet.** The design is in [`specs/`](specs/CATALOG.md). Everything
> below describes the target behaviour from [001](specs/001-todo-mcp-server.md).

## Why a server, and not useSend API keys

useSend API keys belong to the team, not to a person. A key records no creator, any member can
create one, and its permission level isn't enforced. Campaigns record neither the key nor the
user. So "everyone uses their own key" gives you nothing to attribute actions to.

This server sits in front of useSend and does three things:

- **knows who is calling:** each person gets their own token;
- **calls useSend with one service key** that no user ever sees;
- **writes an audit row for every tool call:** who, what, when, outcome, and the resulting
  email/campaign ids.

## Using it from Claude

Get your server URL and a token from whoever runs the deployment, then:

```bash
claude mcp add --transport http usesend https://<your-server>/ \
  --header "Authorization: Bearer <token>"
```

If the server is mounted under a path, use the URL with its trailing slash (e.g.
`https://usesend.example.com/mcp/`). Then just ask, for example:

- "list my contact books"
- "import this CSV into the 'Customers' book"
- "create a campaign for 'Customers' from news@example.com with this HTML"
- "how did last week's campaign do?"

### Tools

| Kind | Tools |
|---|---|
| Read | `list_domains`, `list_contact_books`, `get_contact_book`, `list_contacts`, `list_campaigns`, `get_campaign` (with stats), `list_emails`, `get_email`, `get_analytics` |
| Contacts | `create_contact_book`, `add_contact`, `import_contacts` (CSV), `update_contact` (incl. unsubscribe) |
| Email | `send_email`, `cancel_email` |
| Campaigns | `create_campaign`, `schedule_campaign`, `pause_campaign`, `resume_campaign` |

### Safety rules

- **Campaigns are created as drafts.** Review them in the useSend UI before scheduling.
- **Sending needs confirmation.** `send_email`, `schedule_campaign` and `resume_campaign` return
  a preview (from, subject, recipients, when) unless called with `confirm=true`. MCP clients also
  ask before running them.
- **Campaign HTML must contain `{{usesend_unsubscribe_url}}`.** useSend refuses to send without
  it, and so does the server, earlier.
- **Senders are allow-listed.** `from` must be on a domain in `MCP_ALLOWED_FROM_DOMAINS`.
- **No delete tools** in v1.
- `pause_campaign` is always allowed. It's the emergency stop.

Contact variables such as `{{firstName}}`, `{{lastName}}` and custom properties are filled in per
recipient by useSend.

## CLI

The same operations work from a terminal, using only the Python standard library. The CLI talks
to the useSend API directly, so it needs a useSend API key, and its calls are **not** in the
audit log.

```bash
export USESEND_BASE_URL=https://usesend.example.com
export USESEND_API_KEY=us_…            # or put it in ~/.config/usesend/key (chmod 600)
python3 -m usesend_mcp.cli books list
python3 -m usesend_mcp.cli contacts import <bookId> contacts.csv
python3 -m usesend_mcp.cli campaigns create --name 'Oct/26' --from 'News <news@example.com>' \
    --subject 'News' --book <bookId> --html-file news.html
python3 -m usesend_mcp.cli --dry-run campaigns schedule <campaignId> --at 'tomorrow 9am'
python3 -m usesend_mcp.cli campaigns get <campaignId>
```

Anything that sends or deletes asks first. `--dry-run` prints the request and sends nothing;
`--yes` skips the prompt. A Claude skill wrapping the CLI lives in
[`.claude/skills/usesend/`](.claude/skills/usesend/).

CSV import columns: `email` (required), `firstName`/`lastName` (or `first_name`/`last_name`),
`subscribed`. Any other column becomes a contact property.

## Running it

The server is one container: `ghcr.io/<owner>/usesend-mcp:vX.Y.Z`. Run it where it can reach
useSend's API and a Postgres database. Next to useSend on the same Docker network works well.

- MCP over streamable HTTP, stateless, at `/`. A reverse proxy may mount it under a path, provided
  it strips the prefix.
- `GET /healthz` is unauthenticated and checks the audit database.
- Images are built on `v*` tags. There is no `:latest`: pin the exact tag.

### Configuration

| Variable | Purpose |
|---|---|
| `MCP_TOKENS` | `name:token[,name:token…]`. The name is what the audit log records. |
| `MCP_USESEND_API_KEY` | The server's own useSend key. |
| `USESEND_BASE_URL` | useSend base URL without `/api`, e.g. `http://usesend:3000`. |
| `MCP_DATABASE_URL` | Postgres URL for the audit role. |
| `MCP_AUDIT_SCHEMA` | Audit schema, default `mcp`. |
| `MCP_ALLOWED_FROM_DOMAINS` | Comma-separated sender domains, e.g. `example.com`. |
| `MCP_PORT` | Listen port, default `8080`. |

Pass secrets through your deployment's secret store. Never commit them.

### Audit log

`<schema>.audit_log` has one row per tool call:

- who (`actor`), what (`tool`), and the arguments;
- the `outcome`: `ok`, `error`, `refused`, `preview`, or `pending` if the process died mid-call;
- the resulting `email_ids`, `campaign_id` and `contact_book_id`.

The arguments are stored redacted: email bodies and CSV content are replaced by a hash and a
size, while recipients and subjects are kept. If the audit row can't be written, a mutating tool
refuses to act. Sent emails also carry an `X-MCP-Actor` header.

If the audit schema lives in useSend's own database (recommended: a dedicated role with only
`CONNECT` + `CREATE`), it joins with useSend's tables directly:

```sql
SELECT a.at, a.actor, c.name, c.sent, c.delivered, c.opened
FROM mcp.audit_log a JOIN public."Campaign" c ON c.id = a.campaign_id
WHERE a.tool = 'schedule_campaign' AND a.outcome = 'ok'
ORDER BY a.at DESC;
```

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                                  # no network: useSend is mocked
MCP_TOKENS=alice:$(openssl rand -hex 32) MCP_USESEND_API_KEY=us_… \
USESEND_BASE_URL=https://usesend.example.com MCP_DATABASE_URL=postgresql://… \
MCP_ALLOWED_FROM_DOMAINS=example.com python -m usesend_mcp.server
```

Releasing: push a `vX.Y.Z` tag and wait for the image workflow. Then bump the pinned tag in your
deployment.

The tool shapes are checked against useSend **v1.9.8**. Before moving to a newer useSend,
re-check the public API routes this server uses.

## Specs

Design decisions live in [`specs/`](specs/CATALOG.md):

| # | Spec |
|---|---|
| [001](specs/001-todo-mcp-server.md) | MCP server: tools, token auth, audit log, image |
