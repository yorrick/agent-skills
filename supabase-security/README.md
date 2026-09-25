# supabase-security

Access-control rules for Supabase projects exposed directly to a browser via PostgREST,
plus a read-only audit script.

## Install

```bash
# Claude Code
claude plugin marketplace add yorrick/agent-skills
claude plugin install supabase-security@yorrick

# Codex
codex plugin marketplace add yorrick/agent-skills
codex plugin add supabase-security@yorrick
```

## What it covers

The three enforcement layers and — the part that causes most confusion — **their order**:
`GRANT` is checked before RLS, RLS constrains rows and cannot correlate `OLD` with `NEW`,
and only a trigger sees everything at once. Getting that order wrong is what makes an
admin policy silently unreachable behind a revoked column.

Sixteen rules, a column-authorization trigger pattern with a test matrix, a pre-merge
checklist, and a reference for the services that sit beside PostgREST: Storage buckets,
Realtime channels and Edge Functions, each of which has its own access gate.

## The audit script

```bash
uv run scripts/audit_rls.py --db-url "$DATABASE_URL" --schema public
uv run scripts/audit_rls.py --db-url "$DATABASE_URL" --json          # for CI
uv run scripts/audit_rls.py --db-url "$DATABASE_URL" --no-splinter   # own rules only
```

Read-only, enforced by a read-only transaction rather than merely asserted.

### It bundles Supabase's own linter

**[Splinter](https://github.com/supabase/splinter) is Supabase's work, not ours.** It is
a pure-SQL linter (~29 rules) that powers the dashboard's Security Advisor and the
`get_advisors` MCP tool. Upstream publishes a single self-contained `splinter.sql`
explicitly for project linting; it is vendored here **unmodified** at
`vendor/splinter.sql`, pinned to the commit in `vendor/SPLINTER_VERSION`.

This script runs it and adds **rules Splinter does not have**:

| | |
|---|---|
| `R1` | permissive policies covering ALL commands, or policies applying `TO PUBLIC` (including Storage and Realtime policies) |
| `R2` | RLS tables with no `RESTRICTIVE` policy, or one that covers reads but not writes |
| `R4` | delete-and-reinsert defeating a column-level `UPDATE` revoke |
| `R11` | `TRUNCATE`, which no RLS policy applies to |
| `R12` | default privileges that expose every future table or function |
| `R13` | an Auth hook left executable by the API roles |
| `R14` | public Storage buckets |

It also sets `pgrst.db_schemas` before running, so Splinter audits every schema PostgREST
actually exposes. The list comes from the `authenticator` role setting or from `--schema`;
when neither is available the script exits 2 instead of guessing `public`, because a
guessed list gives a clean report on a project that serves an `api` schema.

Findings are prefixed `splinter:` where they come from Splinter. You can always run it
standalone: `psql -f vendor/splinter.sql "$DATABASE_URL"`.

See `vendor/README.md` for provenance and the licence position.

## Not a substitute for tests

Lints check configuration; only tests check reality. Assert 401/403/empty on every table,
view and RPC using the publishable key alone, and again with a second tenant's JWT.
