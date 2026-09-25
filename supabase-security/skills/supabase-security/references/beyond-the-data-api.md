# Beyond the Data API: Storage, Realtime, Edge Functions

`SKILL.md` treats Postgres as the perimeter, which is true for PostgREST. Three other
Supabase services accept requests carrying the same publishable key, and each has its
own gate. A project with perfect RLS can still expose data through any of them.

Run every check below on a project you control, and preferably on a local
`supabase start` copy first.

## Storage

**How access works.** Objects live in `storage.objects`, and private buckets are
governed by RLS policies on that table. A **public** bucket skips policies for
downloads entirely: `GET /storage/v1/object/public/<bucket>/<path>` returns the file to
anyone. Policies still govern upload, update, delete and listing on public buckets.

**Common mistakes**

| Mistake | Effect |
|---|---|
| Per-user files in a public bucket | anyone with, or able to guess, the path downloads them |
| `for select using (bucket_id = 'docs')` | every signed-in user can list and download every file in the bucket |
| Policy with no `TO` | applies to `anon` as well (R1) |
| Long-lived signed URLs | a signed URL is a bearer token; tightening policies or rotating API keys does not revoke it before it expires |
| Broad `SELECT` added to make upsert work | upsert needs `SELECT` + `UPDATE`; broadening `SELECT` to fix it exposes other users' files |

**The fix**

```sql
-- private bucket, files stored under <user id>/...
update storage.buckets set public = false where id = 'docs';

create policy "own files: read" on storage.objects
  for select to authenticated
  using (bucket_id = 'docs'
         and (storage.foldername(name))[1] = (select auth.uid())::text);

create policy "own files: upload" on storage.objects
  for insert to authenticated
  with check (bucket_id = 'docs'
              and (storage.foldername(name))[1] = (select auth.uid())::text);
```

Every policy pins `bucket_id` **and** an owner or path predicate. Issue signed URLs from
server code only, with the shortest lifetime the feature tolerates.

**Checking it**

```sql
select id, public from storage.buckets;                       -- auditor: R14-public-bucket
select policyname, cmd, roles, qual, with_check
  from pg_policies where schemaname = 'storage' and tablename = 'objects';
```

Then confirm with two test users on a local stack: user B requesting a path under user
A's folder gets an empty list and a 400/404, before and after any policy change.

## Realtime

**How access works.** Three features, two gates.

- `postgres_changes` delivers row changes filtered by the **source table's RLS**, so it
  inherits your table policies. Exception: RLS is **not** applied to `DELETE` events,
  because the deleted row no longer exists to check. The event carries the primary key
  (or the full old row with `REPLICA IDENTITY FULL`).
- **Broadcast** and **Presence** on a *public* channel are open to anyone holding the
  publishable key, for reading and sending.
- *Private* channels are authorized by RLS policies on `realtime.messages`, keyed on
  `realtime.topic()`.

**The fix**

1. Turn off **"Allow public access"** in Realtime settings, so every channel must be
   private. Without this, a client can simply ask for a public channel with the same name.
2. Create channels with `{ config: { private: true } }`.
3. Add topic-scoped policies:

```sql
create policy "members read their room" on realtime.messages
  for select to authenticated
  using (
    realtime.topic() like 'room:%'
    and exists (select 1 from public.room_members m
                 where m.user_id = (select auth.uid())
                   and 'room:' || m.room_id::text = realtime.topic())
  );
```

Use the same predicate for an `insert` policy if members may send.

4. Tables whose deletions are sensitive should not be in the `supabase_realtime`
   publication, or should use the default replica identity so `DELETE` events carry only
   the key.

**Checking it**

```sql
select policyname, cmd, roles from pg_policies
 where schemaname = 'realtime' and tablename = 'messages';
select schemaname, tablename from pg_publication_tables
 where pubname = 'supabase_realtime';
```

The "Allow public access" setting is not in the database; check the dashboard, or the
`[realtime]` section of `supabase/config.toml` for local stacks.

## Edge Functions

**How access works.** With `verify_jwt = true` (the default), the platform checks the
request's credentials before your handler runs. That check does **not** prove a signed-in
user:

- The legacy anon key is itself a valid JWT, so it passes. It proves only that the caller
  has the public key.
- The publishable and secret keys are not JWTs, but the check still accepts them, in the
  `Authorization` header or in `apikey`. Supabase: *"The check alone doesn't authenticate
  a caller that sends only an API key."*
- `verify_jwt = false` is for callers that send no `Authorization` header at all, such as
  webhooks. It makes the function reachable by anyone, so the handler must check a
  shared secret or signature.
- CORS headers constrain browsers only. Any other HTTP client ignores them.

Inside the function, a client created with the secret key has `BYPASSRLS`. If the
handler then reads data based on request parameters, **the function is the only
authorization layer** and must behave like a `SECURITY DEFINER` RPC (R6).

**The fix**

- Default to a client scoped to the caller: forward the caller's `Authorization` header
  so queries run under their RLS. Use the secret-key client only for the specific
  operations that need it, after checking the caller's claims yourself
  (`supabase.auth.getUser(token)` or verifying the JWT against the project's JWKS).
- For webhooks and server-to-server calls with `verify_jwt = false`, check a shared
  secret or signature in the handler before doing anything.
- Never return rows from a secret-key query without filtering them to what the verified
  caller may see.

**Checking it**

- `supabase/config.toml` (or the dashboard) lists each function's `verify_jwt`. Every
  `false` needs an in-handler check.
- Grep function sources for the secret key env var (`SUPABASE_SERVICE_ROLE_KEY` or
  `SUPABASE_SECRET_KEY`) and review every query that client runs.
- On a local stack, call the function with only the publishable key and with a second
  user's token: both should get 401/403 or only their own data.

## Sources

- Storage: [access control](https://supabase.com/docs/guides/storage/security/access-control) · [bucket fundamentals](https://supabase.com/docs/guides/storage/buckets/fundamentals) · [downloads and signed URLs](https://supabase.com/docs/guides/storage/serving/downloads)
- Realtime: [authorization](https://supabase.com/docs/guides/realtime/authorization) · [postgres changes](https://supabase.com/docs/guides/realtime/postgres-changes)
- Edge Functions: [auth](https://supabase.com/docs/guides/functions/auth) · [authorization headers](https://supabase.com/docs/guides/functions/auth-headers)
