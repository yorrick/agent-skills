"""End-to-end access checks against this repository's disposable local Supabase lab."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import queue
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import psycopg
import pytest

LAB = Path(__file__).resolve().parents[1] / "supabase-security" / "lab"
DB_URL = "postgresql://postgres:postgres@127.0.0.1:58622/postgres"
API_URL = "http://127.0.0.1:58621"
USER_A = "00000000-0000-0000-0000-0000000000a1"
USER_B = "00000000-0000-0000-0000-0000000000b2"


class LabCredentials(dict[str, str]):
    def __repr__(self) -> str:
        return "<local lab credentials redacted>"


def _local_status() -> dict[str, str]:
    result = subprocess.run(
        ["supabase", "status", "-o", "json", "--workdir", str(LAB)],
        capture_output=True,
        text=True,
        check=True,
    )
    status: dict[str, str] = json.loads(result.stdout)
    # Refuse a linked or hosted project even if a caller changes the CLI context.
    assert status["API_URL"] == API_URL
    assert urlparse(status["DB_URL"]).hostname == "127.0.0.1"
    assert urlparse(status["DB_URL"]).port == 58622
    with psycopg.connect(DB_URL, connect_timeout=2) as conn:
        assert conn.execute("select current_user").fetchone() == ("postgres",)
    return LabCredentials(status)


@pytest.fixture(scope="module")
def lab() -> dict[str, str]:
    try:
        with psycopg.connect(DB_URL, connect_timeout=2):
            pass
    except psycopg.OperationalError:
        pytest.skip("the disposable local Supabase lab is not running")
    return _local_status()


def _jwt(secret: str, subject: str) -> str:
    def encode(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=").decode()

    head = encode({"alg": "HS256", "typ": "JWT"})
    body = encode({"sub": subject, "role": "authenticated", "aud": "authenticated", "exp": int(time.time()) + 3600})
    message = f"{head}.{body}"
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).rstrip(
        b"="
    )
    return f"{message}.{signature.decode()}"


def _request(
    lab: dict[str, str],
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
    service: bool = False,
    timeout: int = 8,
) -> tuple[int, bytes]:
    key = lab["SERVICE_ROLE_KEY"] if service else lab["PUBLISHABLE_KEY"]
    headers = {"apikey": key}
    if token or service:
        headers["Authorization"] = f"Bearer {token or key}"
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(f"{API_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


@pytest.fixture
def documents(lab: dict[str, str]) -> Iterator[None]:
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("drop table if exists public.lab_http_documents")
        conn.execute(
            "create table public.lab_http_documents (id int primary key, owner uuid not null, body text not null)"
        )
        conn.execute("alter table public.lab_http_documents enable row level security")
        conn.execute("grant select on public.lab_http_documents to anon, authenticated")
        conn.execute(
            "insert into public.lab_http_documents values (1, %s, 'A private'), (2, %s, 'B private')",
            (USER_A, USER_B),
        )
        conn.execute(
            "create policy lab_open on public.lab_http_documents for select to anon, authenticated using (true)"
        )
        conn.execute("notify pgrst, 'reload schema'")
    try:
        yield
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("drop table if exists public.lab_http_documents")
            conn.execute("notify pgrst, 'reload schema'")


def _secure_documents() -> None:
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("drop policy lab_open on public.lab_http_documents")
        conn.execute(
            "create policy lab_owner on public.lab_http_documents for select to authenticated "
            "using (owner = (select auth.uid()))"
        )


def _rows(lab: dict[str, str], token: str | None = None) -> list[dict[str, Any]]:
    status = 0
    for _ in range(20):
        status, payload = _request(lab, "GET", "/rest/v1/lab_http_documents?select=id,owner,body&order=id", token=token)
        if status == 200:
            return json.loads(payload)
        time.sleep(0.25)  # PostgREST refreshes its schema cache asynchronously.
    pytest.fail(f"PostgREST did not expose the fixture (HTTP {status})")


def test_postgrest_exposure_then_tenant_policy(lab: dict[str, str], documents: None) -> None:
    token_a = _jwt(lab["JWT_SECRET"], USER_A)
    token_b = _jwt(lab["JWT_SECRET"], USER_B)
    assert [row["id"] for row in _rows(lab)] == [1, 2]  # Reproduce the leak with the public key alone.
    assert [row["id"] for row in _rows(lab, token_b)] == [1, 2]

    _secure_documents()
    assert _rows(lab) == []
    assert [row["id"] for row in _rows(lab, token_a)] == [1]
    assert [row["id"] for row in _rows(lab, token_b)] == [2]


def test_storage_public_download_then_private_policy(lab: dict[str, str]) -> None:
    bucket = "lab-http-access"
    path = f"{USER_A}/canary.txt"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute(
            "insert into storage.buckets (id, name, public) values (%s, %s, true) "
            "on conflict (id) do update set public = true",
            (bucket, bucket),
        )
    try:
        status, _ = _request(
            lab,
            "POST",
            f"/storage/v1/object/{bucket}/{path}",
            body=b"owner A only",
            content_type="text/plain",
            service=True,
        )
        assert status in (200, 201), status
        status, payload = _request(lab, "GET", f"/storage/v1/object/public/{bucket}/{path}")
        assert (status, payload) == (200, b"owner A only")  # Reproduce the public bucket leak.

        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("update storage.buckets set public = false where id = %s", (bucket,))
            conn.execute("drop policy if exists lab_http_owner on storage.objects")
            conn.execute(
                "create policy lab_http_owner on storage.objects for select to authenticated "
                "using (bucket_id = 'lab-http-access' and (storage.foldername(name))[1] = (select auth.uid())::text)"
            )
        token_a = _jwt(lab["JWT_SECRET"], USER_A)
        token_b = _jwt(lab["JWT_SECRET"], USER_B)
        public_status, _ = _request(lab, "GET", f"/storage/v1/object/public/{bucket}/{path}")
        b_status, _ = _request(lab, "GET", f"/storage/v1/object/authenticated/{bucket}/{path}", token=token_b)
        a_status, a_payload = _request(lab, "GET", f"/storage/v1/object/authenticated/{bucket}/{path}", token=token_a)
        assert public_status in (400, 401, 403, 404)
        assert b_status in (400, 401, 403, 404)
        assert (a_status, a_payload) == (200, b"owner A only")
    finally:
        _request(lab, "DELETE", f"/storage/v1/object/{bucket}/{path}", service=True)
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("drop policy if exists lab_http_owner on storage.objects")
        _request(lab, "DELETE", f"/storage/v1/bucket/{bucket}", service=True)


def test_edge_function_checks_caller_before_service_role_read(lab: dict[str, str], documents: None) -> None:
    users: list[tuple[str, str]] = []
    process = subprocess.Popen(
        ["supabase", "functions", "serve", "lab-tenant", "--workdir", str(LAB)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output: queue.Queue[str] = queue.Queue()
    assert process.stdout is not None

    def drain_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output.put(line)

    reader = Thread(target=drain_output, daemon=True)
    reader.start()
    try:
        path = "/functions/v1/lab-tenant"
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("local Edge Function runtime exited before serving the fixture")
            try:
                line = output.get(timeout=1)
            except queue.Empty:
                continue
            if "Serving functions on" in line:
                break
        else:
            pytest.fail("local Edge Function runtime did not report readiness")

        status = 0
        while time.monotonic() < deadline:
            try:
                status, payload = _request(lab, "GET", path, timeout=5)
            except (TimeoutError, URLError):
                status = 0
                payload = b""
            if (status, payload) == (401, b"Authentication required"):
                break
            if process.poll() is not None:
                pytest.fail("local Edge Function runtime exited before serving the fixture")
            time.sleep(0.5)
        else:
            pytest.fail(f"local Edge Function runtime did not become ready (last HTTP {status})")

        for owner_row in (1, 2):
            email = f"lab-{uuid.uuid4()}@example.test"
            password = f"Lab-{uuid.uuid4()}"
            status, payload = _request(
                lab,
                "POST",
                "/auth/v1/admin/users",
                service=True,
                body=json.dumps({"email": email, "password": password, "email_confirm": True}).encode(),
                content_type="application/json",
            )
            assert status in (200, 201), status
            user_id = json.loads(payload)["id"]
            users.append((user_id, ""))
            with psycopg.connect(DB_URL, autocommit=True) as conn:
                conn.execute("update public.lab_http_documents set owner = %s where id = %s", (user_id, owner_row))
            status, payload = _request(
                lab,
                "POST",
                "/auth/v1/token?grant_type=password",
                body=json.dumps({"email": email, "password": password}).encode(),
                content_type="application/json",
            )
            assert status == 200, status
            users[-1] = (user_id, json.loads(payload)["access_token"])

        assert _request(lab, "GET", path) == (401, b"Authentication required")
        assert _request(lab, "GET", path, token="invalid-token") == (401, b"Authentication required")

        def edge_rows(token: str) -> list[dict[str, Any]]:
            retry_until = time.monotonic() + 30
            while time.monotonic() < retry_until:
                try:
                    status, payload = _request(lab, "GET", path, token=token, timeout=8)
                except (TimeoutError, URLError):
                    status = 502
                    payload = b""
                if status == 200:
                    return json.loads(payload)
                if status not in (502, 503, 504):
                    pytest.fail(f"Edge Function rejected an authenticated lab user (HTTP {status})")
                time.sleep(0.5)
            pytest.fail("Edge Function did not complete an authenticated read")

        a_rows = edge_rows(users[0][1])
        b_rows = edge_rows(users[1][1])
        assert [row["id"] for row in a_rows] == [1]
        assert [row["id"] for row in b_rows] == [2]
    finally:
        for user_id, _ in users:
            _request(lab, "DELETE", f"/auth/v1/admin/users/{user_id}", service=True)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        reader.join(timeout=2)


def test_realtime_public_broadcast_then_topic_policy(lab: dict[str, str]) -> None:
    topic = "lab-tenant-a"
    probe = LAB / "realtime-probe.mjs"

    def run_probe(mode: str) -> dict[str, Any]:
        request = {
            "apiUrl": API_URL,
            "apikey": lab["PUBLISHABLE_KEY"],
            "anonKey": lab["ANON_KEY"],
            "tokenA": _jwt(lab["JWT_SECRET"], USER_A),
            "tokenB": _jwt(lab["JWT_SECRET"], USER_B),
            "topic": topic,
            "mode": mode,
        }
        result = subprocess.run(
            ["node", str(probe)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    try:
        assert run_probe("public") == {"a": "ok", "b": "ok", "delivered": True}
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("drop policy if exists lab_realtime_read on realtime.messages")
            conn.execute("drop policy if exists lab_realtime_send on realtime.messages")
            conn.execute(
                "create policy lab_realtime_read on realtime.messages for select to authenticated "
                f"using (realtime.topic() = 'lab-tenant-a' and (select auth.uid()) = '{USER_A}'::uuid)"
            )
            conn.execute(
                "create policy lab_realtime_send on realtime.messages for insert to authenticated "
                f"with check (realtime.topic() = 'lab-tenant-a' and (select auth.uid()) = '{USER_A}'::uuid)"
            )
        assert run_probe("private") == {"a": "ok", "b": "error", "anon": "error", "delivered": True}
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("drop policy if exists lab_realtime_read on realtime.messages")
            conn.execute("drop policy if exists lab_realtime_send on realtime.messages")
