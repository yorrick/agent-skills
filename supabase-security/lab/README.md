# Local lab

A local Supabase stack for testing the auditor, trigger guard, and HTTP access gates. It runs
on its own ports (API 58621, database 58622) so it does not collide with a project on
the CLI defaults. Studio, Mailpit and analytics are disabled to keep startup fast.

```bash
supabase start --workdir supabase-security/lab
uv run pytest tests/test_supabase_security_audit.py
uv run pytest tests/test_supabase_security_http.py
supabase stop --workdir supabase-security/lab
```

The tests skip themselves when the lab is not running. The HTTP tests accept only this
lab at `127.0.0.1:58621` with the local PostgreSQL credential; they obtain API keys
from `supabase status` without printing them. Node.js is required for the direct
Realtime WebSocket probe. The Edge Function test starts and stops `supabase functions
serve` itself. Set `SUPABASE_LAB_DB_URL` only for the older database-level tests.

| File | What it checks |
|---|---|
| `audit-fixtures.sql` | one misconfiguration per auditor rule, each beside a correct counterpart the rule must not flag |
| `trigger-guard/setup.sql` | an `account` table with RLS, a regular user and an admin |
| `trigger-guard/tests.sql` | the full test matrix from `references/trigger-guard-pattern.md`, runnable with `psql -f` after the guard migration |
| `realtime-probe.mjs` | direct public and private Phoenix channel join and broadcast checks |
| `supabase/functions/lab-tenant/index.ts` | local handler that verifies a user before its service-role query |

The HTTP suite first reproduces a PostgREST cross-tenant read, a public Storage
download, and a public Realtime broadcast. It then checks the tightened table and
Storage policies, topic-scoped private channel, and Edge Function with two real Auth
users. All fixtures are removed after each test.

Everything here targets the local stack only. Never point it at a hosted project.
