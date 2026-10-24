# usesend-mcp

## Dev server

Local harness from spec 001 (`dev/run.py`): Postgres via `pgserver`, a stdlib fake useSend on
`127.0.0.1:3999`, a dev token in `.dev/tokens`. No Docker or real useSend needed (neither is
available on the dev machine). Python 3.12, matching the image (`pgserver` wheels don't cover the
system Python).

```
Setup: uv venv --python 3.12 .venv && uv pip install -p .venv -e '.[dev]'
Start: .venv/bin/python dev/run.py
Port:  8080 (fake useSend 3999)
Ready: curl -sf http://127.0.0.1:8080/healthz   → {"ok": true}
Auth:  Authorization: Bearer $(cut -d: -f2 .dev/tokens)
Stop:  Ctrl-C / SIGTERM to dev/run.py (it stops Postgres and the fake)
```

Live evidence is API-level (curl), since there is no UI: `POST /` without a token → 401; an MCP
`initialize` + `tools/list` with the token; a `confirm=false` tool call → a preview and a
`preview` audit row (`psql` against the pgserver URL the harness prints); the fake's request log
in `.dev/usesend.log` shows no call was made.

## This repository is PUBLIC

Everything committed here (code, specs, README, commit messages, PR titles and bodies, issue
comments, test fixtures) is world-readable, and git history keeps it after a later commit removes
it. Write as if for an outside user who runs their own useSend.

**Never commit:**

- **People:** names, usernames, emails, phone numbers, or GitHub/Linear handles of team members
  or customers. That includes branch names that embed a person's name.
- **Company specifics:** real domains and hostnames, sender addresses, contact book or campaign
  names, customer data, or sample data copied from production.
- **Infrastructure:** cloud account IDs, regions, instance types, database hosts, role and user
  names, parameter-store / secret-store paths, internal scripts and commands, deploy runbooks,
  CDN or access-policy setup, internal repo names, internal spec, concern or issue numbers, and
  links to private repos, dashboards or Linear issues.
- **Security posture:** known weaknesses, accepted risks, or gaps in the deployment (e.g. which
  secrets a container can read). These are attacker roadmaps.
- **Secrets**, obviously, including example values that look real.

**Instead:**

- Use placeholders: `usesend.example.com`, `news@example.com`, `<your-org>`, `<token>`,
  `alice`/`bob` as actors, `ghcr.io/<owner>/usesend-mcp`.
- Make anything deployment-specific configurable (env var, flag) instead of hard-coding it.
- Keep deployment work, runbooks and operational gaps in the private infra repo. A spec here may
  say "deployment is specified elsewhere" and nothing more.
- Describe upstream useSend facts (its API, schema, versions) freely; they are public already.

**Before every commit and PR**, scan the diff for the items above, e.g.:

```bash
git diff --cached | grep -niP 'liatax|l-ia-tax|\blia\b|@(?!example\.)[a-z0-9-]+\.[a-z]{2,}|\b(rds|ssm)\b|arn:|\bt3\.|cloudflare|infra (repo|spec|concern)'
```

If something slips into a commit that hasn't been pushed, amend or rebase it out. If it was
pushed, stop and tell the user: removing it needs a history rewrite and possibly a secret rotation.
