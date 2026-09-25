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

2. RULES SPLINTER DOES NOT HAVE:

       R4   delete-and-reinsert defeating a column-level UPDATE revoke
       R13  an Auth hook left executable by the API roles
       R1   permissive policies covering ALL commands, or applying TO PUBLIC
            (including storage.objects and realtime.messages)
       R2   RLS tables with no RESTRICTIVE policy, or one covering reads only
       R11  TRUNCATE, which no RLS policy applies to
       R12  default privileges that expose every future table or function
       R14  public Storage buckets

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
        SELECT n.nspname || '.' || c.relname || ' (' || r.rolname || ')',
               'column-level UPDATE withholds ' || string_agg(quote_ident(a.attname), ', ' ORDER BY a.attnum)
                 || ', but the role can DELETE the row and INSERT those columns - '
                 || 'the column revoke can be bypassed by deleting and re-inserting the row'
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          CROSS JOIN unnest(%(api_roles)s::text[]) AS r(rolname)
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
         WHERE c.relkind IN ('r', 'p')
           AND n.nspname = ANY(%(schemas)s)
           AND has_any_column_privilege(r.rolname, c.oid, 'UPDATE')
           AND NOT has_table_privilege(r.rolname, c.oid, 'UPDATE')
           AND has_table_privilege(r.rolname, c.oid, 'DELETE')
           AND NOT has_column_privilege(r.rolname, c.oid, a.attnum, 'UPDATE')
           AND has_column_privilege(r.rolname, c.oid, a.attnum, 'INSERT')
           AND (NOT c.relrowsecurity
                OR (EXISTS (SELECT 1 FROM pg_policies p
                             WHERE p.schemaname = n.nspname AND p.tablename = c.relname
                               AND p.permissive = 'PERMISSIVE' AND p.cmd IN ('DELETE', 'ALL')
                               AND EXISTS (SELECT 1 FROM unnest(p.roles) AS pr(name)
                                            WHERE pr.name = 'public'
                                               OR pg_has_role(r.rolname, pr.name, 'USAGE')))
                    AND EXISTS (SELECT 1 FROM pg_policies p
                                 WHERE p.schemaname = n.nspname AND p.tablename = c.relname
                                   AND p.permissive = 'PERMISSIVE' AND p.cmd IN ('INSERT', 'ALL')
                                   AND EXISTS (SELECT 1 FROM unnest(p.roles) AS pr(name)
                                                WHERE pr.name = 'public'
                                                   OR pg_has_role(r.rolname, pr.name, 'USAGE')))))
         GROUP BY n.nspname, c.relname, r.rolname
        """,
        # Reason: the bypass needs BOTH -- DELETE alone destroys the row but
        # cannot recreate it with attacker-chosen values, so it is data loss,
        # not privilege escalation.
        #
        # INSERT is checked per column, not per table: a column-level INSERT
        # grant makes has_table_privilege(..., 'INSERT') false, and an earlier
        # version missed exactly that case. The columns reported are the ones
        # the role cannot UPDATE yet can write by re-inserting.
        #
        # With RLS on, both halves also need a permissive policy for the role:
        # without a DELETE and an INSERT policy the path is closed by default
        # deny, whatever the grants say. The policy predicates are not examined,
        # so a finding on an RLS table means "possible", to be tested.
        #
        # has_*_privilege (rather than information_schema) is deliberate: it
        # resolves privileges inherited via role membership and PUBLIC, which the
        # information_schema views do not show.
        "Undocumented by Supabase and Postgres alike. The reason this script exists.",
    ),
    (
        "WARN",
        "R11-truncate-granted",
        """
        SELECT n.nspname || '.' || c.relname || ' (' || r.rolname || ')',
               'TRUNCATE granted - no RLS policy applies to it, so any function that '
                 || 'truncates as the caller wipes every tenant''s rows'
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          CROSS JOIN unnest(%(api_roles)s::text[]) AS r(rolname)
         WHERE c.relkind IN ('r', 'p')
           AND n.nspname = ANY(%(schemas)s)
           AND has_table_privilege(r.rolname, c.oid, 'TRUNCATE')
        """,
        # Reason: RLS governs rows; TRUNCATE is a whole-table operation and no
        # policy is consulted. WARN, not ERROR: PostgREST and pg_graphql expose no
        # TRUNCATE verb and anon/authenticated cannot log in, so it is reachable
        # only through a SECURITY INVOKER function that truncates (or builds SQL
        # dynamically). Legacy default privileges grant it on every new table,
        # so expect this on older projects.
        "Defence in depth: the one privilege RLS cannot narrow at all.",
    ),
    (
        "WARN",
        "R1-policy-for-all",
        """
        SELECT p.schemaname || '.' || p.tablename || ' :: ' || p.policyname,
               'permissive policy covers ALL commands - if FOR was omitted this is also your '
                 || 'INSERT/UPDATE/DELETE rule; confirm it is intentional'
          FROM pg_policies p
         WHERE (p.schemaname = ANY(%(schemas)s)
                OR (p.schemaname, p.tablename) IN (('storage', 'objects'), ('realtime', 'messages')))
           AND p.cmd = 'ALL'
           AND p.permissive = 'PERMISSIVE'
        """,
        # Reason: Postgres does not record whether ALL came from an omitted FOR
        # or an explicit `FOR ALL`, so this cannot prove a mistake -- it asks for
        # confirmation. RESTRICTIVE policies are exempt: they only narrow, and a
        # restrictive FOR ALL is the recommended shape for tenant isolation (R2).
        # A policy named "Users can VIEW..." that is silently also the write rule
        # caused three separate escalations in one codebase.
        "Splinter has no equivalent; this is the highest-yield policy check.",
    ),
    (
        "WARN",
        "R1-policy-no-to",
        """
        SELECT p.schemaname || '.' || p.tablename || ' :: ' || p.policyname,
               'policy applies TO PUBLIC (every role) - name anon/authenticated explicitly'
          FROM pg_policies p
         WHERE (p.schemaname = ANY(%(schemas)s)
                OR (p.schemaname, p.tablename) IN (('storage', 'objects'), ('realtime', 'messages')))
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
    (
        "WARN",
        "R2-restrictive-reads-only",
        """
        SELECT n.nspname || '.' || c.relname || ' (' || r.rolname || ')',
               'RESTRICTIVE policy exists, but not for ' || string_agg(w.cmd, ', ' ORDER BY w.cmd)
                 || ' - a permissive write policy there is not held to the tenant invariant'
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          CROSS JOIN unnest(%(api_roles)s::text[]) AS r(rolname)
          CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE']) AS w(cmd)
         WHERE c.relkind IN ('r', 'p')
           AND n.nspname = ANY(%(schemas)s)
           AND c.relrowsecurity
           AND (has_table_privilege(r.rolname, c.oid, w.cmd)
                OR (w.cmd <> 'DELETE' AND has_any_column_privilege(r.rolname, c.oid, w.cmd)))
           AND EXISTS (SELECT 1 FROM pg_policies p
                        WHERE p.schemaname = n.nspname AND p.tablename = c.relname
                          AND p.permissive = 'RESTRICTIVE')
           AND EXISTS (SELECT 1 FROM pg_policies p
                        WHERE p.schemaname = n.nspname AND p.tablename = c.relname
                          AND p.permissive = 'PERMISSIVE'
                          AND p.cmd IN (w.cmd, 'ALL')
                          AND EXISTS (SELECT 1 FROM unnest(p.roles) AS pr(name)
                                       WHERE pr.name = 'public' OR pg_has_role(r.rolname, pr.name, 'USAGE')))
           AND NOT EXISTS (SELECT 1 FROM pg_policies p
                            WHERE p.schemaname = n.nspname AND p.tablename = c.relname
                              AND p.permissive = 'RESTRICTIVE'
                              AND p.cmd IN (w.cmd, 'ALL')
                              AND EXISTS (SELECT 1 FROM unnest(p.roles) AS pr(name)
                                           WHERE pr.name = 'public' OR pg_has_role(r.rolname, pr.name, 'USAGE')))
         GROUP BY n.nspname, c.relname, r.rolname
        """,
        # Reason: the common shape is a RESTRICTIVE `FOR SELECT` tenancy policy
        # plus permissive write policies. Restrictive policies bind only the
        # commands they name, so writes -- including the NEW row an UPDATE
        # produces -- are held only to the permissive predicate. Reported only
        # where the role holds the privilege AND a permissive policy applies to
        # the command; otherwise the command is denied anyway. The permissive
        # predicate is not examined, so it may already pin tenancy. Policy roles
        # are matched by effective membership (pg_has_role), because a policy
        # granted to a parent role applies to its members.
        "Restrictive tenancy must cover every command the role can run.",
    ),
    (
        "WARN",
        "R12-default-privileges",
        """
        SELECT coalesce(nullif(d.defaclnamespace::regnamespace::text, '-'), '<all schemas>')
                 || ' (objects created by ' || d.defaclrole::regrole::text || ')',
               'default privileges grant '
                 || string_agg(DISTINCT a.privilege_type || ' on new '
                      || CASE d.defaclobjtype WHEN 'r' THEN 'tables' ELSE 'functions' END
                      || ' to ' || CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
                    '; ')
                 || ' - every future object starts out reachable from the Data API'
          FROM pg_default_acl d
          CROSS JOIN LATERAL aclexplode(d.defaclacl) a
         WHERE (a.grantee = 0
                OR EXISTS (SELECT 1 FROM unnest(%(api_roles)s::text[]) AS r(rolname)
                            WHERE pg_has_role(r.rolname, a.grantee, 'USAGE')))
           AND d.defaclobjtype IN ('r', 'f')
           AND (d.defaclnamespace = 0
                OR (SELECT nspname FROM pg_namespace WHERE oid = d.defaclnamespace) = ANY(%(schemas)s))
         GROUP BY d.defaclnamespace, d.defaclrole
        UNION ALL
        SELECT '<all schemas> (functions created by ' || r.rolname || ')',
               'no global default privilege replaces PostgreSQL''s built-in EXECUTE for PUBLIC - '
                 || 'every new function is callable by anon and authenticated until revoked'
          FROM pg_roles r
         WHERE r.rolname !~ '^pg_'
           AND EXISTS (SELECT 1 FROM pg_namespace n
                        WHERE n.nspname = ANY(%(schemas)s)
                          AND has_schema_privilege(r.oid, n.oid, 'CREATE'))
           AND NOT EXISTS (SELECT 1 FROM pg_default_acl d
                            WHERE d.defaclrole = r.oid AND d.defaclnamespace = 0
                              AND d.defaclobjtype = 'f')
        """,
        # Reason: a table-by-table audit is a snapshot; default privileges decide
        # what the NEXT migration exposes. Tables and functions only -- sequence
        # USAGE is functional, not an authorization boundary. Expected on
        # projects created before Supabase's safer-defaults change; it is the
        # reason RLS must be enabled in the same migration that creates a table.
        #
        # Grantees are matched by effective membership, and PUBLIC counts: both
        # reach anon. The UNION arm covers PostgreSQL's hard-wired default, which
        # grants EXECUTE on functions to PUBLIC and has no pg_default_acl row at
        # all. Only a GLOBAL entry (defaclnamespace = 0) replaces it; a per-schema
        # REVOKE ... FROM PUBLIC has no effect. Every role that can CREATE in an
        # audited schema is checked, since defaults belong to the creating role
        # (on a stock project that is postgres and supabase_admin).
        "Snapshot audits miss what the next CREATE will expose.",
    ),
    (
        "WARN",
        "R13-auth-hook-callable",
        """
        SELECT n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
               'granted to supabase_auth_admin (likely an Auth hook) and executable by '
                 || string_agg(r.rolname, ', ' ORDER BY r.rolname)
                 || ' in an exposed schema - callable as POST /rest/v1/rpc/' || p.proname
                 || '; revoke it from anon, authenticated and public'
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
          CROSS JOIN unnest(%(api_roles)s::text[]) AS r(rolname)
         WHERE n.nspname = ANY(%(schemas)s)
           AND EXISTS (SELECT 1 FROM aclexplode(p.proacl) a
                        JOIN pg_roles g ON g.oid = a.grantee
                       WHERE g.rolname = 'supabase_auth_admin' AND a.privilege_type = 'EXECUTE')
           AND has_function_privilege(r.rolname, p.oid, 'EXECUTE')
           AND has_schema_privilege(r.rolname, n.oid, 'USAGE')
         GROUP BY n.nspname, p.proname, p.oid
        """,
        # Reason: an Auth hook (e.g. custom access token) is recognised by the
        # explicit EXECUTE grant Supabase's docs require for supabase_auth_admin.
        # That grant does not prove the hook is configured, hence WARN and
        # "likely". Left executable by the API roles, it is an RPC that returns
        # the claims it would issue for an arbitrary user_id (it does not sign a
        # token). The docs require the revoke from authenticated, anon and public.
        "Hooks must be revoked from the API roles.",
    ),
]

# Reason: Storage is outside the schemas PostgREST exposes, but a public bucket
# is readable by anyone who knows an object's path -- no policy is consulted on
# the public download route. Kept separate from QUERIES because it targets a
# fixed Supabase schema, and skipped when Storage is not installed.
STORAGE_QUERIES: list[tuple[str, str, str, str]] = [
    (
        "WARN",
        "R14-public-bucket",
        """
        SELECT 'storage.buckets :: ' || b.id,
               'public bucket - objects are served to anyone with the URL and '
                 || 'storage.objects policies are not consulted for downloads'
          FROM storage.buckets b
         WHERE b.public
        """,
        "Per-user files belong in a private bucket with owner- or path-scoped policies.",
    ),
]

# Reason: resolve the schemas PostgREST actually exposes instead of assuming
# 'public'. A project serving an `api` schema would otherwise be audited on the
# wrong objects entirely -- and report a clean bill of health.
#
# The auditor's own session almost never carries `pgrst.db_schemas`: PostgREST
# reads it from its config file or environment (the Supabase CLI passes
# PGRST_DB_SCHEMAS), or from a role setting on `authenticator`. Only the last is
# visible from SQL, so read it there. NULL means "not discoverable", and the
# caller must then name the schemas -- silently falling back to 'public' is the
# bug this replaces.
EXPOSED_SCHEMAS_SQL = """
    WITH configured AS (
        SELECT coalesce(
            nullif(current_setting('pgrst.db_schemas', true), ''),
            (SELECT substr(cfg, length('pgrst.db_schemas=') + 1)
               FROM pg_db_role_setting s
               JOIN pg_roles r ON r.oid = s.setrole
               CROSS JOIN LATERAL unnest(s.setconfig) AS cfg
              WHERE r.rolname = 'authenticator'
                AND s.setdatabase IN (0, (SELECT oid FROM pg_database
                                           WHERE datname = current_database()))
                AND cfg LIKE 'pgrst.db_schemas=%%'
              ORDER BY s.setdatabase DESC   -- a per-database setting beats a global one
              LIMIT 1)
        ) AS value
    )
    SELECT CASE WHEN value IS NULL THEN NULL ELSE array(
        SELECT trim(unnest(string_to_array(value, ',')))
        EXCEPT
        SELECT trim(unnest(string_to_array(%(system_schemas)s, ',')))
    ) END
    FROM configured
"""


class SchemasNotDiscoverable(Exception):
    """PostgREST's exposed-schema list is not readable from this connection."""


def resolve_schemas(conn: psycopg.Connection, override: list[str] | None) -> list[str]:
    """Which schemas to audit: the caller's list, else whatever PostgREST exposes."""
    if override:
        return override
    with conn.cursor() as cur:
        cur.execute(cast("LiteralString", EXPOSED_SCHEMAS_SQL), {"system_schemas": SYSTEM_SCHEMAS})
        row = cur.fetchone()
    if not row or row[0] is None:
        raise SchemasNotDiscoverable
    return list(row[0])


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

        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('storage.buckets') IS NOT NULL")
            row = cur.fetchone()
            has_storage = bool(row and row[0])

        for severity, rule, sql, _rationale in QUERIES + (STORAGE_QUERIES if has_storage else []):
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
    except SchemasNotDiscoverable:
        typer.echo(
            "Cannot tell which schemas PostgREST exposes: no `pgrst.db_schemas` role "
            "setting on `authenticator`. Pass them explicitly, e.g. "
            "--schema public --schema api (Dashboard -> Data API -> Exposed schemas, "
            "or [api].schemas in supabase/config.toml).",
            err=True,
        )
        raise typer.Exit(2) from None
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
