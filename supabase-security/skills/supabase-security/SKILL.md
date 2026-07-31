---
name: supabase-security
description: "Access control for Supabase projects where a browser talks straight to PostgREST with no API middle layer. Load this skill BEFORE writing or reviewing anything that decides who can read or write data: RLS policies, GRANT/REVOKE statements, SECURITY DEFINER functions, RPCs, views, triggers used as authorization, migrations that touch permissions, custom JWT claims, or code that handles an anon/publishable key or a service_role/secret key. Also load it when diagnosing 'permission denied for table', a 403 or empty result that should have returned rows, when a user can see or change data belonging to another tenant, or when asked to audit a Supabase project for privilege escalation. Triggers on: RLS, row level security, Supabase policy, anon key, service_role, privilege escalation, multi-tenant isolation, column level security, PostgREST."
license: MIT
---

# Supabase Access Control

With no API between the browser and the database, **Postgres is the entire security perimeter**. There is nowhere else to put a check a determined client cannot route around. Every rule here follows from that.

## The one-paragraph model

> The **anon key** says *which project*. The **JWT** says *which user*. Neither says *what you may do* — that is schema exposure, `GRANT`, RLS, and (for per-column rules) triggers.

Teams get breached by believing the anon key is a credential. It is not. It is public by design and ships in your JS bundle. In [CVE-2025-48757](https://mattpalmer.io/posts/2025/05/CVE-2025-48757/) (CVSS 9.3), 170+ production apps were fully readable *and writable* by anyone holding the public key — not because the key leaked, but because RLS was off. The key was doing exactly what it was designed to do.

## The enforcement layers, and their order

This ordering causes more confusion than anything else in Supabase. Learn it before writing a policy.

```
1. GRANT           role → table/column    no predicates, no rows
2. RLS USING       which existing ROWS may be touched
3. BEFORE trigger  sees OLD, NEW and the caller; may alter NEW
4. RLS WITH CHECK  validates the resulting NEW row
5. Constraints
```

Note steps 3 and 4: **`WITH CHECK` runs *after* `BEFORE` triggers**, so a trigger that rewrites `NEW` is still subject to the policy.

Three consequences, each of which has caused a production incident:

**Each layer only narrows the one above it.** RLS can never grant a privilege the GRANT layer withheld. If you revoke a column, a policy saying "admins may update everything" becomes *unreachable* — the privilege check rejects the statement before any row is considered. This is Postgres-documented; **Supabase does not document it anywhere.**

**RLS cannot compare the old and new values.** A policy *can* reference columns — what it cannot do is correlate `OLD` with `NEW`, because `WITH CHECK` only ever sees the proposed row. So *"this column must not change"* is **inexpressible as a policy.** (A policy can still pin a column to a stable external invariant, e.g. `tenant_id = auth.jwt() ->> 'tenant_id'` — that works because it needs no `OLD`.) General per-column change rules need a trigger.

**Only a trigger sees everything.** `OLD`, `NEW`, and the caller's identity at once. That is why it is the only place to write "admins may change this column, others may not."

|  | knows role | can call `is_admin()` | sees OLD | sees NEW |
|---|---|---|---|---|
| GRANT | yes | **no** | no | no |
| RLS | yes | yes | **no** | yes |
| trigger | yes | yes | **yes** | **yes** |

## Rules

### R1 — Never write a policy without `FOR` and `TO`

```sql
-- WRONG: applies to ALL commands and ALL roles
create policy "Users can view their account" on accounts
  using (id = (select auth.uid()));

-- RIGHT
create policy "Users can view their account" on accounts
  for select to authenticated
  using (id = (select auth.uid()));
```

Postgres: *"The default for newly created policies is that they apply for all commands and roles."* A policy you filed mentally as "the read policy" is silently your UPDATE and DELETE policy too. A policy named `"Users can **view**…"` that omits `FOR SELECT` is a write policy — this exact bug caused three separate escalations in one codebase.

Omitting `TO` defaults to `PUBLIC`. Naming the role is also a documented ~99% performance win.

### R2 — Tenant isolation must be RESTRICTIVE

Permissive policies (the default) are **ORed**. A stricter policy added later cannot tighten anything — it only adds another true-branch:

```
is_admin() OR id IN (my accounts)  →  false OR true  →  ALLOWED
```

So tenant isolation written as a permissive policy can be ORed past by *any* feature policy added later. Write it restrictive so it ANDs — but **a restrictive policy grants nothing.** Postgres requires at least one *permissive* policy to pass as well; restrictive-only means default-deny and nothing works. You need both:

```sql
-- The permissive policy grants access...
create policy documents_read on documents
  for select to authenticated
  using (true);

-- ...and the restrictive one constrains it. Both must pass.
create policy tenant_isolation on documents
  as restrictive for select to authenticated
  using (tenant_id = (select auth.tenant_id()));
```

Three caveats that bite:

- **A restrictive policy applies to every access by that role, including admins.** If staff need cross-tenant reads and they authenticate as `authenticated`, the predicate must exempt them explicitly (`... or public.is_admin()`) or they are locked out. `service_role` is unaffected — it has `BYPASSRLS`.
- **Isolation is per table.** A join reaches other tables, each of which needs its own correct policy. One table's restrictive policy protects only that table.
- Use restrictive for an invariant that must *always* hold (tenancy). Never for a role check on its own — `as restrictive using (is_admin())` locks out every ordinary user.

### R3 — State `WITH CHECK` explicitly

If omitted on `ALL`/`UPDATE`, Postgres reuses `USING` as `WITH CHECK`. Usually not what you meant, and it hides intent. Write both.

Note the precise failure: an explicit-but-identical `WITH CHECK` is *equally* vulnerable. The defect is never the reuse itself — it is that the predicate identifies **which row** may be written, without constraining how the row's protected values may change.

### R4 — A column `GRANT` is not a control if the role holds both INSERT and DELETE

```sql
revoke update on posts from authenticated;
grant update (title, body) on posts to authenticated;   -- looks safe
```

It is not, *if the same role can also delete and re-insert the row*. `DELETE` is a **whole-row privilege** — it cannot be granted per column. The client deletes the row and inserts a replacement with any values it likes. No `UPDATE` occurs, so neither the column grant nor an `UPDATE` trigger ever fires.

```sql
revoke insert, delete on posts from authenticated;
```

**Revoking either one closes this path** — the attack needs both (delete the row, then recreate it). Revoke whichever the client genuinely does not need; revoke both if it needs neither. `DELETE` alone is destructive but not an escalation: the row is gone, not rewritten.

Two further conditions must also hold for the bypass to work, so check them before assuming you are safe *or* exposed: the role needs INSERT on the protected columns specifically, and constraints/FKs must permit recreating the row.

Undocumented by both Supabase and Postgres, but mechanically certain. **Any `BEFORE UPDATE` trigger guard has the identical hole.**

### R5 — `REVOKE ... FROM PUBLIC` does not lock down a function

Supabase has historically granted `EXECUTE` on new `public`-schema functions **directly** to `anon` and `authenticated`. `REVOKE FROM PUBLIC` strips only Postgres's implicit grant; the direct role grants survive untouched. Name the roles:

```sql
revoke all on function public.f(text) from public, anon, authenticated;
grant execute on function public.f(text) to service_role;
```

Projects created from **30 May 2026** get a safer default that does not auto-expose new objects. **Existing projects were not migrated** — check yours rather than assume, and keep the explicit revokes either way (they are harmless when redundant).

**A function has no policies of its own.** That does not mean it bypasses RLS: a `SECURITY INVOKER` function (the default) runs table queries as the *caller*, so their policies still apply. A `SECURITY DEFINER` function runs as the owner — usually `postgres`, which owns the tables and therefore bypasses RLS. That is the dangerous case, and in an exposed schema it is a `POST /rpc/f` away from any browser. Assume every such function is internet-facing and authorize inside it.

### R6 — `SECURITY DEFINER` functions: pin `search_path`, check the caller

```sql
create function public.admin_thing(target uuid)
returns void language plpgsql security definer
set search_path = ''                       -- or 'pg_catalog, public, pg_temp'
as $$
begin
  if not public.is_admin() then
    raise exception 'Not authorized' using errcode = 'insufficient_privilege';
  end if;
  ...
end $$;
```

A definer function that does neither is a privilege-escalation primitive. Tables are typically owned by `postgres`, so a definer function owned by `postgres` **reads and writes with RLS off**.

### R7 — Views need `security_invoker = on`

Views run with the **owner's** privileges by default, bypassing the caller's RLS on underlying tables. `security_invoker` exists in PG15+ and is **not** the default.

```sql
create view public.v with (security_invoker = on) as select ...;
```

Materialized views and foreign tables **cannot enforce RLS at all** — keep them out of exposed schemas.

### R8 — Authorization claims come from `app_metadata`, never `user_metadata`

`raw_user_meta_data` is **user-writable through the auth API**. A policy reading `auth.jwt() -> 'user_metadata' ->> 'role'` is self-service admin. Supabase lints this as ERROR (`0015`).

### R9 — Keys

| Key | Where | Notes |
|---|---|---|
| anon / `sb_publishable_…` | **public**, ships in the browser | selects the `anon` role; not a secret, not a credential |
| service_role / `sb_secret_…` | **server only** | carries `BYPASSRLS` — *no policy can constrain it* |

Never in `VITE_*` / `NEXT_PUBLIC_*` — that prefix ships it to the browser. Supabase rejects secret keys sent with a browser `User-Agent`, but that is a safety net for accidents, **not a control**: any other UA defeats it.

New-format keys are opaque tokens, not JWTs, and must go in the `apikey` header rather than `Authorization: Bearer`. Migrating changes nothing about your RLS posture — the win is revocable, rotatable secret keys.

### R10 — RLS enabled with zero policies is safe; RLS disabled is not

Zero policies = default deny. **RLS not enabled at all** = wide open to anyone with the public key *for whatever the API roles have been granted, in a schema PostgREST exposes*. On a legacy project that is typically everything. These two states look similar in a dashboard and are opposites. Supabase lints them very differently: `0008` INFO vs `0013` **ERROR**.

Projects created before 30 May 2026 do **not** have the safer default of not auto-exposing `public` tables. Verify rather than assume.

### R11 — `TRUNCATE` ignores RLS entirely

RLS governs rows. `TRUNCATE` is a whole-table operation and **no policy applies to it** — a role holding `TRUNCATE` can wipe every tenant's data regardless of how good your isolation is. Never grant it to `anon` or `authenticated`.

```sql
revoke truncate on all tables in schema public from anon, authenticated;
```

Same reasoning applies to `REFERENCES`: foreign-key and unique-constraint checks run outside RLS, so they can reveal whether an invisible row exists.

## Server-side vs browser: the three access patterns

| Pattern | Role | Gate |
|---|---|---|
| Browser, logged out | `anon` | schema exposure + grants, then RLS |
| Browser, logged in — JWT in `Authorization: Bearer` | `authenticated`, claims in `request.jwt.claims`, read by `auth.uid()` | schema exposure + grants, then RLS |
| Server with secret key | `service_role` | grants only — **`BYPASSRLS`, no policy applies** |

Two things to internalise. Browser access is never "RLS only" — a table is reachable only if PostgREST exposes its schema *and* the role holds the privilege; RLS then narrows which rows. And `service_role` bypasses RLS but still needs object privileges: it is not a superuser.

The asymmetry is the point: server-side code can do things no user can, which is why secret keys never touch a browser and why server code must do its own authorization — nothing else will.

## Admins are not a database role

`role = 'admin'` in a table is **not** a Postgres role. Those users connect as `authenticated`, exactly like every customer; `is_admin()` is merely a predicate. So:

- `GRANT` **cannot** distinguish them — grants name roles and take no predicates. Re-granting a column "for admins" grants it to everyone.
- RLS **can** distinguish them, but cannot see columns.

To let admins write columns ordinary users cannot, pick one:

1. **`SECURITY DEFINER` RPC** — clearest for a few fields; needs frontend changes to call it.
2. **`BEFORE UPDATE` trigger guard** — needs *no* frontend change; see `references/trigger-guard-pattern.md`.
3. **Distinct Postgres role via a JWT claim** — cleanest in principle; PostgREST-documented but **Supabase-undocumented**, so no support guarantee on managed hosting.

## Before you revoke anything on a shared table

Process rules, learned the expensive way — each of these caused a production outage.

1. **Enumerate every repo that writes the table.** There is rarely one frontend. An allow-list derived from one client will break the others — this is the single most common cause of self-inflicted outages here.
2. **`git fetch` first, then grep `origin/main`.** A stale checkout produces confident, wrong claims about what is unused.
3. **Allow-list by subtraction, never enumerate guarded columns.** A block-list fails open for every column added later:
   ```sql
   to_jsonb(new) - 'name' - 'email'  is distinct from  to_jsonb(old) - 'name' - 'email'
   ```
4. **Compare values, not presence.** SPAs PATCH whole objects and echo unchanged fields. Use `is distinct from` (NULL-safe, unlike `<>`).
5. **Test from both seats.** Attacker *and* admin. Fixing an escalation while silently breaking staff tooling is the norm, not the exception.
6. **Reproduce the vulnerability before trusting your harness.** A test setup that cannot demonstrate the bug cannot demonstrate the fix.

## Verifying

### 1. Run the auditor — it includes Supabase's own linter

The script runs **two rule sets in one pass**:

**Splinter**, Supabase's own linter (vendored at `vendor/splinter.sql`) — the engine
behind the dashboard's Security Advisor and the `get_advisors` MCP tool. Authoritative,
maintained against the platform, ~29 rules. It covers RLS-disabled tables, `USING (true)`,
definer views, mutable `search_path`, `user_metadata` in policies, exposed materialized
views, browser-callable definer functions, and sensitive-looking column names.

**Plus four rules Splinter does not have:**

| | |
|---|---|
| `R4` | delete-and-reinsert defeating a column-level `UPDATE` revoke |
| `R11` | `TRUNCATE`, which no policy applies to |
| `R1` | policies covering ALL commands, or applying `TO PUBLIC` |
| `R2` | RLS tables with no `RESTRICTIVE` policy pinning tenancy |

Findings from Splinter are prefixed `splinter:`. Pass `--no-splinter` to run only the
four. Earlier versions reimplemented seven Splinter rules by hand; those were removed —
an unmaintained duplicate that is subtly wrong is worse than no check, and two of them
were (the `USING (true)` check missed `1=1` and every whitespace variant).

Resolve the script's path first — a bare relative path resolves against the user's
working directory, not the plugin, and Codex sets no plugin-root variable at all:

```bash
AUDIT="${CLAUDE_PLUGIN_ROOT:-}/scripts/audit_rls.py"
[ -f "$AUDIT" ] || AUDIT=$(
  find ~/.claude/plugins ~/.codex/plugins ~/.agents -name audit_rls.py 2>/dev/null \
    | xargs -r ls -t | head -1
)
[ -f "$AUDIT" ] || echo "audit_rls.py not found — is the plugin installed?"

uv run "$AUDIT" --db-url "$DATABASE_URL"          # read-only
uv run "$AUDIT" --db-url "$DATABASE_URL" --json
uv run "$AUDIT" --db-url "$DATABASE_URL" --schema public --schema api
```

With no `--schema`, it audits whatever PostgREST actually exposes (`pgrst.db_schemas`), not just `public` — a project serving an `api` schema would otherwise be audited on the wrong objects and report a clean bill of health.

**Neither replaces a negative test suite.** Lints check configuration; only tests check reality. Assert 401/403/empty on every table, view and RPC using (a) the publishable key alone and (b) a *second tenant's* JWT.

## References

- `references/trigger-guard-pattern.md` — full column-authorization trigger, with tests
- `references/threat-checklist.md` — pre-merge review checklist
- [Postgres RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) · [CREATE POLICY](https://www.postgresql.org/docs/current/sql-createpolicy.html) · [Privileges](https://www.postgresql.org/docs/current/ddl-priv.html)
- [Supabase: Hardening the Data API](https://supabase.com/docs/guides/database/hardening-data-api) · [Database Advisors](https://supabase.com/docs/guides/database/database-advisors) · [Column Level Security](https://supabase.com/docs/guides/database/postgres/column-level-security)
