---
name: supabase-security
description: "Access control for Supabase projects where a browser talks straight to PostgREST with no API middle layer. Load this skill BEFORE writing or reviewing anything that decides who can read or write data: RLS policies, GRANT/REVOKE statements, SECURITY DEFINER functions, RPCs, views, triggers used as authorization, migrations that touch permissions, custom JWT claims, or code that handles an anon/publishable key or a service_role/secret key. Also load it when diagnosing 'permission denied for table', a 403 or empty result that should have returned rows, when a user can see or change data belonging to another tenant, or when asked to audit a Supabase project for privilege escalation. Triggers on: RLS, row level security, Supabase policy, anon key, service_role, privilege escalation, multi-tenant isolation, column level security, PostgREST."
license: MIT
---

# Supabase Access Control

With no API between the browser and the database, **Postgres is the entire security perimeter**. There is nowhere else to put a check a determined client cannot route around. Every rule here follows from that.

## The one-paragraph model

> The **anon key** says *which project*. The **JWT** says *which user*. Neither says *what you may do* — that is `GRANT` and RLS, and only `GRANT` and RLS.

Teams get breached by believing the anon key is a credential. It is not. It is public by design and ships in your JS bundle. In [CVE-2025-48757](https://mattpalmer.io/posts/2025/05/CVE-2025-48757/) (CVSS 9.3), 170+ production apps were fully readable *and writable* by anyone holding the public key — not because the key leaked, but because RLS was off. The key was doing exactly what it was designed to do.

## The three enforcement layers, and their order

This ordering causes more confusion than anything else in Supabase. Learn it before writing a policy.

```
1. GRANT      role → table/column    no predicates, no rows    planning time
2. RLS        which ROWS             cannot see columns         per row
3. TRIGGER    which COLUMNS changed  sees OLD, NEW, and role    per row
```

Three consequences, each of which has caused a production incident:

**Each layer only narrows the one above it.** RLS can never grant a privilege the GRANT layer withheld. If you revoke a column, a policy saying "admins may update everything" becomes *unreachable* — the statement aborts at planning time, before any row is fetched. This is Postgres-documented; **Supabase does not document it anywhere.**

**RLS constrains rows, never columns.** A policy is a predicate over a row. It has no vocabulary for "only these columns," and `WITH CHECK` sees only `NEW` — there is no `OLD` in a policy. So *"this column must not change"* is **inexpressible in RLS.** Column control is `GRANT (col_list)`; change-detection is a trigger.

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

So tenant isolation written as a permissive policy can be ORed past by *any* feature policy added later. Write it restrictive so it ANDs:

```sql
create policy tenant_isolation on documents
  as restrictive to authenticated
  using (tenant_id = (select auth.tenant_id()));
```

Caveat: a restrictive policy applies to **every** access. Use it for the invariant that must always hold (tenant), never for a role check like "is admin" — that would lock ordinary users out entirely.

### R3 — State `WITH CHECK` explicitly

If omitted on `ALL`/`UPDATE`, Postgres reuses `USING` as `WITH CHECK`. Usually not what you meant, and it hides intent. Write both.

Note the precise failure: an explicit-but-identical `WITH CHECK` is *equally* vulnerable. The defect is never the reuse itself — it is that the predicate constrains **which row**, not **which columns**.

### R4 — A column `GRANT` is not a control unless INSERT and DELETE are revoked

```sql
revoke update on posts from authenticated;
grant update (title, body) on posts to authenticated;   -- looks safe
```

It is not. `DELETE` is a **whole-row privilege** — it cannot be granted per column. The client deletes the row and re-inserts it with any values it likes. No `UPDATE` occurs, so neither the column grant nor an `UPDATE` trigger ever fires.

```sql
revoke insert, delete on posts from authenticated;   -- required, not optional
```

Undocumented by both Supabase and Postgres, but mechanically certain. **Any `BEFORE UPDATE` trigger guard has the identical hole.**

### R5 — `REVOKE ... FROM PUBLIC` does not lock down a function

Supabase grants `EXECUTE` on new `public`-schema functions **directly** to `anon` and `authenticated`. `REVOKE FROM PUBLIC` strips only Postgres's implicit grant; the direct role grants survive untouched. Name the roles:

```sql
revoke all on function public.f(text) from public, anon, authenticated;
grant execute on function public.f(text) to service_role;
```

**RLS does not apply to functions.** A `SECURITY DEFINER` function in an exposed schema is a `POST /rpc/f` away from any browser. Assume every such function is internet-facing and authorize inside it.

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

Zero policies = default deny. **RLS not enabled at all** = fully open to anyone with the public key. These look similar in a dashboard and are opposites. Supabase lints them very differently: `0008` INFO vs `0013` **ERROR**.

Projects created before 30 May 2026 do **not** have the safer default of not auto-exposing `public` tables. Verify rather than assume.

## Server-side vs browser: the three access patterns

| Pattern | Role | Gate |
|---|---|---|
| Browser, logged out | `anon` | RLS only |
| Browser, logged in — JWT in `Authorization: Bearer` | `authenticated`, claims in `request.jwt.claims`, read by `auth.uid()` | RLS only |
| Server with secret key | `service_role` | **none — bypasses RLS entirely** |

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

Process rules, learned the expensive way. See `references/incident-case-study.md`.

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

Run the auditor in this skill:

```bash
uv run scripts/audit_rls.py --db-url "$DATABASE_URL"          # read-only
uv run scripts/audit_rls.py --db-url "$DATABASE_URL" --json
```

It flags policies missing `FOR`/`TO`, `USING (true)`, tables with RLS disabled, functions executable by `anon`/`authenticated`, views without `security_invoker`, definer functions with a mutable `search_path`, and tables where a column-`UPDATE` revoke is undermined by an INSERT/DELETE grant.

Also run Supabase's own advisors (`get_advisors` via MCP, or the dashboard Security Advisor) — it is a different rule set, not a substitute.

**Neither replaces a negative test suite.** Lints check configuration; only tests check reality. Assert 401/403/empty on every table, view and RPC using (a) the publishable key alone and (b) a *second tenant's* JWT.

## References

- `references/incident-case-study.md` — six real vulnerabilities, how each was proven and fixed
- `references/trigger-guard-pattern.md` — full column-authorization trigger, with tests
- `references/threat-checklist.md` — pre-merge review checklist
- [Postgres RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) · [CREATE POLICY](https://www.postgresql.org/docs/current/sql-createpolicy.html) · [Privileges](https://www.postgresql.org/docs/current/ddl-priv.html)
- [Supabase: Hardening the Data API](https://supabase.com/docs/guides/database/hardening-data-api) · [Database Advisors](https://supabase.com/docs/guides/database/database-advisors) · [Column Level Security](https://supabase.com/docs/guides/database/postgres/column-level-security)
