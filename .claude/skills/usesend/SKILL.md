---
name: usesend
description: Send emails and create/schedule/track marketing campaigns through a self-hosted useSend — contact books, CSV contact import, campaigns, stats. Use when the user wants to send an email, build an audience, or create, send, pause or check a campaign.
---

# useSend — emails & campaigns

All calls go through the stdlib-only CLI: `python3 -m usesend_mcp.cli` from the repo root.
`python3 -m usesend_mcp.cli <group> <cmd> -h` documents every flag.

## Setup
- **Base URL:** `$USESEND_BASE_URL` (or `--url`), without `/api`. There is no default; if it is
  unset, ask the user for their useSend URL.
- **Key:** a useSend API key (`us_…`, UI → Developer settings → API keys). The CLI reads
  `$USESEND_API_KEY`, else `~/.config/usesend/key`. **Never print, echo, or write the key into the
  repo or the conversation.** If no key is set, ask the user to create one and save it themselves:
  `! install -m 600 /dev/null ~/.config/usesend/key && $EDITOR ~/.config/usesend/key`.
  Prefer a key dedicated to this use, not one shared with an application.

## Rules (outward-facing — mail cannot be unsent)
1. Before anything that sends (`emails send|batch`, `campaigns create --send-now|--schedule`,
   `campaigns schedule|resume`), show the user: from, subject, recipient count / book name, when.
   Then run it with `--dry-run` first, and with `--yes` only after the user explicitly confirms.
2. Prefer creating the campaign as a **draft** (no `--send-now`/`--schedule`) and have the user check
   it in the useSend UI (`<base URL>/campaigns/<id>`) before `campaigns schedule`.
3. Campaign HTML must contain `{{usesend_unsubscribe_url}}` (useSend refuses otherwise; the CLI
   checks). Contact variables like `{{firstName}}` / `{{lastName}}` / custom properties are replaced
   per recipient.
4. `from` must be on a domain verified in useSend (`domains list`). Never create or delete domains;
   `domains` is read/verify only.
5. Deletes (`books delete`, `contacts delete`, `campaigns delete`) need explicit user approval.

## Typical flow
```bash
U="python3 -m usesend_mcp.cli"
$U books list                                          # or: books create --name 'Customers'
$U contacts import <bookId> contacts.csv               # columns: email[,firstName,lastName,subscribed,<props>]
$U campaigns create --name 'News Oct/26' --from 'News <news@example.com>' \
   --subject 'News' --book <bookId> --html-file news.html --preview-text '…'
# user reviews the draft in the UI, then:
$U --dry-run campaigns schedule <campaignId> --at '2026-10-08T09:00:00+00:00'
$U --yes     campaigns schedule <campaignId> --at '2026-10-08T09:00:00+00:00'
$U campaigns get <campaignId>                          # total/sent/delivered/opened/clicked/bounced
$U campaigns pause <campaignId>                        # emergency stop
```
One-off email: `$U emails send --from 'News <noreply@example.com>' --to alice@example.com --subject … --html-file …`
(`--schedule ISO` to delay, `emails cancel <id>` to drop a scheduled one).

## Errors
- HTTP 401/403 → bad key. An HTML body instead of JSON → something in front of the API (a proxy or
  an access gate) answered instead of useSend. 429 is retried automatically.
