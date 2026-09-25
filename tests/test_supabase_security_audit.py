"""Tests for the supabase-security auditor and trigger-guard pattern.

The database tests run against the plugin's local lab and are skipped when it is not
running. Start it with:

    supabase start --workdir supabase-security/lab
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from types import ModuleType

import psycopg
import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "supabase-security"
SKILL = PLUGIN / "skills" / "supabase-security" / "SKILL.md"
GUARD_DOC = PLUGIN / "skills" / "supabase-security" / "references" / "trigger-guard-pattern.md"
LAB = PLUGIN / "lab"
LAB_DB_URL = os.environ.get("SUPABASE_LAB_DB_URL", "postgresql://postgres:postgres@127.0.0.1:58622/postgres")

USER = "00000000-0000-0000-0000-00000000000a"
ADMIN = "00000000-0000-0000-0000-00000000000b"


def load_auditor() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_rls", PLUGIN / "scripts" / "audit_rls.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_rls"] = module
    spec.loader.exec_module(module)
    return module


audit = load_auditor()


def test_every_auditor_rule_maps_to_a_skill_rule() -> None:
    headings = set(re.findall(r"^### (R\d+) ", SKILL.read_text(), flags=re.M))
    rules = {rule for _, rule, _, _ in audit.QUERIES + audit.STORAGE_QUERIES}
    prefixes = {rule.split("-")[0] for rule in rules}
    assert prefixes <= headings, prefixes - headings


def lab_available() -> bool:
    try:
        with psycopg.connect(LAB_DB_URL, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


needs_lab = pytest.mark.skipif(not lab_available(), reason="local lab not running")


def run_sql_file(path: Path) -> None:
    with psycopg.connect(LAB_DB_URL, autocommit=True) as conn:
        conn.execute(path.read_text())  # type: ignore[arg-type]


@needs_lab
def test_auditor_flags_each_fixture_and_spares_the_correct_counterparts() -> None:
    run_sql_file(LAB / "audit-fixtures.sql")
    findings, schemas = audit.run_audit(LAB_DB_URL, ["audit_fixture"], with_splinter=False)
    got = {(f.rule, f.object) for f in findings}

    expected = {
        ("R4-delete-reinsert", "audit_fixture.accounts (authenticated)"),
        ("R2-restrictive-reads-only", "audit_fixture.documents (authenticated)"),
        ("R13-auth-hook-callable", "audit_fixture.custom_access_token_hook(event jsonb)"),
        ("R14-public-bucket", "storage.buckets :: fixture-public"),
        ("R1-policy-for-all", "storage.objects :: fixture bare"),
        ("R1-policy-no-to", "storage.objects :: fixture bare"),
    }
    assert expected <= got, expected - got
    assert schemas == ["audit_fixture"]

    # Reason: a check that also fires on the correct pattern trains people to
    # ignore it, so the counterparts matter as much as the fixtures. Each is
    # checked against the rule it is the counterpart for; advisory rules such as
    # R2-no-restrictive may still, correctly, mention them.
    counterparts = {
        ("R4-delete-reinsert", "audit_fixture.ok_accounts (authenticated)"),
        ("R4-delete-reinsert", "audit_fixture.ok_rls_accounts (authenticated)"),
        ("R2-restrictive-reads-only", "audit_fixture.ok_documents (authenticated)"),
        ("R1-policy-for-all", "audit_fixture.ok_documents :: ok_documents_tenant"),
        ("R13-auth-hook-callable", "audit_fixture.ok_access_token_hook(event jsonb)"),
        ("R14-public-bucket", "storage.buckets :: fixture-private"),
    }
    assert not counterparts & got, counterparts & got


@needs_lab
def test_auditor_refuses_to_guess_exposed_schemas() -> None:
    # The local CLI passes the schema list to PostgREST as an environment
    # variable, so nothing in the database says which schemas are exposed.
    with pytest.raises(audit.SchemasNotDiscoverable):
        audit.run_audit(LAB_DB_URL, None, with_splinter=False)


def guard_migration() -> str:
    blocks = re.findall(r"^```sql\n(.*?)^```", GUARD_DOC.read_text(), flags=re.M | re.S)
    return blocks[0]


def attempt(sql: str, *, role: str | None, sub: str | None = None) -> str:
    """Run one statement as `role` inside a rolled-back transaction."""
    with psycopg.connect(LAB_DB_URL) as conn:
        try:
            if role:
                conn.execute(f"set local role {role}")  # type: ignore[arg-type]
            if sub:
                conn.execute(
                    "select set_config('request.jwt.claims', %s, true)",
                    (f'{{"sub":"{sub}","role":"authenticated"}}',),
                )
            cur = conn.execute(sql)  # type: ignore[arg-type]
            return f"rows={cur.rowcount}"
        except psycopg.errors.InsufficientPrivilege as exc:
            return f"denied: {exc.diag.message_primary}"
        finally:
            conn.rollback()


@pytest.fixture(scope="module")
def guarded_account() -> None:
    run_sql_file(LAB / "trigger-guard" / "setup.sql")
    with psycopg.connect(LAB_DB_URL, autocommit=True) as conn:
        conn.execute(guard_migration())  # type: ignore[arg-type]


@needs_lab
@pytest.mark.parametrize(
    ("case", "sql", "role", "sub", "expected"),
    [
        ("user edits allowed column", "update public.account set name = 'n'", "authenticated", USER, "rows=1"),
        (
            "user echoes guarded columns",
            "update public.account set name = 'n', plan = 'free', is_admin = false",
            "authenticated",
            USER,
            "rows=1",
        ),
        (
            "user edits guarded column",
            "update public.account set plan = 'pro'",
            "authenticated",
            USER,
            "denied: Only admins may change column(s): plan",
        ),
        (
            "user smuggles guarded column",
            "update public.account set name = 'n', is_admin = true",
            "authenticated",
            USER,
            "denied: Only admins may change column(s): is_admin",
        ),
        (
            "user deletes to re-insert",
            f"delete from public.account where id = '{USER}'",
            "authenticated",
            USER,
            "denied: permission denied for table account",
        ),
        ("admin edits guarded columns", "update public.account set plan = 'x'", "authenticated", ADMIN, "rows=2"),
        ("service_role writes", "update public.account set plan = 'x'", "service_role", None, "rows=2"),
        ("postgres job writes", "update public.account set plan = 'x'", None, None, "rows=2"),
        (
            "anon writes",
            "update public.account set name = 'n'",
            "anon",
            None,
            "denied: permission denied for table account",
        ),
        (
            "custom API role edits guarded column",
            "update public.account set plan = 'x'",
            "lab_custom_api",
            None,
            "denied: Only admins may change column(s): plan",
        ),
    ],
)
def test_trigger_guard_matrix(
    guarded_account: None, case: str, sql: str, role: str | None, sub: str | None, expected: str
) -> None:
    assert attempt(sql, role=role, sub=sub) == expected, case


@needs_lab
def test_r12_reports_builtin_function_default_until_revoked_globally() -> None:
    """PostgreSQL grants EXECUTE on new functions to PUBLIC unless a GLOBAL default replaces it."""
    sql = next(sql for _, rule, sql, _ in audit.QUERIES if rule == "R12-default-privileges")
    params = {"schemas": ["public"], "api_roles": list(audit.API_ROLES)}
    builtin = "<all schemas> (functions created by postgres)"
    with psycopg.connect(LAB_DB_URL) as conn:
        try:
            before = {row[0] for row in conn.execute(sql, params)}  # type: ignore[arg-type]
            # The per-schema form SKILL.md warns about: no effect on the built-in default.
            conn.execute("alter default privileges in schema public revoke execute on functions from public")
            unchanged = {row[0] for row in conn.execute(sql, params)}  # type: ignore[arg-type]
            conn.execute("alter default privileges revoke execute on functions from public")
            after = {row[0] for row in conn.execute(sql, params)}  # type: ignore[arg-type]
        finally:
            conn.rollback()
    assert builtin in before
    assert builtin in unchanged
    assert builtin not in after
