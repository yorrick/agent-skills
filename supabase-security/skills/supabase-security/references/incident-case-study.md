# Case study: six vulnerabilities, fourteen migrations, two self-inflicted outages

A real incident on a production multi-tenant Supabase project (hotel SaaS, ~170 tenant
accounts, browser → PostgREST, no API layer). Every item was **verified against
production** — either by exploiting it or by breaking something with the fix.

Included because the *sequence* teaches more than any individual rule: several fixes
were themselves defective, and the failure modes were systematic.

## The six vulnerabilities

| # | Defect | Proof | Root cause |
|---|---|---|---|
| 1 | `read_secret(uuid)` — no authorization whatsoever. Any logged-in user could read **any** tenant's third-party API credentials | called it with another tenant's UUID → plaintext returned | `SECURITY DEFINER` + default `EXECUTE` to `authenticated` |
| 2 | `hotel_user.role` self-escalation to admin | `PATCH` own row `{"role":"aura_admin"}` → 204 | policy with no `FOR` clause (R1) |
| 3 | Same escalation via `DELETE` + re-`INSERT` **after** the column fix | `DELETE` own row → 204; `INSERT` own id with admin role → 201 | R4 |
| 4 | Tenant could repoint `onboarding_data.pms_api_key_id` at another tenant's vault secret | — | RLS constrains rows, not columns (R-model) |
| 5 | `upsert_secret` / `create_secret` / `delete_secret` — authorization-free Vault writers on the Data API | an unprivileged JWT **actually created a real vault secret** | R5 |
| 6 | Tenants could set their own billing/entitlement flags | `PATCH {"test_account":true,"stripe_payment_activated":true}` → 204, both flipped | policy with no `FOR` clause (R1) |

Note #2 and #6 share a root cause with #4: **one** policy, written years earlier and
named `"Users can view their related account"`, missing `FOR SELECT`. One omission,
three vulnerabilities.

## The vault pattern that made #1 and #4 severe

Third-party API keys lived in `vault.secrets`, referenced by UUID from a
**user-writable** column. Two independent problems compounded:

- A definer function resolved UUID → plaintext with no ownership check (#1).
- The referencing column was user-writable, so a tenant could point their row at
  another tenant's secret (#4).

Even a resolver that never *returns* the plaintext is an exfiltration oracle — it will
*use* the victim's key to call the third-party API on the attacker's behalf.

**Rule:** the resolver must re-verify that the caller owns *the secret*, not merely
that it owns the row holding the reference. This pattern is entirely undocumented by
Supabase; treat it as your own design problem.

## Self-inflicted outage #1 — the fix broke every admin

Fixing #2 and #6 meant revoking table-level `UPDATE` and re-granting specific columns.
Result: **admins were locked out too.**

```
PATCH /rest/v1/aura_account {"test_account":true}   →  403  42501
```

Why: column privileges are checked **before** RLS, and `aura_admin` is not a database
role — admins connect as `authenticated` like everyone else. The existing
`"Admins can update all accounts"` policy became unreachable: the statement aborted at
planning time, before any row was fetched.

The instinct "just re-grant the column and check `is_admin()` in RLS" **does not work**
— permissive policies are ORed, so the tenant's own policy returns true for its own row
and the update is allowed. See R2.

## Self-inflicted outage #2 — the same mistake, a second time

The column allow-list was derived by auditing **one** frontend. A **second** frontend —
the internal admin dashboard — wrote four other columns and broke completely. Reported
by a screenshot of a red toast: *"permission denied for table aura_account."*

Two compounding errors:

1. **Only one client repo was audited.** There is rarely "the frontend."
2. **A stale checkout.** The audit grepped a six-week-old working tree, which led to a
   confident claim that a column was unused. It was in fact the portal's entitlement
   gate, added nine days earlier. `git fetch` and grep `origin/main`.

## What finally worked

Restore the table grant so the privilege check passes, and move column authorization
into a `BEFORE UPDATE` trigger — the only place `OLD`, `NEW` and `is_admin()` are all
visible. See `trigger-guard-pattern.md`.

Critically, the guard is an **allow-list by subtraction**: it subtracts the editable
columns from `to_jsonb(row)` rather than enumerating guarded ones. Enumeration is what
caused outage #2 — a column added nine days earlier was simply absent from the list.
With subtraction, columns added by future migrations are protected by default.

## Cost

Fourteen migrations over two days. Roughly eight were genuine security fixes; the rest
repaired over-corrections. **That ratio is the argument for writing rules down.**

## Transferable lessons

1. One policy missing `FOR SELECT` produced three separate vulnerabilities. Audit every
   policy for it *first* — it is the highest-yield check available.
2. Every column-privilege fix must be tested from the **admin's** seat as well as the
   attacker's.
3. Enumerate every client repo before revoking on a shared table, and confirm each
   checkout is current.
4. Prefer allow-list-by-subtraction over enumeration, so the safe default survives
   future schema changes.
5. Reproduce the vulnerability in your test harness *before* trusting it to prove a fix.
6. `DELETE` + `INSERT` defeats both column grants and `UPDATE` triggers. Revoke them.
