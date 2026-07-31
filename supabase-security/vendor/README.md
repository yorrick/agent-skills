# Vendored: Splinter

`splinter.sql` is Supabase's own Postgres linter — the engine behind the dashboard's
**Security Advisor** and the `get_advisors` MCP tool.

- Source: <https://github.com/supabase/splinter>
- Pinned commit: see `SPLINTER_VERSION`
- Upstream ships this as a single self-contained query at the repo root, explicitly
  "for anyone only interested in linting a project".

## Why vendored rather than fetched

The auditor should work offline and give reproducible results. Fetching at runtime would
make findings depend on whatever upstream happened to be that morning, and would fail in
a sandbox with no network.

## Licence

**Splinter has no LICENSE file** (verified via the GitHub API). It is a public Supabase
repository, distributed for exactly this use, but there is no explicit grant. It is kept
here **unmodified and attributed**, in its own `vendor/` directory, so its provenance is
unambiguous and it can be removed cleanly.

If this skill is ever published more broadly, confirm the licensing position with
Supabase first, or drop this directory and shell out to the user's own copy.

## Updating

```bash
gh api repos/supabase/splinter/contents/splinter.sql --jq .content | base64 -d > splinter.sql
gh api repos/supabase/splinter/commits --jq '.[0] | "\(.sha[0:7]) \(.commit.author.date)"' > SPLINTER_VERSION
```

Do not edit `splinter.sql` by hand. If a rule needs changing, that belongs upstream.
