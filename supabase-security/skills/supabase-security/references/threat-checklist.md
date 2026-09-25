# Pre-merge checklist: any migration that touches access control

Run through this before approving a migration that creates or alters a policy,
grant, function, view, or trigger. Each line maps to a rule in `SKILL.md`.

## Policies

- [ ] Every `CREATE POLICY` names a command: `FOR SELECT` / `FOR INSERT` / `FOR UPDATE` / `FOR DELETE`. **No bare policies.** (R1)
- [ ] Every policy names its roles: `TO authenticated` / `TO anon`. (R1)
- [ ] `WITH CHECK` stated explicitly on `ALL` and `UPDATE` policies. (R3)
- [ ] Tenant isolation is **RESTRICTIVE** — *and* a permissive policy exists to grant access at all, or the table is default-deny. (R2)
- [ ] Any restrictive policy exempts staff/admins if they need cross-tenant access, since it constrains them too. (R2)
- [ ] No policy reads `user_metadata` for authorization. (R8)
- [ ] Auth calls wrapped: `(select auth.uid())`, not bare `auth.uid()`.
- [ ] The table has RLS **enabled**, not merely policies defined. (R10)
- [ ] Tenant isolation covers **writes** too: restrictive `FOR ALL` (or per write command) with `WITH CHECK`, not just `FOR SELECT`. (R2)
- [ ] No `to authenticated using (true)` on user data. Anyone who signs up is `authenticated`, including anonymous sign-ins. (R13)
- [ ] Anonymous sign-ins considered: they arrive as `authenticated`. Check the `is_anonymous` claim if that matters. (R13)
- [ ] No authorization on the `email` claim. (R13)
- [ ] Every `UPDATE`/upsert/`return=representation` path has a `SELECT` policy scoped like the write, not `using (true)`. (R15)
- [ ] Rows readable by other users contain no sensitive columns, or those columns are revoked from `SELECT`. (R16)

## Grants

- [ ] `REVOKE` precedes any column `GRANT` — privileges are additive, and a table grant implies every column.
- [ ] Column `UPDATE` grants are paired with a revoke of `INSERT` **or** `DELETE` — holding both defeats the boundary. (R4)
- [ ] `TRUNCATE` is not granted to `anon`/`authenticated`. No policy applies to it. (R11)
- [ ] The migration that creates a table also enables RLS on it, and default privileges were checked. (R12)
- [ ] Unique constraints on user data are scoped per tenant; a global unique check reveals rows RLS hides. (R11)
- [ ] `anon` explicitly revoked where it should not write.
- [ ] Any admin path does **not** depend on re-granting a column to `authenticated` — that grants it to every user. (R2, "Admins are not a database role")

## Functions and RPCs

- [ ] `REVOKE` names `anon` and `authenticated` explicitly, not just `PUBLIC`. (R5)
- [ ] Re-check after any `DROP FUNCTION` + `CREATE` — the default grant comes back.
- [ ] `SECURITY DEFINER` functions pin `search_path`. (R6)
- [ ] `SECURITY DEFINER` functions authorize the caller **in the body**. They run as their owner, usually `postgres`, which bypasses RLS. (`SECURITY INVOKER` functions still run under the caller's policies.) (R5, R6)
- [ ] Helper functions called from policies (`is_admin()`, `tenant_id()`) live in a schema PostgREST does not expose. (R6)
- [ ] Auth hooks are revoked from `anon`, `authenticated` and `public`, and never build claims from user-writable data. (R13)
- [ ] The function is not a privileged primitive reachable from a browser (`POST /rpc/...`).

## Views

- [ ] `WITH (security_invoker = on)`. (R7)
- [ ] Grants restated after `DROP VIEW` + `CREATE VIEW`.
- [ ] No materialized views or foreign tables in an exposed schema — they cannot enforce RLS.

## Storage, Realtime, Edge Functions

See `beyond-the-data-api.md`.

- [ ] No per-user files in a public bucket. (R14)
- [ ] Every `storage.objects` policy pins `bucket_id` **and** an owner or path predicate, and names its role. (R1, R14)
- [ ] Signed URLs are issued server-side with short lifetimes. (R14)
- [ ] Realtime "Allow public access" is off if any channel carries private data, and `realtime.messages` has topic-scoped policies. (R14)
- [ ] Tables whose primary keys are sensitive are not in the `supabase_realtime` publication; `DELETE` events skip RLS. (R14)
- [ ] Every Edge Function with `verify_jwt = false`, or that uses the secret key, authorizes the caller in its handler. (R14)

## Vault / secrets

- [ ] Columns holding secret UUIDs are not user-writable (and the delete-and-reinsert path is closed).
- [ ] Any resolver function verifies the caller owns **the secret**, not just the row referencing it.
- [ ] Nothing logs, returns, or serialises secret material — including lengths and hashes.

## Blast radius — the step most often skipped

- [ ] **Every** repo with a Supabase client pointed at this project has been enumerated and grepped for writes to the affected tables. Not just the obvious frontend.
- [ ] Each of those checkouts was `git fetch`ed and grepped against `origin/main`, not a stale working tree.
- [ ] Server-side callers (`service_role`) confirmed unaffected, or explicitly exempted.

## Verification — before merge, not after

- [ ] The vulnerability was **reproduced** first, so the harness is known to be faithful.
- [ ] Tested as an ordinary user: the legitimate write still works (including a no-op PATCH that echoes unchanged fields).
- [ ] Tested as an ordinary user: the escalation is blocked.
- [ ] Tested as an **admin**: the admin path still works. *This is the one that gets skipped and causes the outage.*
- [ ] Tested as `service_role`: unaffected.
- [ ] Tested as `anon`: blocked.
- [ ] Cross-tenant attempt returns zero rows, not an error that leaks existence.
- [ ] Supabase advisors and `audit_rls.py` re-run after the migration.

## Rollback

- [ ] The migration has a stated rollback, and you know whether rolling back re-opens a vulnerability or merely restores a broken UI.
