drop table if exists public.account cascade;
create schema if not exists private;
create table public.account (id uuid primary key, name text, locale text, timezone text, plan text, is_admin boolean default false);
alter table public.account enable row level security;
create or replace function private.is_admin() returns boolean language sql stable security definer set search_path = '' as $$
  select coalesce((select a.is_admin from public.account a where a.id = (select auth.uid())), false) $$;
grant usage on schema private to authenticated, anon;
create policy account_select on public.account for select to authenticated using (id = (select auth.uid()) or (select private.is_admin()));
create policy account_update on public.account for update to authenticated using (id = (select auth.uid()) or (select private.is_admin())) with check (id = (select auth.uid()) or (select private.is_admin()));
insert into public.account (id, name, plan) values ('00000000-0000-0000-0000-00000000000a', 'user', 'free'), ('00000000-0000-0000-0000-00000000000b', 'admin', 'pro');
update public.account set is_admin = true where name = 'admin';
-- A custom role PostgREST could switch to from a JWT `role` claim. The guard must
-- treat it like any other untrusted caller.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'lab_custom_api') then
    create role lab_custom_api nologin;
  end if;
end $$;
grant lab_custom_api to postgres;
grant select, update on public.account to lab_custom_api;
grant usage on schema private to lab_custom_api;
grant execute on function private.is_admin() to lab_custom_api;
create policy account_custom on public.account for all to lab_custom_api using (true) with check (true);
