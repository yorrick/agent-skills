\set user_claims '{"sub":"00000000-0000-0000-0000-00000000000a","role":"authenticated"}'
\set admin_claims '{"sub":"00000000-0000-0000-0000-00000000000b","role":"authenticated"}'

\echo CASE 1 user changes allowed column (expect UPDATE 1)
begin; set local role authenticated; select set_config('request.jwt.claims', :'user_claims', true) \g /dev/null
update public.account set name = 'renamed' where id = '00000000-0000-0000-0000-00000000000a';
rollback;

\echo CASE 2 user echoes guarded columns unchanged (expect UPDATE 1)
begin; set local role authenticated; select set_config('request.jwt.claims', :'user_claims', true) \g /dev/null
update public.account set name = 'x', plan = 'free', is_admin = false where id = '00000000-0000-0000-0000-00000000000a';
rollback;

\echo CASE 3 user changes guarded column (expect error naming plan)
begin; set local role authenticated; select set_config('request.jwt.claims', :'user_claims', true) \g /dev/null
update public.account set plan = 'pro' where id = '00000000-0000-0000-0000-00000000000a';
rollback;

\echo CASE 4 user smuggles guarded column with allowed one (expect error)
begin; set local role authenticated; select set_config('request.jwt.claims', :'user_claims', true) \g /dev/null
update public.account set name = 'y', is_admin = true where id = '00000000-0000-0000-0000-00000000000a';
rollback;

\echo CASE 5 user targets another row (expect UPDATE 0)
begin; set local role authenticated; select set_config('request.jwt.claims', :'user_claims', true) \g /dev/null
update public.account set name = 'z' where id = '00000000-0000-0000-0000-00000000000b';
rollback;

\echo CASE 6 user delete then insert (expect permission denied)
begin; set local role authenticated; select set_config('request.jwt.claims', :'user_claims', true) \g /dev/null
delete from public.account where id = '00000000-0000-0000-0000-00000000000a';
rollback;

\echo CASE 7 admin changes guarded columns (expect UPDATE 1)
begin; set local role authenticated; select set_config('request.jwt.claims', :'admin_claims', true) \g /dev/null
update public.account set plan = 'enterprise', is_admin = true where id = '00000000-0000-0000-0000-00000000000a';
rollback;

\echo CASE 8 service_role writes (expect UPDATE 1)
begin; set local role service_role;
update public.account set plan = 'enterprise' where id = '00000000-0000-0000-0000-00000000000a';
rollback;

\echo CASE 9 postgres job writes (expect UPDATE 1)
begin;
update public.account set plan = 'enterprise' where id = '00000000-0000-0000-0000-00000000000a';
rollback;

\echo CASE 10 anon writes (expect permission denied)
begin; set local role anon;
update public.account set name = 'anon' where id = '00000000-0000-0000-0000-00000000000a';
rollback;

\echo CASE 11 column added after migration: user blocked, admin allowed
begin;
alter table public.account add column credits int default 0;
set local role authenticated; select set_config('request.jwt.claims', :'user_claims', true) \g /dev/null
savepoint s; update public.account set credits = 999 where id = '00000000-0000-0000-0000-00000000000a'; rollback to savepoint s;
select set_config('request.jwt.claims', :'admin_claims', true) \g /dev/null
update public.account set credits = 999 where id = '00000000-0000-0000-0000-00000000000a';
rollback;
