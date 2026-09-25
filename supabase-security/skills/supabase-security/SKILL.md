---
name: supabase-security
description: "Access control for Supabase projects where a browser talks straight to PostgREST with no API middle layer. Load BEFORE writing or reviewing anything that decides who can read or write data: RLS policies, GRANT/REVOKE, SECURITY DEFINER functions, RPCs, views, triggers used as authorization, permission migrations, custom JWT claims or Auth hooks, Storage bucket policies, Realtime channel authorization, Edge Functions using a secret key, or code handling an anon/publishable or service_role/secret key. Also load when diagnosing 'permission denied for table', a 403 or empty result that should have rows, one tenant seeing another's data, or when auditing a Supabase project. Bundles Supabase's Splinter linter plus checks it lacks. Triggers on: RLS, row level security, Supabase policy, anon key, service_role, privilege escalation, multi-tenant isolation, column level security, public bucket, realtime authorization, PostgREST, Splinter, Security Advisor."
license: MIT
---

# Supabase Access Control

With no API between the browser and the database, **Postgres is the entire security perimeter**. There is nowhere else to put a check a determined client cannot route around. Every rule here follows from that.

> **Built on Supabase's own linter.** The audit script bundles
> [Splinter](https://github.com/supabase/splinter) — the SQL linter behind the
> dashboard's Security Advisor and the `get_advisors` MCP tool — and adds checks it
> does not have. Splinter is Supabase's work, vendored unmodified at
> `vendor/splinter.sql`; see [Credits](#credits).

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

**Each layer only narrows the one above it.** RLS can never grant a privilege the GRANT layer withheld. If you revoke a column, a policy saying "admins may update everything" becomes *unreachable* — the privilege check rejects the statement with `42501` before any row is considered. Postgres documents this, and Supabase now does too, in its [42501 troubleshooting guide](https://supabase.com/docs/guides/troubleshooting/database-api-42501-errors).

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
  as restrictive for all to authenticated
  using (tenant_id = (select private.tenant_id()))
  with check (tenant_id = (select private.tenant_id()));
```

`private.tenant_id()` is your own helper in a schema PostgREST does not expose. Supabase has refused new functions in the `auth` schema since April 2025, so an `auth.tenant_id()` example will not run on a current project.

**Cover every command the role can run, not just `SELECT`.** A restrictive policy binds only the commands it names. The common shape, a restrictive `FOR SELECT` plus permissive `INSERT`/`UPDATE` policies, lets a user insert a row carrying another tenant's ID or `PATCH` their own row into another tenant, because the proposed row is held only to the permissive `WITH CHECK`. Use `FOR ALL` with both `USING` and `WITH CHECK`, as above, or one restrictive policy per write command. The auditor flags this as `R2-restrictive-reads-only`.

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

Projects created from **30 May 2026** get a safer default that does not auto-expose new objects, and Supabase applies it to **existing projects on 30 October 2026** ([changelog](https://supabase.com/changelog/45329-breaking-change-tables-not-exposed-to-data-and-graphql-api-automatically)). Grants that already exist are kept, so the change stops *future* exposure and fixes nothing retroactively. `service_role` loses its automatic grants too, so server code that relied on them needs explicit grants. Keep the explicit revokes either way; they are harmless when redundant. See R12.

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

A definer function that does neither is a privilege-escalation primitive. Tables are typically owned by `postgres`, so a definer function owned by `postgres` **reads and writes with RLS off**. `FORCE ROW LEVEL SECURITY` does not change that: it binds an ordinary table owner, but `postgres` holds `BYPASSRLS`, and no table setting constrains a `BYPASSRLS` role.

Supabase's guidance is to **never create a definer function in an exposed schema** unless it is meant to be an RPC. Helpers that policies call, like `is_admin()` or `tenant_id()`, belong in a `private` schema PostgREST does not serve. A definer function that *is* a deliberate RPC stays in the exposed schema and must authorize the caller in its body, as above.

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
| service_role / `sb_secret_…` | **server only** | carries `BYPASSRLS` — *no policy can constrain it*, unless the request also carries a user token (below) |

Never in `VITE_*` / `NEXT_PUBLIC_*` — that prefix ships it to the browser. Supabase rejects secret keys sent with a browser `User-Agent`, but that is a safety net for accidents, **not a control**: any other UA defeats it.

New-format keys are opaque tokens, not JWTs, and must go in the `apikey` header rather than `Authorization: Bearer`. Migrating changes nothing about your RLS posture — the win is revocable, rotatable secret keys.

**A secret key does not bypass RLS when a user token rides along.** Supabase: *"A secret key bypasses RLS only when the request carries no user access token."* A server client created with the secret key that forwards the caller's `Authorization: Bearer <user JWT>` runs as that user, under their policies. That is useful for acting on a user's behalf, but it surprises code that expects admin reads. Keep one client per purpose.

### R10 — RLS enabled with zero policies is safe; RLS disabled is not

Zero policies = default deny. **RLS not enabled at all** = wide open to anyone with the public key *for whatever the API roles have been granted, in a schema PostgREST exposes*. On a legacy project that is typically everything. These two states look similar in a dashboard and are opposites. Supabase lints them very differently: `0008` INFO vs `0013` **ERROR**.

Projects created before 30 May 2026 auto-expose new `public` tables until Supabase's 30 October 2026 migration, and keep every grant made before then. Verify rather than assume; the auditor's `R12` shows the default privileges in force.

### R11 — `TRUNCATE` ignores RLS entirely

RLS governs rows. `TRUNCATE` is a whole-table operation and **no policy applies to it**. PostgREST and pg_graphql have no `TRUNCATE` verb, and `anon`/`authenticated` cannot log in, so the privilege is reachable only through a `SECURITY INVOKER` function that truncates, or one that runs dynamic SQL. That makes it defence in depth rather than an open door. Legacy default privileges grant it on every new table, so revoke it:

```sql
revoke truncate on all tables in schema public from anon, authenticated;
```

**Constraints see rows RLS hides.** Primary-key, unique and foreign-key checks run without RLS, so an `INSERT` that fails with a unique violation reveals that an invisible row with that value exists. Revoking `REFERENCES` does nothing about this. Scope unique constraints per tenant (`unique (tenant_id, email)`) and avoid meaningful values in globally unique columns.

### R12 — Enable RLS in the migration that creates the table

On a project that still has the legacy default privileges, every new table in `public` is granted to `anon` and `authenticated` the moment it is created. If the migration creates the table and a later one enables RLS, the table is open in between. Put `alter table ... enable row level security` in the same migration as `create table`, and treat default privileges as part of the attack surface: the auditor's `R12` lists them.

```sql
-- the per-schema grants Supabase's legacy defaults add for the API roles
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated;
-- PostgreSQL's own EXECUTE-for-PUBLIC default on functions is GLOBAL:
-- a per-schema `revoke ... from public` has no effect on it
alter default privileges revoke execute on functions from public;
```

Default privileges belong to the role that creates the objects. The statements above apply to objects the current role creates; repeat them with `for role <name>` for every other role that creates objects in exposed schemas (that requires membership in the role). Existing objects keep their grants, so this protects only what comes next.

### R13 — `authenticated` is not "trusted"

Anyone can become `authenticated` when signup is open, and **anonymous sign-ins also arrive as `authenticated`**. With email confirmation off, the email claim is unverified. So:

- Every `to authenticated` policy needs an ownership or membership predicate. `to authenticated using (true)` means "anyone who signs up", and Splinter's always-true lint does not flag it for `SELECT`.
- If anonymous users must be excluded, add a restrictive policy: `using ((select (auth.jwt() ->> 'is_anonymous')::boolean) is false)`.
- Never authorize on the `email` claim or its domain.
- A **custom access token hook** must be revoked from the API roles (`revoke execute ... from authenticated, anon, public`). Otherwise it is an RPC that returns the claims it would issue for any user ID you pass it. The auditor flags likely hooks as `R13-auth-hook-callable`. Never build the hook's claims from data the user can write.
- Claims are fixed until the token refreshes. Demoting an admin in `app_metadata` leaves their current token working. For permissions that must revoke immediately, look them up in a table inside the policy.

### R14 — Storage, Realtime and Edge Functions have their own gates

PostgREST is not the only door. Each of these needs its own check; details and verification steps are in `references/beyond-the-data-api.md`.

- **Storage.** A **public bucket serves every object to anyone with its URL; no policy is consulted on download.** Per-user files go in a private bucket with policies that pin `bucket_id` *and* an owner or path predicate. Signed URLs are bearer tokens that outlive any policy change. The auditor lists public buckets (`R14-public-bucket`).
- **Realtime.** Broadcast and Presence are open on public channels to anyone with the publishable key. Private channels need policies on `realtime.messages` **and** "Allow public access" turned off in Realtime settings. `postgres_changes` applies the table's RLS, but not to `DELETE` events.
- **Edge Functions.** `verify_jwt` does not prove a signed-in user: it accepts the legacy anon key (itself a JWT) and also the publishable and secret keys. A function that uses a secret-key client bypasses every policy, so it must authorize the caller in its handler. CORS does not restrict non-browser clients.

### R15 — `UPDATE`, upsert and `RETURNING` need a `SELECT` policy

An `UPDATE` through the API with no matching `SELECT` policy silently changes zero rows. `Prefer: return=representation` and upserts (`ON CONFLICT DO UPDATE`) must also pass `SELECT` policies, and fail with `42501` when they don't. The tempting fix, `for select using (true)`, turns a write bug into a data leak. Add a `SELECT` policy scoped exactly like the write policy instead.

### R16 — RLS is per row; sensitive columns need their own boundary

A user allowed to read a row reads **every column** of it. A `profiles` table readable by teammates leaks email, phone and billing IDs along with the display name. Splinter's sensitive-columns lint only fires when RLS is off. Revoke the table-level `SELECT` and grant only the safe columns, or split the table into a public part and a private part with a stricter policy. Column grants are enforced before RLS (see the layer order above).

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

**Plus rules Splinter does not have:**

| | |
|---|---|
| `R4` | delete-and-reinsert defeating a column-level `UPDATE` revoke, including column-level `INSERT` |
| `R13` | an Auth hook left executable by `anon`/`authenticated` in an exposed schema |
| `R1` | permissive policies covering ALL commands, or policies applying `TO PUBLIC`, including on `storage.objects` and `realtime.messages` |
| `R2` | RLS tables with no `RESTRICTIVE` policy, or one that covers reads but not writes |
| `R11` | `TRUNCATE`, which no policy applies to |
| `R12` | default privileges that expose every future table or function |
| `R14` | public Storage buckets |

Findings from Splinter are prefixed `splinter:`. Pass `--no-splinter` to run only this
skill's own rules. Earlier versions reimplemented seven Splinter rules by hand; those were removed —
an unmaintained duplicate that is subtly wrong is worse than no check, and two of them
were (the `USING (true)` check missed `1=1` and every whitespace variant).

Resolve the script's path first — a bare relative path resolves against the user's
working directory, not the plugin. Codex sets no plugin-root variable at all, and
opencode installs packages under `~/.cache/opencode/packages`, so the fallback
searches every harness's install location:

```bash
AUDIT="${CLAUDE_PLUGIN_ROOT:-}/scripts/audit_rls.py"
[ -f "$AUDIT" ] || AUDIT=$(
  find ~/.claude/plugins ~/.codex/plugins ~/.agents ~/.cache/opencode \
    -name audit_rls.py 2>/dev/null | xargs -r ls -t | head -1
)
[ -f "$AUDIT" ] || echo "audit_rls.py not found — is the plugin installed?"

uv run "$AUDIT" --db-url "$DATABASE_URL"          # read-only
uv run "$AUDIT" --db-url "$DATABASE_URL" --json
uv run "$AUDIT" --db-url "$DATABASE_URL" --schema public --schema api
```

With no `--schema`, it reads the exposed schemas from the `pgrst.db_schemas` setting on the `authenticator` role. When that setting is absent, which is normal for the local CLI and for projects configured from the dashboard, it **exits 2 and asks for `--schema`** rather than guessing. An earlier version silently fell back to `public`, so a project serving an `api` schema was audited on the wrong objects and got a clean report. Take the list from Dashboard → Data API → Exposed schemas, or `[api].schemas` in `supabase/config.toml`.

**Audit only projects you control.** The auditor needs the project's database URL, including the `postgres` password, which only the project's owners hold. Treat that as the proof of ownership: if you do not have it, you are not authorized to audit the project. The auditor is read-only either way.

**Neither replaces a negative test suite.** Lints check configuration; only tests check reality. Assert 401/403/empty on every table, view and RPC using (a) the publishable key alone and (b) a *second tenant's* JWT.

## Credits

**[Splinter](https://github.com/supabase/splinter) is Supabase's, not ours.** It is a
pure-SQL linter — ~29 rules, one `.sql` file each, compiled into a single
self-contained query that upstream publishes at its repo root explicitly for
project linting. It powers the dashboard's Security Advisor and the `get_advisors`
MCP tool.

It is vendored here **unmodified** at `vendor/splinter.sql`, pinned to the commit
recorded in `vendor/SPLINTER_VERSION`. Vendoring rather than fetching keeps the audit
offline-capable and reproducible; fetching at runtime would make findings depend on
whatever upstream happened to be that morning.

What this skill adds around it:

| | |
|---|---|
| Rules Splinter lacks | `R1`, `R2`, `R4`, `R11`, `R12`, `R13`, `R14` (see the table above) |
| `pgrst.db_schemas` is set first | from the `authenticator` role setting or `--schema`; without it, several of Splinter's API-exposure lints silently fall back to `public` only — upstream's README warns about this |
| One read-only transaction | server-enforced, not merely asserted |
| Filtering and formatting | `EXTERNAL` + `SECURITY` findings only, `--json` for CI, `--no-splinter` to skip |

You can run Splinter without any of this — `psql -f vendor/splinter.sql "$DATABASE_URL"`
works on its own. Nothing here replaces or forks it.

**Licence note:** Splinter has no LICENSE file. It is a public Supabase repository
distributed for exactly this use, but there is no explicit grant. It is kept unmodified
and attributed in its own directory so it can be removed cleanly. Confirm the position
with Supabase before publishing this skill more widely. See `vendor/README.md`.

## References

- `references/trigger-guard-pattern.md` — full column-authorization trigger, with tests
- `references/threat-checklist.md` — pre-merge review checklist
- `references/beyond-the-data-api.md` — Storage, Realtime and Edge Functions: configuration, fixes, verification
- [Splinter](https://github.com/supabase/splinter) — Supabase's linter, bundled here · [its rule docs](https://supabase.com/docs/guides/database/database-advisors)
- [Postgres RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) · [CREATE POLICY](https://www.postgresql.org/docs/current/sql-createpolicy.html) · [Privileges](https://www.postgresql.org/docs/current/ddl-priv.html)
- [Supabase: Hardening the Data API](https://supabase.com/docs/guides/database/hardening-data-api) · [Column Level Security](https://supabase.com/docs/guides/database/postgres/column-level-security)
