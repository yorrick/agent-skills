#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.1", "typer>=0.12"]
# ///
"""Audit a Supabase/Postgres database for access-control defects.

READ-ONLY. Every query reads catalogue tables; nothing is written, and the
connection is opened in a read-only transaction as a belt-and-braces guard.

Runs TWO rule sets in one pass:

1. SPLINTER -- Supabase's own linter (vendor/splinter.sql), the engine behind the
   dashboard's Security Advisor and the `get_advisors` MCP tool. Authoritative,
   maintained against the platform, ~29 rules. Running it here means one command
   gives complete coverage instead of relying on someone remembering a second
   tool, which is exactly what does not happen.

2. FOUR RULES SPLINTER DOES NOT HAVE:

       R4   delete-and-reinsert defeating a column-level UPDATE revoke
       R11  TRUNCATE, which no RLS policy applies to
       R1   policies covering ALL commands, or applying TO PUBLIC
       R2   RLS tables with no RESTRICTIVE policy pinning tenancy

An earlier version reimplemented seven Splinter rules by hand. Those were
removed: an unmaintained duplicate that is subtly wrong is worse than no check,
and two of them were -- the `USING (true)` check missed `1=1` and every
whitespace variant.

Usage:
    uv run audit_rls.py --db-url "$DATABASE_URL"
    uv run audit_rls.py --db-url "$DATABASE_URL" --json
    uv run audit_rls.py --db-url "$DATABASE_URL" --schema public --schema api
    uv run audit_rls.py --db-url "$DATABASE_URL" --no-splinter   # own rules only

Exit codes: 0 = no findings, 1 = findings, 2 = usage/connection error.
"""

from __future__ import annotations

import json as jsonlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import typer

app = typer.Typer(add_completion=False)

# Roles PostgREST exposes to the internet. A privilege held by one of these is
# reachable by anyone holding the (public) publishable key.
API_ROLES = ("anon", "authenticated")

# Reason: copied from Splinter. Supabase's own schemas carry permissive internal
# grants by design; flagging them is pure noise and trains people to ignore the
# tool. Kept verbatim so it stays diffable against the upstream list.
SYSTEM_SCHEMAS = (
    "_timescaledb_cache, _timescaledb_catalog, _timescaledb_config, "
    "_timescaledb_internal, auth, cron, extensions, graphql, graphql_public, "
    "information_schema, net, pgmq, pgroonga, pgsodium, pgsodium_masks, pgtle, "
    "pgbouncer, pg_catalog, realtime, repack, storage, supabase_functions, "
    "supabase_migrations, tiger, topology, vault"
)


@dataclass
class Finding:
    """One problem found. `rule` maps to a rule ID in SKILL.md."""

    severity: str  # ERROR | WARN | INFO
    rule: str
    object: str
    detail: str


# Each query returns rows of (object, detail). Kept as SQL rather than ORM code
# so a reviewer can read exactly what is being asserted.
QUERIES: list[tuple[str, str, str, str]] = [
    (
        "ERROR",
        "R4-delete-reinsert",
        """
        SELECT t.table_schema || '.' || t.table_name || ' (' || t.grantee || ')',
               'column-level UPDATE granted, but the role holds BOTH INSERT and DELETE - '
               'the column revoke can be bypassed by deleting and re-inserting the row'
          FROM (SELECT DISTINCT table_schema, table_name, grantee
                  FROM information_schema.column_privileges
                 WHERE privilege_type = 'UPDATE'
                   AND grantee = ANY(%(api_roles)s)
                   AND table_schema = ANY(%(schemas)s)) t
         WHERE has_table_privilege(t.grantee,
                                   format('%%I.%%I', t.table_schema, t.table_name), 'INSERT')
           AND has_table_privilege(t.grantee,
                                   format('%%I.%%I', t.table_schema, t.table_name), 'DELETE')
           AND NOT has_table_privilege(t.grantee,
                                       format('%%I.%%I', t.table_schema, t.table_name), 'UPDATE')
        """,
        # Reason: the bypass needs BOTH -- DELETE alone destroys the row but
        # cannot recreate it with attacker-chosen values, so it is data loss,
        # not privilege escalation.
        #
        # has_table_privilege (rather than information_schema) is deliberate: it
        # resolves privileges inherited via role membership and PUBLIC, which the
        # information_schema views do not show.
        "Undocumented by Supabase and Postgres alike. The reason this script exists.",
    ),
    (
        "ERROR",
        "R11-truncate-granted",
        """
        SELECT n.nspname || '.' || c.relname || ' (' || r.rolname || ')',
               'TRUNCATE granted - NO RLS policy applies to it; this role can wipe '
                 || 'every tenant''s rows regardless of isolation'
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          CROSS JOIN unnest(%(api_roles)s::text[]) AS r(rolname)
         WHERE c.relkind IN ('r', 'p')
           AND n.nspname = ANY(%(schemas)s)
           AND has_table_privilege(r.rolname, c.oid, 'TRUNCATE')
        """,
        # Reason: RLS governs rows; TRUNCATE is a whole-table operation and no
        # policy is consulted. Perfect tenant isolation does not survive it.
        "The gap RLS cannot cover at all.",
    ),
    (
        "WARN",
        "R1-policy-for-all",
        """
        SELECT p.schemaname || '.' || p.tablename || ' :: ' || p.policyname,
               'policy covers ALL commands - if FOR was omitted this is also your '
                 || 'INSERT/UPDATE/DELETE rule; confirm it is intentional'
          FROM pg_policies p
         WHERE p.schemaname = ANY(%(schemas)s)
           AND p.cmd = 'ALL'
        """,
        # Reason: Postgres does not record whether ALL came from an omitted FOR
        # or an explicit `FOR ALL`, so this cannot prove a mistake -- it asks for
        # confirmation. A policy named "Users can VIEW..." that is silently also
        # the write rule caused three separate escalations in one codebase.
        "Splinter has no equivalent; this is the highest-yield policy check.",
    ),
    (
        "WARN",
        "R1-policy-no-to",
        """
        SELECT p.schemaname || '.' || p.tablename || ' :: ' || p.policyname,
               'policy applies TO PUBLIC (every role) - name anon/authenticated explicitly'
          FROM pg_policies p
         WHERE p.schemaname = ANY(%(schemas)s)
           AND p.roles = ARRAY['public']::name[]
        """,
        # Reason: the RAW catalogue (pg_policy.polroles) stores PUBLIC as {0},
        # but the pg_policies VIEW resolves role OIDs to names, so PUBLIC reads
        # as {public}. Matching '{0}' here silently matched nothing -- a check
        # that never fires is indistinguishable from a clean database, which is
        # why that bug survived testing. Reading Splinter's SQL first would have
        # caught it.
        "Omitted TO defaults to PUBLIC; naming the role is also a large perf win.",
    ),
    (
        "WARN",
        "R2-no-restrictive",
        """
        SELECT n.nspname || '.' || c.relname,
               'RLS enabled but no RESTRICTIVE policy - a permissive policy added later '
                 || 'can OR straight past tenant isolation'
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p')
           AND n.nspname = ANY(%(schemas)s)
           AND c.relrowsecurity
           AND EXISTS (SELECT 1 FROM pg_policies p
                        WHERE p.schemaname = n.nspname AND p.tablename = c.relname)
           AND NOT EXISTS (SELECT 1 FROM pg_policies p
                            WHERE p.schemaname = n.nspname AND p.tablename = c.relname
                              AND p.permissive = 'RESTRICTIVE')
        """,
        # Reason: advisory. Not every table is multi-tenant, so this is a prompt
        # to think rather than a defect. Lookup and reference tables will show up
        # here legitimately.
        "Advisory: multi-tenant invariants belong in a RESTRICTIVE policy.",
    ),
]

# Reason: resolve the schemas PostgREST actually exposes, the way Splinter does,
# instead of assuming 'public'. A project serving an `api` schema would otherwise
# be audited on the wrong objects entirely -- and report a clean bill of health.
EXPOSED_SCHEMAS_SQL = """
    SELECT array(
        SELECT trim(unnest(string_to_array(
            coalesce(current_setting('pgrst.db_schemas', true), 'public'), ',')))
        EXCEPT
        SELECT trim(unnest(string_to_array(%(system_schemas)s, ',')))
    )
"""


def resolve_schemas(conn: psycopg.Connection, override: list[str] | None) -> list[str]:
    """Which schemas to audit: the caller's list, else whatever PostgREST exposes."""
    if override:
        return override
    with conn.cursor() as cur:
        cur.execute(cast("LiteralString", EXPOSED_SCHEMAS_SQL), {"system_schemas": SYSTEM_SCHEMAS})
        row = cur.fetchone()
    return list(row[0]) if row and row[0] else ["public"]


SPLINTER_SQL = Path(__file__).resolve().parent.parent / "vendor" / "splinter.sql"


def run_splinter(conn: psycopg.Connection, schemas: list[str]) -> list[Finding]:
    """Run Supabase's own linter, vendored in vendor/splinter.sql.

    Splinter is the engine behind the dashboard's Security Advisor. Running it
    here means one command gives complete coverage instead of the user having to
    remember a second tool -- and remembering it is exactly what does not happen.

    Safe in a read-only transaction: the file is a single SELECT plus a DO block
    that only reads storage.buckets and sets a transaction-local GUC. Verified.
    """
    if not SPLINTER_SQL.is_file():
        return [
            Finding(
                "INFO",
                "splinter",
                "<not available>",
                f"vendor/splinter.sql not found at {SPLINTER_SQL}; skipping Supabase's own lints",
            )
        ]

    findings: list[Finding] = []
    with conn.cursor() as cur:
        try:
            # Reason: Splinter's API-exposure lints read pgrst.db_schemas, which
            # PostgREST sets at runtime and a plain psql connection does not have.
            # Without this they silently fall back to `public` only -- upstream
            # calls this out explicitly in its README.
            cur.execute(
                cast("LiteralString", "SELECT set_config('pgrst.db_schemas', %(s)s, true)"),
                {"s": ", ".join(schemas)},
            )
            # Reason: splinter.sql is a preamble (SET, then a DO block that stashes
            # public storage buckets in a GUC) followed by the actual SELECT. psycopg
            # returns the results of the LAST statement in a multi-statement execute,
            # but fetchall() then reads from the first -- which is a SET and produces
            # no rows, raising "the last operation didn't produce records". Split on
            # the DO terminator so the query runs as its own statement.
            script = SPLINTER_SQL.read_text()
            marker = "end $$;"
            if marker in script:
                preamble, query = script.split(marker, 1)
                cur.execute(cast("LiteralString", preamble + marker))
            else:
                query = script
            cur.execute(cast("LiteralString", query))
            rows = cur.fetchall()
        except psycopg.Error as exc:
            conn.rollback()
            return [Finding("INFO", "splinter", "<check failed>", f"{type(exc).__name__}: {exc}".strip())]

    # Columns: name, title, level, facing, categories, description, detail,
    #          remediation, metadata, cache_key
    for row in rows:
        name, level, facing, categories = row[0], row[2], row[3], row[4]
        detail, metadata = row[6], row[8]
        # Reason: INTERNAL lints are for Supabase's own operators, and PERFORMANCE
        # ones are out of scope for a security audit. Keep the signal tight.
        if facing != "EXTERNAL" or "SECURITY" not in (categories or []):
            continue
        # Reason: surface the object in its own column so splinter rows line up
        # with ours; metadata carries schema/name for most lints.
        obj = ""
        if isinstance(metadata, dict) and metadata.get("name"):
            obj = f"{metadata.get('schema', '?')}.{metadata['name']}"
        # Reason: upstream wraps identifiers in backticks that arrive escaped.
        findings.append(Finding(level, f"splinter:{name}", obj, detail.replace("\\`", "`")))
    return findings


def run_audit(
    db_url: str, override: list[str] | None, *, with_splinter: bool = True
) -> tuple[list[Finding], list[str]]:
    """Execute every check. Read-only; a failing check is reported, not fatal."""
    findings: list[Finding] = []

    # Reason: read_only=True makes the "this never writes" claim enforced by the
    # server, not merely by inspection of the SQL above.
    with psycopg.connect(db_url) as conn:
        conn.read_only = True
        schemas = resolve_schemas(conn, override)
        params: dict[str, Any] = {"schemas": schemas, "api_roles": list(API_ROLES)}

        if with_splinter:
            findings.extend(run_splinter(conn, schemas))

        for severity, rule, sql, _rationale in QUERIES:
            with conn.cursor() as cur:
                try:
                    # Reason: cast is safe here -- every entry in QUERIES is a
                    # literal defined above, never built from input.
                    cur.execute(cast("LiteralString", sql), params)
                except psycopg.Error as exc:
                    findings.append(Finding("INFO", rule, "<check failed>", f"{type(exc).__name__}: {exc}".strip()))
                    conn.rollback()
                    continue
                for obj, detail in cur.fetchall():
                    findings.append(Finding(severity, rule, obj, detail))
    return findings, schemas


@app.command()
def main(
    db_url: str = typer.Option(..., "--db-url", help="Postgres connection string", envvar="DATABASE_URL"),
    schema: list[str] = typer.Option(
        None, "--schema", help="Schema to audit (repeatable). Default: whatever PostgREST exposes."
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
    splinter: bool = typer.Option(True, "--splinter/--no-splinter", help="Also run Supabase's own linter (vendored)"),
) -> None:
    """Audit a Supabase database for access-control defects Supabase's own linter misses."""
    try:
        findings, schemas = run_audit(db_url, list(schema) if schema else None, with_splinter=splinter)
    except psycopg.Error as exc:
        # Reason: never echo the exception body - a connection string with a
        # password can appear in psycopg error text.
        typer.echo(f"Could not connect or query: {type(exc).__name__}", err=True)
        raise typer.Exit(2) from None

    if json_output:
        typer.echo(jsonlib.dumps({"schemas": schemas, "findings": [asdict(f) for f in findings]}, indent=2))
    else:
        typer.echo(f"Auditing schema(s): {', '.join(schemas)}\n")
        if not findings:
            typer.echo("No findings.")
        else:
            order = {"ERROR": 0, "WARN": 1, "INFO": 2}
            for f in sorted(findings, key=lambda x: (order.get(x.severity, 9), x.rule, x.object)):
                typer.echo(f"[{f.severity:5}] {f.rule:24} {f.object}\n          {f.detail}")
            counts: dict[str, int] = {}
            for f in findings:
                counts[f.severity] = counts.get(f.severity, 0) + 1
            typer.echo("\n" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

        typer.echo(
            "\nRules prefixed `splinter:` come from Supabase's own linter "
            "(vendor/splinter.sql); the rest are checks it does not have."
        )
        typer.echo(
            "Neither replaces negative tests: assert denial with the publishable "
            "key alone and with a second tenant's JWT."
        )

    raise typer.Exit(1 if findings else 0)


if __name__ == "__main__":
    app()
