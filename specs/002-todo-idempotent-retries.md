# 002 — Safe retries: content-derived idempotency keys and an "unknown" outcome

Follows [001](001-done-mcp-server.md) (review of its build). Targets useSend **v1.9.8**.

## Context

An MCP client's model retries. After a timeout or an error it often calls the same tool again with
the same arguments. 001 doesn't make that safe:

- **The idempotency key is per audit row, not per send.** `send_email` sends
  `Idempotency-Key: mcp-audit-<row id>`, and every tool call gets a new row. Two identical calls
  send `mcp-audit-1` and `mcp-audit-2`, so useSend sends the email twice.
- **A timeout reads as a failure.** A read timeout in `Client.request` raises `TimeoutError`, which
  nothing translates. The audit row says `error` and the model sees a generic failure, although
  useSend may have accepted the request and sent the email. The model's natural next step is to
  retry, which (with the per-row key) sends it again.
- **A partial import is invisible.** `import_contacts` posts 1000-row batches in sequence. If
  batch 2 fails, batch 1 is already applied, but neither the tool result nor the audit row says
  how many rows went in.

What useSend offers (checked at v1.9.8, `idempotency-service.ts`, `send-email.ts`):

- `POST /v1/emails` and `POST /v1/emails/batch` accept `Idempotency-Key` (≤ 256 chars). The key is
  stored per team for **24 hours** with a hash of the canonical request body.
- Same key + same body → the original `emailId`, 200, **nothing is sent again**.
- Same key + different body → 409 `NOT_UNIQUE`. Same key while the first request is in flight
  → 409.
- Campaign routes (`schedule`, `resume`, `pause`) and contact routes take no key.

## Decision

1. **`send_email` derives its key from the content, per actor.**
   `Idempotency-Key: mcp-<sha256 hex of the canonical JSON of [actor, payload]>`, where `payload`
   is exactly the JSON body sent to `/v1/emails` (the `X-MCP-Actor` header isn't part of the body;
   the actor is already in the hash). Canonical JSON = `sort_keys=True`,
   `separators=(",", ":")`, `ensure_ascii=False`. The key is 68 chars.
   - Same actor, same email, within 24 h → useSend returns the original id and sends nothing. The
     audit row for the repeat records `ok` with that same email id.
   - A deliberate identical resend needs a new tool argument, `resend: bool = false`. With `true`,
     the audit row id is appended to the hashed material, so the key is new. The tool description
     says when to use it ("only when the user explicitly wants the same email sent again").
   - The key is stored in a new nullable audit column `idempotency_key`, so a repeat is visible in
     the log (two rows, one key).
2. **Errors after the request may have reached useSend become `unknown`, not `error`.**
   - `client.py` gets `OutcomeUnknown(ApiError)`. `Client.request` raises it for a read timeout, a
     connection reset or close after the request was written, or an HTTP 502/503/504 (a proxy
     answered; useSend may have acted). It keeps raising plain `ApiError` for failures that prove
     nothing was done: connection refused, DNS failure, and 4xx responses.
   - The audit outcome set gains `unknown`. The tool result is an error whose text says the result
     is unknown and what to do:
     - `send_email`: "The email may have been sent. Calling send_email again with the same
       arguments is safe within 24 hours: it won't send twice." (true because of 1);
     - `schedule_campaign` / `resume_campaign` / `pause_campaign` / `cancel_email`: "Check the
       current state with get_campaign / get_email before retrying.";
     - contact tools: "Check with list_contacts before retrying."
   - The CLI prints the same distinction (`result unknown: …`) and exits 2 instead of 1.
3. **`import_contacts` reports partial progress.** Batches still run in order and stop at the first
   failure. On a failure the tool result (an error) and the audit row's `error` both state
   `applied <n> of <total> rows (<k> of <m> batches)` and which outcome the failing batch had
   (`error` or `unknown`). The audit outcome is `error`/`unknown` as for any other failure.
4. **Schema change, in place.** At startup, after the existing `CREATE … IF NOT EXISTS`:
   - `ALTER TABLE <schema>.audit_log ADD COLUMN IF NOT EXISTS idempotency_key text;`
   - replace the outcome check: drop the constraint by name if it exists, then
     `ALTER TABLE … ADD CONSTRAINT audit_log_outcome_check CHECK (outcome IN
     ('pending','ok','error','refused','preview','unknown'))`. Both run in one transaction, so a
     concurrent second replica can't observe the table without a check. The 001 `CREATE TABLE`
     gets the same constraint name, so new and migrated tables match.

## Implementation

- `client.py`: `OutcomeUnknown`; classify exceptions in `request()` (the 429 back-off is
  unchanged: a 429 proves nothing was done). `canonical_json()` helper.
- `server.py`: key derivation in `send_email`; `resend` argument; `OutcomeUnknown` branch in
  `call()` (outcome `unknown`, per-tool guidance text); partial-progress reporting in
  `import_contacts`.
- `audit.py`: the migration DDL; `finish()` accepts `idempotency_key`.
- `cli.py`: exit code 2 and the `result unknown` message. `emails send` keeps its explicit
  `--idempotency-key`; with none given it uses the same content-derived key (without an actor).
- README: the retry behaviour in "Safety rules"; `unknown` in the audit outcome list.

## Tests

- Key: two identical `send_email` calls by one actor send the same key; different actor, a
  changed field, or `resend=true` → a different key; the key is ≤ 256 chars.
- Against the mock: a cached-replay reply (same id) on the second call → two `ok` rows, one
  `idempotency_key`, one email id.
- Classification: read timeout, connection reset, 502/503/504 → `OutcomeUnknown`; connection
  refused, 400/401/404/409 → `ApiError`; 429 still retried.
- Tool: a timeout on `send_email` → error result with the "safe to call again" text, audit
  outcome `unknown`; on `schedule_campaign` → the "check get_campaign" text.
- Import: batch 2 of 3 fails → result and audit row say `applied 1000 of 2500 rows (1 of 3
  batches)`; batch 3 was never posted.
- Migration (real Postgres): a table created with 001's DDL is migrated at startup; an `unknown`
  row can then be written; running startup twice is a no-op.

## Known Gaps

- **Campaign calls can't be made idempotent.** useSend takes no key on campaign routes. The
  `unknown` guidance (check state first) is the mitigation.
- **24-hour window.** An identical retry after 24 hours sends again. Acceptable: a model's retry
  happens within seconds or minutes.
- **No server-side duplicate refusal.** The server doesn't look up earlier audit rows to refuse a
  duplicate itself; it relies on useSend's key. Doing it in the audit DB would survive a useSend
  Redis flush but adds a read to every send. Revisit if useSend's key handling changes.
