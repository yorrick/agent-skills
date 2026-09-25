# Local lab

A local Supabase stack for testing the auditor and the trigger-guard pattern. It runs
on its own ports (API 58621, database 58622) so it does not collide with a project on
the CLI defaults. Studio, Mailpit and analytics are disabled to keep startup fast.

```bash
supabase start --workdir supabase-security/lab
uv run pytest tests/test_supabase_security_audit.py
supabase stop --workdir supabase-security/lab
```

The tests skip themselves when the lab is not running. Set `SUPABASE_LAB_DB_URL` to
point them at another local database.

| File | What it checks |
|---|---|
| `audit-fixtures.sql` | one misconfiguration per auditor rule, each beside a correct counterpart the rule must not flag |
| `trigger-guard/setup.sql` | an `account` table with RLS, a regular user and an admin |
| `trigger-guard/tests.sql` | the full test matrix from `references/trigger-guard-pattern.md`, runnable with `psql -f` after the guard migration |

Everything here targets the local stack only. Never point it at a hosted project.
