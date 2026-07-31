# Pattern: column authorization via a BEFORE UPDATE trigger

Use when **admins must write columns ordinary users cannot**, and you cannot (or do
not want to) change the frontend.

> **Do not use this pattern on a table with GENERATED columns.** Postgres computes them
> *after* `BEFORE` triggers, so `to_jsonb(NEW)` reads a stale or undefined value and the
> guard may error or misfire. Such a table needs an explicit column list instead, tested
> against the generated column specifically.
>
> **Check for other `BEFORE UPDATE` triggers first.** Postgres fires same-event triggers
> in **alphabetical order by trigger name**, not creation order. If an `updated_at`
> trigger sorts before this guard, it mutates a guarded column and the guard then rejects
> every ordinary update. Either fold the mutation into this trigger, or name the triggers
> so the ordering is deliberate — and test it. Note the converse risk too: if the guard
> runs first, a later trigger can still modify a guarded column after authorization.

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
    -- SECURITY INVOKER (the default) is deliberate: it keeps current_user as the
    -- calling role, which is what the service_role check below relies on. A
    -- DEFINER function would report the owner instead. Row comparison needs no
    -- elevated privilege; if is_admin() needs privileged reads, isolate that in
    -- its own small definer helper.
    language plpgsql
    set search_path to 'pg_catalog', 'public', 'pg_temp'
    as $$
declare
  editable constant text[] := array['name', 'locale', 'timezone'];
  new_j    jsonb;
  old_j    jsonb;
  changed  text;
begin
  -- Reason: current_user is the role that actually queued the statement, and it
  -- cannot be spoofed by a request claim. auth.role() reads the request JWT and
  -- is deprecated by Supabase; it is also NULL for direct database jobs (cron,
  -- psql, migrations), which would then be wrongly subjected to the guard.
  -- This works because the function is SECURITY INVOKER (the default).
  if current_user = 'service_role' or public.is_admin() then
    return new;
  end if;

  -- Reason: serialize once. The error path below would otherwise repeat this on
  -- every row, and cost scales with serialized row width.
  new_j := to_jsonb(new) - editable;
  old_j := to_jsonb(old) - editable;

  -- ALLOW-LIST BY SUBTRACTION. Enumerating the *guarded* columns fails open for
  -- every column a future migration adds -- which is exactly how this class of
  -- bug recurs. Subtracting the editable ones means new columns are admin-only
  -- by default, with no list to keep in sync.
  --
  -- Compare VALUES, not presence: SPAs PATCH whole objects and echo unchanged
  -- fields. jsonb equality is also NULL-safe, unlike <>.
  if new_j is distinct from old_j then
    -- Name the offending column so an operator can tell a real permission
    -- problem from a client bug. Column NAMES only, never values -- this table
    -- may hold payment identifiers.
    select string_agg(n.key, ', ' order by n.key) into changed
      from jsonb_each(new_j) n
     where n.value is distinct from (old_j -> n.key);

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
-- user who can replace the row wholesale bypasses the guard entirely. Revoking
-- EITHER one closes the path (the attack needs both); revoke both if the client
-- needs neither, which is the common case for a config table.
revoke insert, delete on public.account from authenticated;
revoke insert, update, delete on public.account from anon;

-- RLS is unaffected and still required: it is what stops a user reaching another
-- tenant's row at all. The trigger only prevents self-escalation on rows the
-- policy already lets them touch.
```

## Tradeoffs

**Good:** no frontend change; one place to maintain; new columns safe by default.

**Watch out:**
- Useless unless `INSERT` or `DELETE` is revoked (see above). Not optional.
- **Not safe on tables with generated columns** — see the warning at the top.
- **Trigger firing order is alphabetical by name** and can break this — see above.
- `to_jsonb` cost scales with serialized row width, not row count alone. Wide
  `text`/`jsonb`/array/TOAST columns are where it bites. Fine for narrow config
  tables at hundreds of writes/sec; measure before a hot path or bulk updates.
- Conversion to jsonb is not injective for every type. `json` columns are
  normalized (whitespace, key order, duplicate keys lost) and arrays lose
  lower-bound metadata — possible **false negatives**. `citext` goes the other
  way: a case-only change compares as different, which is conservative but can
  surprise.
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
