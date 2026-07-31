# Pattern: column authorization via a BEFORE UPDATE trigger

Use when **admins must write columns ordinary users cannot**, and you cannot (or do
not want to) change the frontend.

A `GRANT` cannot express "only when `is_admin()`" — grants name roles and take no
predicates. RLS cannot express "this column must not change" — policies see only `NEW`.
A `BEFORE UPDATE` trigger is the first point where `OLD`, `NEW` and the caller's
identity are all available.

## Where it sits

```
1. GRANT check     role only, no predicates      → passes (restore the table grant)
2. RLS             confines to permitted rows    → unchanged
3. BEFORE trigger  OLD vs NEW + is_admin()       → the real gate
```

RLS still does real work: it stops a user reaching *another tenant's* row, so the
trigger only has to prevent **self**-escalation. That keeps it small.

## The migration

```sql
create or replace function public.guard_account_admin_columns()
returns trigger
    language plpgsql security definer
    set search_path to 'pg_catalog', 'public', 'pg_temp'
    as $$
declare
  changed text;
begin
  -- service_role runs provisioning/billing and is not subject to RLS.
  if (select auth.role()) = 'service_role' or public.is_admin() then
    return new;
  end if;

  -- ALLOW-LIST BY SUBTRACTION. Enumerating the *guarded* columns fails open for
  -- every column a future migration adds -- which is exactly how this class of
  -- bug recurs. Subtracting the editable ones means new columns are admin-only
  -- by default, with no list to keep in sync.
  --
  -- Compare VALUES, not presence: SPAs PATCH whole objects and echo unchanged
  -- fields. jsonb equality is also NULL-safe, unlike <>.
  if (to_jsonb(new) - 'name' - 'locale' - 'timezone')
     is distinct from
     (to_jsonb(old) - 'name' - 'locale' - 'timezone')
  then
    -- Name the offending column so an operator can tell a real permission
    -- problem from a client bug. Column NAMES only, never values -- this table
    -- may hold payment identifiers.
    select string_agg(key, ', ' order by key) into changed
      from jsonb_each(to_jsonb(new)) n
     where n.key not in ('name', 'locale', 'timezone')
       and n.value is distinct from (to_jsonb(old) -> n.key);

    raise exception 'Only admins may change column(s): %', changed
      using errcode = 'insufficient_privilege';
  end if;

  return new;
end;
$$;

drop trigger if exists guard_account_admin_columns on public.account;
create trigger guard_account_admin_columns
    before update on public.account
    for each row
    execute function public.guard_account_admin_columns();

-- Restore the table grant: the trigger is now the authorization boundary, so the
-- allow-list lives in ONE place instead of being split between a GRANT and a
-- guard that must be kept in sync.
grant update on public.account to authenticated;

-- LOAD-BEARING. A BEFORE UPDATE trigger never fires for DELETE + INSERT, so a
-- user able to replace the row wholesale bypasses the guard entirely.
revoke insert, delete on public.account from authenticated;
revoke insert, update, delete on public.account from anon;
```

## Tradeoffs

**Good:** no frontend change; one place to maintain; new columns safe by default.

**Watch out:**
- Useless without the `INSERT`/`DELETE` revoke. Not optional.
- `to_jsonb` on very wide rows costs more than comparing named columns. Fine for
  config tables; measure before using on a hot path.
- Runs per row — a bulk admin update evaluates `is_admin()` per row. Acceptable for
  admin tooling; not for bulk data pipelines (which should use `service_role` anyway).
- The trigger is invisible in the SPA's error until it fires. Surface the message.

## Verifying it

Do **not** ship this untested. Build a local Postgres replica with the same table,
roles and policies, and **first reproduce the original vulnerability** — a harness that
cannot demonstrate the bug cannot demonstrate the fix.

Minimum cases, all of which must pass:

| Case | Expected |
|---|---|
| user changes an allowed column | allowed |
| user echoes guarded columns **unchanged** | allowed (no-op PATCH must keep working) |
| user changes a guarded column | blocked, column named |
| user smuggles a guarded column alongside an allowed one | blocked |
| user targets another tenant's row | 0 rows (RLS, before the trigger) |
| user attempts DELETE then INSERT | blocked at the privilege layer |
| admin changes every guarded column | allowed |
| `service_role` writes | allowed |
| `anon` writes | blocked |
| **a column added *after* the migration** | **blocked for users, allowed for admins** |

That last row is the one that proves the subtraction approach works. Test it by
`ALTER TABLE ... ADD COLUMN` in the harness after applying the guard.
