#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.1", "typer>=0.12"]
# ///
"""Audit a Supabase/Postgres database for the access-control defects in this skill.

READ-ONLY. Every query reads catalogue tables; nothing is written, and the
connection is opened in a read-only transaction as a belt-and-braces guard.

This complements Supabase's own advisors (Splinter) rather than replacing them:
it checks things the advisors do not, notably the delete-and-reinsert bypass and
policies that omit FOR/TO.

Usage:
    uv run audit_rls.py --db-url "$DATABASE_URL"
    uv run audit_rls.py --db-url "$DATABASE_URL" --json
    uv run audit_rls.py --db-url "$DATABASE_URL" --schema public --schema api

Exit codes: 0 = no findings, 1 = findings, 2 = usage/connection error.
"""

from __future__ import annotations

import json as jsonlib
from dataclasses import asdict, dataclass
from typing import Any, LiteralString, cast

import psycopg
import typer

app = typer.Typer(add_completion=False)

# Roles PostgREST exposes to the internet. A privilege held by one of these is
# reachable by anyone holding the (public) publishable key.
API_ROLES = ("anon", "authenticated")


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
        "R10-rls-disabled",
        """
        SELECT c.relnamespace::regnamespace || '.' || c.relname,
               'RLS is NOT enabled - readable and writable by anyone with the publishable key'
          FROM pg_class c
         WHERE c.relkind = 'r'
           AND c.relnamespace::regnamespace::text = ANY(%(schemas)s)
           AND NOT c.relrowsecurity
        """,
        "Tables exposed to the API with row-level security switched off.",
    ),
    (
        "ERROR",
        "R1-policy-no-for",
        """
        SELECT p.schemaname || '.' || p.tablename || ' :: ' || p.policyname,
               'policy has no FOR clause - it governs SELECT, INSERT, UPDATE and DELETE'
          FROM pg_policies p
         WHERE p.schemaname = ANY(%(schemas)s)
           AND p.cmd = 'ALL'
        """,
        "A policy named like a read rule is silently also the write rule.",
    ),
    (
        "WARN",
        "R1-policy-no-to",
        """
        SELECT p.schemaname || '.' || p.tablename || ' :: ' || p.policyname,
               'policy applies TO PUBLIC (every role) - name anon/authenticated explicitly'
          FROM pg_policies p
         WHERE p.schemaname = ANY(%(schemas)s)
           AND p.roles = '{0}'
        """,
        "Omitted TO defaults to PUBLIC; naming the role is also a large perf win.",
    ),
    (
        "WARN",
        "R2-permissive-true",
        """
        SELECT p.schemaname || '.' || p.tablename || ' :: ' || p.policyname,
               'permissive policy with USING (true) - grants unrestricted access'
          FROM pg_policies p
         WHERE p.schemaname = ANY(%(schemas)s)
           AND p.permissive = 'PERMISSIVE'
           AND btrim(coalesce(p.qual, '')) = 'true'
        """,
        "USING (true) ORs past every other permissive policy on the table.",
    ),
    (
        "WARN",
        "R2-no-restrictive",
        """
        SELECT c.relnamespace::regnamespace || '.' || c.relname,
               'RLS enabled but no RESTRICTIVE policy - tenant isolation can be ORed past'
          FROM pg_class c
         WHERE c.relkind = 'r'
           AND c.relnamespace::regnamespace::text = ANY(%(schemas)s)
           AND c.relrowsecurity
           AND EXISTS (SELECT 1 FROM pg_policies p
                        WHERE p.schemaname = c.relnamespace::regnamespace::text
                          AND p.tablename = c.relname)
           AND NOT EXISTS (SELECT 1 FROM pg_policies p
                            WHERE p.schemaname = c.relnamespace::regnamespace::text
                              AND p.tablename = c.relname
                              AND p.permissive = 'RESTRICTIVE')
        """,
        "Advisory: multi-tenant invariants belong in a RESTRICTIVE policy.",
    ),
    (
        "ERROR",
        "R4-delete-reinsert",
        """
        SELECT t.table_schema || '.' || t.table_name || ' (' || t.grantee || ')',
               'column-level UPDATE granted, but INSERT/DELETE also granted - '
               'the column revoke can be bypassed by deleting and re-inserting the row'
          FROM (SELECT DISTINCT table_schema, table_name, grantee
                  FROM information_schema.column_privileges
                 WHERE privilege_type = 'UPDATE'
                   AND grantee = ANY(%(api_roles)s)
                   AND table_schema = ANY(%(schemas)s)) t
         WHERE EXISTS (SELECT 1 FROM information_schema.table_privileges tp
                        WHERE tp.table_schema = t.table_schema
                          AND tp.table_name = t.table_name
                          AND tp.grantee = t.grantee
                          AND tp.privilege_type IN ('INSERT', 'DELETE'))
           AND NOT EXISTS (SELECT 1 FROM information_schema.table_privileges tp
                            WHERE tp.table_schema = t.table_schema
                              AND tp.table_name = t.table_name
                              AND tp.grantee = t.grantee
                              AND tp.privilege_type = 'UPDATE')
        """,
        "The highest-value check here - undocumented by Supabase and Postgres alike.",
    ),
    (
        "WARN",
        "R5-function-executable",
        """
        SELECT p.pronamespace::regnamespace || '.' || p.proname
                 || CASE WHEN p.prosecdef THEN ' [SECURITY DEFINER]' ELSE '' END,
               'EXECUTE granted to ' || r.rolname
                 || ' - reachable as POST /rest/v1/rpc/' || p.proname
          FROM pg_proc p
          CROSS JOIN unnest(%(api_roles)s::text[]) AS r(rolname)
         WHERE p.pronamespace::regnamespace::text = ANY(%(schemas)s)
           AND has_function_privilege(r.rolname, p.oid, 'EXECUTE')
           AND p.prosecdef
        """,
        "SECURITY DEFINER functions callable from a browser. RLS does not apply to functions.",
    ),
    (
        "WARN",
        "R6-mutable-search-path",
        """
        SELECT p.pronamespace::regnamespace || '.' || p.proname,
               'SECURITY DEFINER without a pinned search_path'
          FROM pg_proc p
         WHERE p.pronamespace::regnamespace::text = ANY(%(schemas)s)
           AND p.prosecdef
           AND (p.proconfig IS NULL
                OR NOT EXISTS (SELECT 1 FROM unnest(p.proconfig) cfg
                                WHERE cfg LIKE 'search_path=%%'))
        """,
        "An unpinned search_path in a definer function is an escalation primitive.",
    ),
    (
        "ERROR",
        "R7-view-not-invoker",
        """
        SELECT c.relnamespace::regnamespace || '.' || c.relname,
               'view without security_invoker - runs as owner and bypasses the caller''s RLS'
          FROM pg_class c
         WHERE c.relkind = 'v'
           AND c.relnamespace::regnamespace::text = ANY(%(schemas)s)
           AND (c.reloptions IS NULL
                OR NOT ('security_invoker=on' = ANY(c.reloptions)
                        OR 'security_invoker=true' = ANY(c.reloptions)))
        """,
        "PG15+ feature, opt-in. Supabase lints this at ERROR level too.",
    ),
    (
        "WARN",
        "R7-matview-exposed",
        """
        SELECT c.relnamespace::regnamespace || '.' || c.relname,
               'materialized view in an exposed schema - cannot enforce RLS at all'
          FROM pg_class c
         WHERE c.relkind = 'm'
           AND c.relnamespace::regnamespace::text = ANY(%(schemas)s)
        """,
        "Materialized views have no RLS. Keep them out of exposed schemas.",
    ),
    (
        "ERROR",
        "R8-user-metadata",
        """
        SELECT p.schemaname || '.' || p.tablename || ' :: ' || p.policyname,
               'policy references user_metadata, which end users can rewrite themselves'
          FROM pg_policies p
         WHERE p.schemaname = ANY(%(schemas)s)
           AND (coalesce(p.qual, '') || ' ' || coalesce(p.with_check, ''))
               LIKE '%%user_metadata%%'
        """,
        "raw_user_meta_data is user-writable through the auth API. Use app_metadata.",
    ),
    (
        "WARN",
        "R10-anon-writes",
        """
        SELECT tp.table_schema || '.' || tp.table_name,
               'anon holds ' || tp.privilege_type || ' - logged-out users can write'
          FROM information_schema.table_privileges tp
         WHERE tp.grantee = 'anon'
           AND tp.privilege_type IN ('INSERT', 'UPDATE', 'DELETE')
           AND tp.table_schema = ANY(%(schemas)s)
        """,
        "Logged-out write access is almost never intended.",
    ),
]


def run_audit(db_url: str, schemas: list[str]) -> list[Finding]:
    """Execute every check. Read-only; a failing check is reported, not fatal."""
    findings: list[Finding] = []
    params: dict[str, Any] = {"schemas": schemas, "api_roles": list(API_ROLES)}

    # Reason: read_only=True makes the "this never writes" claim enforced by the
    # server, not merely by inspection of the SQL above.
    with psycopg.connect(db_url) as conn:
        conn.read_only = True
        for severity, rule, sql, _rationale in QUERIES:
            with conn.cursor() as cur:
                try:
                    # Reason: cast is safe and meaningful here -- every entry in
                    # QUERIES is a literal defined above, never built from input.
                    # psycopg's LiteralString parameter type exists precisely to
                    # stop SQL being assembled from runtime strings.
                    cur.execute(cast("LiteralString", sql), params)
                except psycopg.Error as exc:
                    findings.append(Finding("INFO", rule, "<check failed>", f"{type(exc).__name__}: {exc}".strip()))
                    conn.rollback()
                    continue
                for obj, detail in cur.fetchall():
                    findings.append(Finding(severity, rule, obj, detail))
    return findings


@app.command()
def main(
    db_url: str = typer.Option(..., "--db-url", help="Postgres connection string", envvar="DATABASE_URL"),
    schema: list[str] = typer.Option(["public"], "--schema", help="Schema to audit (repeatable)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Audit a Supabase database for access-control defects."""
    try:
        findings = run_audit(db_url, list(schema))
    except psycopg.Error as exc:
        # Reason: never echo the exception body - a connection string with a
        # password can appear in psycopg error text.
        typer.echo(f"Could not connect or query: {type(exc).__name__}", err=True)
        raise typer.Exit(2) from None

    if json_output:
        typer.echo(jsonlib.dumps([asdict(f) for f in findings], indent=2))
    else:
        if not findings:
            typer.echo(f"No findings in schema(s): {', '.join(schema)}")
        else:
            order = {"ERROR": 0, "WARN": 1, "INFO": 2}
            for f in sorted(findings, key=lambda x: (order.get(x.severity, 9), x.rule, x.object)):
                typer.echo(f"[{f.severity:5}] {f.rule:24} {f.object}\n          {f.detail}")
            counts: dict[str, int] = {}
            for f in findings:
                counts[f.severity] = counts.get(f.severity, 0) + 1
            typer.echo("\n" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
            typer.echo(
                "\nThis is not a substitute for negative tests: assert denial with the "
                "publishable key alone and with a second tenant's JWT."
            )

    raise typer.Exit(1 if findings else 0)


if __name__ == "__main__":
    app()
