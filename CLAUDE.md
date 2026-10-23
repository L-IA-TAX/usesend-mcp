# usesend-mcp

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
