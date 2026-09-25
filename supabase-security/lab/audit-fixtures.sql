-- Fixtures for audit_rls.py. Each misconfigured object must be flagged by the rule
-- named beside it; each "ok_" object is its correct counterpart and must NOT be.
-- Idempotent: safe to re-run. Local lab only (supabase start --workdir lab).

drop schema if exists audit_fixture cascade;
drop policy if exists "fixture bare" on storage.objects;

create schema audit_fixture;
-- Like any schema PostgREST exposes, the API roles can use it.
grant usage on schema audit_fixture to anon, authenticated;

-- R4: column-level INSERT + DELETE defeats the column-level UPDATE grant.
create table audit_fixture.accounts (id uuid primary key, name text, plan text, is_admin boolean);
revoke all on audit_fixture.accounts from anon, authenticated;
grant select, delete on audit_fixture.accounts to authenticated;
grant insert (id, name, plan, is_admin) on audit_fixture.accounts to authenticated;
grant update (name) on audit_fixture.accounts to authenticated;

-- ok: the same grants, but RLS has no DELETE or INSERT policy, so default deny
-- closes the delete-and-reinsert path.
create table audit_fixture.ok_rls_accounts (id uuid primary key, name text, plan text);
alter table audit_fixture.ok_rls_accounts enable row level security;
revoke all on audit_fixture.ok_rls_accounts from anon, authenticated;
grant select, delete on audit_fixture.ok_rls_accounts to authenticated;
grant insert (id, name, plan) on audit_fixture.ok_rls_accounts to authenticated;
grant update (name) on audit_fixture.ok_rls_accounts to authenticated;
create policy ok_rls_accounts_read on audit_fixture.ok_rls_accounts
  for select to authenticated using (id = (select auth.uid()));

-- ok: same column UPDATE grant, but no DELETE, so the row cannot be replaced.
create table audit_fixture.ok_accounts (id uuid primary key, name text, plan text);
revoke all on audit_fixture.ok_accounts from anon, authenticated;
grant select, insert on audit_fixture.ok_accounts to authenticated;
grant update (name) on audit_fixture.ok_accounts to authenticated;

-- R2-restrictive-reads-only: tenancy is restrictive for SELECT, writes are not.
create table audit_fixture.documents (id uuid primary key, tenant_id uuid, body text);
alter table audit_fixture.documents enable row level security;
revoke all on audit_fixture.documents from anon, authenticated;
grant select, insert, update on audit_fixture.documents to authenticated;
create policy documents_read on audit_fixture.documents for select to authenticated using (true);
create policy documents_tenant on audit_fixture.documents
  as restrictive for select to authenticated using (tenant_id = (select auth.uid()));
create policy documents_write on audit_fixture.documents
  for update to authenticated using (true) with check (true);

-- ok: restrictive FOR ALL covers every command.
create table audit_fixture.ok_documents (id uuid primary key, tenant_id uuid);
alter table audit_fixture.ok_documents enable row level security;
revoke all on audit_fixture.ok_documents from anon, authenticated;
grant select, update on audit_fixture.ok_documents to authenticated;
create policy ok_documents_rw on audit_fixture.ok_documents
  for select to authenticated using (true);
create policy ok_documents_update on audit_fixture.ok_documents
  for update to authenticated using (true) with check (true);
create policy ok_documents_tenant on audit_fixture.ok_documents
  as restrictive for all to authenticated
  using (tenant_id = (select auth.uid())) with check (tenant_id = (select auth.uid()));

-- R13: an Auth hook still executable by an API role.
create function audit_fixture.custom_access_token_hook(event jsonb) returns jsonb
  language sql as 'select event';
revoke all on function audit_fixture.custom_access_token_hook(jsonb) from public, anon, authenticated;
grant execute on function audit_fixture.custom_access_token_hook(jsonb) to supabase_auth_admin, authenticated;

-- ok: hook revoked from the API roles, as Supabase's docs require.
create function audit_fixture.ok_access_token_hook(event jsonb) returns jsonb
  language sql as 'select event';
revoke all on function audit_fixture.ok_access_token_hook(jsonb) from public, anon, authenticated;
grant execute on function audit_fixture.ok_access_token_hook(jsonb) to supabase_auth_admin;

-- R14 and R1 on storage.objects: a public bucket and a bare policy.
-- Upsert, not delete-and-insert: Supabase blocks direct DELETE on storage tables.
insert into storage.buckets (id, name, public)
values ('fixture-public', 'fixture-public', true), ('fixture-private', 'fixture-private', false)
on conflict (id) do update set public = excluded.public;
create policy "fixture bare" on storage.objects using (bucket_id = 'fixture-public');
