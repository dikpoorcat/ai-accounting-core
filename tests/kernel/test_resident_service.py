"""Real loopback requests and process restarts; every identity/company is synthetic."""

from __future__ import annotations

import ctypes
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from ai_accounting.kernel.backup import create_portable, restore_portable, verify_portable
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.daemon import build_native_security_controller
from ai_accounting.kernel.http import create_server
from ai_accounting.kernel.integrity import verify_close_integrity, verify_integrity
from ai_accounting.kernel.jobs import JobRunner
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.read_state import advance_repair_revision
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.security import IdentityError, consume_close_approval
from ai_accounting.kernel.security.credentials import InMemoryCredentialStore
from ai_accounting.kernel.security.windows import read_protected_json, write_protected_json
from ai_accounting.kernel.service import LocalService
from ai_accounting.kernel.types import canonical, digest

PASSWORD = SecretStr("Synthetic-resident-owner-123")
TAXPAYER = "91310000123456789A"


@pytest.fixture
def resident(tmp_path):
    service = LocalService(tmp_path / "root")
    service.security.provision("owner", PASSWORD)
    store = InMemoryCredentialStore()
    windows = []
    static = tmp_path / "static"
    static.mkdir()
    (static / "local.html").write_text("<html>synthetic application surface</html>")
    server, capability = create_server(service, static_directory=static)
    service.security_controller = build_native_security_controller(
        service,
        server,
        capability,
        credential_store=store,
        window_opener=windows.append,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class HTTP:
        origin = f"http://127.0.0.1:{server.server_port}"

        def request(self, path, payload=None, *, method=None, headers=None, raw=None):
            body = (
                raw
                if raw is not None
                else json.dumps(payload).encode()
                if payload is not None
                else None
            )
            headers = dict(headers or {})
            if body is not None:
                headers.setdefault("Content-Type", "application/json")
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request(
                    method or ("POST" if body is not None else "GET"), path, body, headers
                )
                response = connection.getresponse()
                data = response.read()
                cookies = SimpleCookie()
                for key, value in response.getheaders():
                    if key.lower() == "set-cookie":
                        cookies.load(value)
                return (
                    response.status,
                    dict(response.headers),
                    cookies,
                    json.loads(data) if path.startswith("/api/") else data,
                )
            finally:
                connection.close()

        def ticket(self, token=None):
            headers = {"X-Local-Capability": capability}
            if token is not None:
                headers["Authorization"] = "Bearer " + token.get_secret_value()
            status, _, _, result = self.request("/api/browser-ticket", {}, headers=headers)
            assert status == 200
            return parse_qs(urlsplit(result["url"]).fragment)["ticket"][0]

        def surface(self, token=None):
            status, _, cookies, result = self.request(
                "/api/browser-session", {"ticket": self.ticket(token)}
            )
            assert status == 200
            return cookies, result

        def browser(self, operation, payload, cookies):
            return self.request(
                "/api/security-request",
                {"operation": operation, "payload": payload},
                headers={"Origin": self.origin, "Cookie": cookie_header(cookies)},
            )

        def native(self, operation, **payload):
            return self.request(
                "/api/security",
                {"operation": operation, **payload},
                headers={"X-Local-Capability": capability},
            )

    try:
        yield service, server, capability, HTTP(), windows
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def cookie_header(cookies):
    return "; ".join(f"{name}={value.value}" for name, value in cookies.items())


def test_ticket_surface_native_login_and_httponly_browser_session(resident):
    service, _, _, http, windows = resident
    cookies, result = http.surface()
    assert not result["authenticated"]
    assert "finance_surface" in cookies and "finance_session" not in cookies
    assert cookies["finance_surface"]["httponly"]
    assert cookies["finance_surface"]["samesite"] == "Strict"
    status, _, window_cookie, request = http.browser("request", {"kind": "login"}, cookies)
    assert status == 200 and windows == [request["request_id"]]
    cookies.update(window_cookie)
    status, _, _, response = http.native(
        "native_execute", request_id=request["request_id"], password=PASSWORD.get_secret_value()
    )
    assert status == 200 and response["login_completed"]
    http.native("native_update", request_id=request["request_id"], status="succeeded")
    status, _, session_cookie, result = http.browser(
        "status", {"request_id": request["request_id"]}, cookies
    )
    assert status == 200 and result["browser_authenticated"]
    session = session_cookie["finance_session"]
    assert session["httponly"] and session["samesite"] == "Strict"
    assert (
        session.value != service.security_controller.store.load_session_token().get_secret_value()
    )
    cookies.update(session_cookie)
    assert (
        http.request("/api/local/companies", headers={"Cookie": cookie_header(cookies)})[0] == 200
    )
    assert http.browser("session_status", {}, cookies)[3]["authenticated"]
    # A second launcher surface does not inherit the first browser session.
    second, _ = http.surface()
    assert not http.browser("session_status", {}, second)[3]["authenticated"]
    assert (
        "finance_session"
        not in http.browser("status", {"request_id": request["request_id"]}, second)[2]
    )
    service.security.logout(service.security_controller.store.load_session_token())
    assert (
        http.request("/api/local/companies", headers={"Cookie": cookie_header(cookies)})[0] == 401
    )
    assert not http.browser("session_status", {}, cookies)[3]["authenticated"]


def test_authenticated_ticket_is_single_use_and_expired_ticket_creates_no_surface(resident):
    service, server, _, http, _ = resident
    token = service.security.login("owner", PASSWORD).session_token
    ticket = http.ticket(token)
    status, _, cookies, body = http.request("/api/browser-session", {"ticket": ticket})
    assert status == 200 and body["authenticated"] and cookies["finance_session"]["httponly"]
    assert http.request("/api/browser-session", {"ticket": ticket})[0] == 401
    expired = http.ticket()
    server.browser_tickets[expired] = (None, time.monotonic() - 1)
    before = len(server.browser_surfaces)
    status, _, cookies, _ = http.request("/api/browser-session", {"ticket": expired})
    assert status == 401 and not cookies
    assert len(server.browser_surfaces) == before


def test_security_origin_surface_and_private_capability_are_separate_boundaries(resident):
    _, _, capability, http, windows = resident
    public = {"operation": "request", "payload": {"kind": "login"}}
    assert http.request("/api/security-request", public)[0] == 403
    assert (
        http.request("/api/security-request", public, headers={"Origin": http.origin})[3]["code"]
        == "launcher_required"
    )
    cookies, _ = http.surface()
    assert (
        http.request(
            "/api/security-request",
            public,
            headers={"Origin": "https://elsewhere.test", "Cookie": cookie_header(cookies)},
        )[0]
        == 403
    )
    assert (
        http.request(
            "/api/security",
            {"operation": "request", "kind": "login"},
            headers={"Cookie": cookie_header(cookies)},
        )[0]
        == 403
    )
    assert (
        http.request(
            "/api/security",
            {"operation": "request", "kind": "login"},
            headers={"X-Local-Capability": "wrong"},
        )[0]
        == 403
    )
    assert (
        http.request(
            "/api/security",
            {"operation": "request", "kind": "login"},
            headers={"X-Local-Capability": capability, "Host": "elsewhere.test"},
        )[0]
        == 403
    )
    status, _, _, error = http.browser("native_execute", {"password": "synthetic-hidden"}, cookies)
    assert status == 400 and error["status"] == "rejected"
    assert "synthetic-hidden" not in json.dumps(error)
    assert windows == []
    assert http.native("request", kind="login")[0] == 200


@pytest.mark.skipif(sys.platform != "win32", reason="Windows protected capability boundary")
def test_same_account_capability_reaches_password_and_bound_approval_without_real_window(
    resident, tmp_path, monkeypatch
):
    service, _, capability, http, windows = resident
    metadata = tmp_path / "synthetic.service.json"
    write_protected_json(metadata, {"capability": capability})
    recovered = read_protected_json(metadata)["capability"]

    status, _, _, requested = http.request(
        "/api/security",
        {"operation": "request", "kind": "login"},
        headers={"X-Local-Capability": recovered},
    )
    assert status == 200 and windows == [requested["request_id"]]
    status, _, _, logged_in = http.request(
        "/api/security",
        {
            "operation": "native_execute",
            "request_id": requested["request_id"],
            "password": PASSWORD.get_secret_value(),
        },
        headers={"X-Local-Capability": recovered},
    )
    assert status == 200 and logged_in["login_completed"]
    assert (
        http.request(
            "/api/security",
            {
                "operation": "native_update",
                "request_id": requested["request_id"],
                "status": "succeeded",
            },
            headers={"X-Local-Capability": recovered},
        )[0]
        == 200
    )

    token = service.security_controller.store.load_session_token()
    company = service.dispatch(
        "create_company",
        {"taxpayer_id": TAXPAYER, "name": "合成测试公司"},
        session_token=token,
    )
    engine = service.engine(company["id"])
    proof = engine.register_evidence(
        b"explicit synthetic no-business confirmation",
        "text/plain",
        "synthetic close confirmation",
        request_id="synthetic-close-evidence",
    )["digest"]
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-09",
            category,
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=proof,
            request_id=f"synthetic-inventory-{category}",
        )
    preview = service.dispatch(
        "preview_close",
        {
            "company_id": company["id"],
            "period": "2026-09",
            "owner_confirmation": proof,
        },
        session_token=token,
    )
    request = {
        "operation": "request",
        "kind": "approve_period_close",
        "company_id": company["id"],
        "database_id": company["database_id"],
        "period": "2026-09",
        "preview_digest": preview["digest"],
        "epochs": preview["epochs"],
    }
    status, _, _, approval_request = http.request(
        "/api/security", request, headers={"X-Local-Capability": recovered}
    )
    assert status == 200, approval_request
    assert windows[-1] == approval_request["request_id"]
    original_reauthenticate = service.security.reauthenticate

    def repair_after_password(token, password):
        authority = original_reauthenticate(token, password)
        with engine.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            advance_repair_revision(connection)
            connection.commit()
        return authority

    monkeypatch.setattr(service.security, "reauthenticate", repair_after_password)
    status, _, _, expired = http.request(
        "/api/security",
        {
            "operation": "native_execute",
            "request_id": approval_request["request_id"],
            "password": PASSWORD.get_secret_value(),
        },
        headers={"X-Local-Capability": recovered},
    )
    assert status == 400 and expired["code"] == "preview_expired"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM security_close_approval").fetchone()[0] == 0
    assert (
        http.request(
            "/api/security",
            {
                "operation": "native_update",
                "request_id": approval_request["request_id"],
                "status": "failed",
                "error_code": "PREVIEW_EXPIRED",
            },
            headers={"X-Local-Capability": recovered},
        )[0]
        == 200
    )

    monkeypatch.setattr(service.security, "reauthenticate", original_reauthenticate)
    preview = service.dispatch(
        "preview_close",
        {
            "company_id": company["id"],
            "period": "2026-09",
            "owner_confirmation": proof,
        },
        session_token=token,
    )
    request.update(preview_digest=preview["digest"], epochs=preview["epochs"])
    status, _, _, approval_request = http.request(
        "/api/security", request, headers={"X-Local-Capability": recovered}
    )
    assert status == 200, approval_request
    status, _, _, approved = http.request(
        "/api/security",
        {
            "operation": "native_execute",
            "request_id": approval_request["request_id"],
            "password": PASSWORD.get_secret_value(),
        },
        headers={"X-Local-Capability": recovered},
    )
    assert status == 200 and approved["approval_id"]
    approval_id = approved["approval_id"]
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM security_close_approval WHERE id=?", (approval_id,)
        ).fetchone()
        assert row is not None and row["consumed_at"] is None

    with service.security.authorized(token) as authority:
        invalid_bindings = [
            {"period": "2026-08", "epochs": preview["epochs"]},
            {
                "period": "2026-09",
                "epochs": {**preview["epochs"], "accounting": preview["epochs"]["accounting"] + 1},
            },
        ]
        for binding in invalid_bindings:
            with engine.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                with pytest.raises(IdentityError, match="IDENTITY_CLOSE_APPROVAL_INVALID"):
                    consume_close_approval(
                        connection,
                        approval_id,
                        authority=authority,
                        company_id=company["id"],
                        database_id=company["database_id"],
                        period=binding["period"],
                        preview_digest=preview["digest"],
                        epochs=binding["epochs"],
                        now=service.security.now(),
                    )
                connection.rollback()

    closed = service.dispatch(
        "close",
        {
            "company_id": company["id"],
            "period": "2026-09",
            "owner_confirmation": proof,
            "preview_digest": preview["digest"],
            "epochs": preview["epochs"],
            "approval_id": approval_id,
            "request_id": "synthetic-close",
        },
        session_token=token,
    )
    assert closed["status"] == "closed"
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT consumed_at FROM security_close_approval WHERE id=?", (approval_id,)
        ).fetchone()
        assert row["consumed_at"] is not None
        assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 1
        connection.execute("BEGIN")
        assert verify_integrity(engine, connection)["status"] == "verified"

    portable = create_portable(
        engine.store.path,
        tmp_path / "approved-close-backup",
        request_id="approved-close-backup",
    )
    assert verify_portable(portable["path"])["latest_closed_period"] == "2026-09"
    restored = tmp_path / "approved-close-restored.sqlite"
    assert (
        restore_portable(
            portable["path"],
            restored,
            expected_company_id=company["id"],
            expected_taxpayer_id=TAXPAYER,
            expected_database_id=company["database_id"],
        )["latest_closed_period"]
        == "2026-09"
    )

    with engine.store.connection(read_only=True) as connection:
        original_manifest = json.loads(
            connection.execute("SELECT manifest FROM period_close").fetchone()["manifest"]
        )

    def replace_close_manifest(manifest):
        with engine.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            triggers = list(
                connection.execute(
                    "SELECT name,sql FROM sqlite_schema "
                    "WHERE type='trigger' AND tbl_name IN ('period_close','read_index_source')"
                )
            )
            for name, _ in triggers:
                connection.execute(f'DROP TRIGGER "{name}"')
            connection.execute(
                "UPDATE period_close SET manifest=?,digest=?",
                (canonical(manifest), digest(manifest)),
            )
            connection.execute(
                "UPDATE read_index_source SET source_digest=? WHERE source_kind='close'",
                (digest(manifest),),
            )
            for _, sql in triggers:
                connection.execute(sql)
            connection.commit()

    forged = json.loads(canonical(original_manifest))
    forged["approval"]["owner_id"] = "forged-owner"
    replace_close_manifest(forged)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as failure:
            verify_integrity(engine, connection)
    assert failure.value.details["reason"] == "close_approval_mismatch"

    missing = json.loads(canonical(original_manifest))
    missing["approval"] = None
    replace_close_manifest(missing)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as failure:
            verify_close_integrity(engine, connection, "2026-09")
    assert failure.value.details["reason"] == "orphaned_close_approval"

    replace_close_manifest(original_manifest)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        triggers = list(
            connection.execute(
                "SELECT name,sql FROM sqlite_schema "
                "WHERE type='trigger' AND tbl_name='security_close_approval'"
            )
        )
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            "UPDATE security_close_approval SET period=period-1 WHERE id=?", (approval_id,)
        )
        for _, sql in triggers:
            connection.execute(sql)
        connection.commit()
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as failure:
            verify_integrity(engine, connection)
    assert failure.value.details["reason"] == "close_approval_mismatch"


def test_direct_private_password_failures_share_persistent_throttle(resident):
    service, _, _, http, _ = resident
    request = http.native("request", kind="login")[3]
    for _ in range(5):
        status, _, _, result = http.native(
            "native_execute",
            request_id=request["request_id"],
            password="wrong-synthetic-password",
        )
        assert status == 400 and result["code"] == "IDENTITY_AUTHENTICATION_FAILED"
    status, _, _, result = http.native(
        "native_execute",
        request_id=request["request_id"],
        password=PASSWORD.get_secret_value(),
    )
    assert status == 400 and result["code"] == "IDENTITY_AUTHENTICATION_FAILED"
    with service.security._transaction() as connection:
        owner = connection.execute(
            "SELECT password_failures,password_blocked_until FROM security_owner WHERE singleton=1"
        ).fetchone()
        blocked = connection.execute(
            "SELECT count(*) FROM security_audit WHERE outcome='blocked' "
            "AND reason='AUTHENTICATION_THROTTLED'"
        ).fetchone()[0]
    assert owner["password_failures"] == 5 and owner["password_blocked_until"] is not None
    assert blocked == 1


def test_shutdown_requires_capability_and_valid_origin_without_owner_password(resident):
    service, server, capability, http, _ = resident
    token = service.security.login("owner", PASSWORD).session_token
    assert http.request("/api/shutdown", {})[0] == 403
    assert (
        http.request(
            "/api/shutdown",
            {},
            headers={
                "Authorization": "Bearer " + token.get_secret_value(),
            },
        )[0]
        == 403
    )
    assert (
        http.request(
            "/api/shutdown",
            {},
            headers={
                "X-Local-Capability": capability,
                "Origin": "https://elsewhere.test",
            },
        )[0]
        == 403
    )
    assert (
        http.request(
            "/api/shutdown",
            {"unexpected": True},
            headers={
                "X-Local-Capability": capability,
            },
        )[0]
        == 400
    )
    status, _, _, result = http.request(
        "/api/shutdown",
        {},
        headers={
            "X-Local-Capability": capability,
        },
    )
    assert status == 200 and result == {"status": "stopping"}
    assert server._BaseServer__is_shut_down.wait(3)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows resident process and protected state")
def test_stale_build_is_stopped_and_replaced_by_actual_resident_process(resident):
    from ai_accounting.kernel.daemon import ensure_service, stop_service
    from ai_accounting.kernel.security.windows import write_protected_json

    service, server, capability, _, _ = resident
    root = service.catalog.root
    server.build_id = "synthetic-previous-build"
    write_protected_json(
        root / ".service.json",
        {
            "protocol": 2,
            "database_format": service.catalog.database_format(),
            "pid": os.getpid(),
            "port": server.server_port,
            "capability": capability,
            "catalog_id": service.security.catalog_instance_id,
            "build_id": server.build_id,
        },
    )
    metadata = None
    try:
        metadata = ensure_service(root)
        assert metadata["pid"] != os.getpid()
        assert metadata["catalog_id"] == service.security.catalog_instance_id
        assert metadata["build_id"] != "synthetic-previous-build"
        assert server._BaseServer__is_shut_down.is_set()
    finally:
        if metadata is not None:
            kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x00100000, False, metadata["pid"])
            try:
                assert stop_service(root)["status"] in {"stopping", "stopped"}
                assert not handle or kernel.WaitForSingleObject(handle, 10_000) == 0
            finally:
                if handle:
                    kernel.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Protected Windows service metadata")
@pytest.mark.parametrize("action", ["ensure_service", "stop_service"])
def test_copied_service_metadata_cannot_select_or_stop_another_catalog(resident, tmp_path, action):
    from ai_accounting.kernel import daemon
    from ai_accounting.kernel.security.windows import write_protected_json

    service, server, capability, http, _ = resident
    other = Catalog(tmp_path / "different-root", production_bundle())
    write_protected_json(
        other.root / ".service.json",
        {
            "protocol": 2,
            "database_format": service.catalog.database_format(),
            "pid": os.getpid(),
            "port": server.server_port,
            "capability": capability,
            "catalog_id": service.security.catalog_instance_id,
            "build_id": server.build_id,
        },
    )
    with pytest.raises(KernelError) as error:
        getattr(daemon, action)(other.root)
    assert error.value.code == "service_identity_mismatch"
    assert http.request("/api/health", headers={"X-Local-Capability": capability})[0] == 200


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b'{"password":"never-echo",', "malformed_json"),
        (b"[]", "invalid_command"),
        (b"null", "invalid_command"),
        (b'{"operation":"status","request_id":[]}', "IDENTITY_REQUEST_INVALID"),
    ],
)
def test_malformed_security_json_returns_stable_redacted_errors(resident, raw, expected):
    _, _, capability, http, _ = resident
    status, _, _, result = http.request(
        "/api/security", raw=raw, headers={"X-Local-Capability": capability}
    )
    assert status == 400 and result["status"] == "rejected"
    assert result["code"] == expected
    assert "never-echo" not in json.dumps(result)


def make_company(root):
    catalog = Catalog(root, production_bundle())
    company = catalog.create_company(TAXPAYER, "合成恢复企业")
    return catalog, company, catalog.bind(company["id"])


def schedule_backup(store, directory, *, status="pending", attempts=0):
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO jobs(id,kind,payload,status,attempts) "
            "VALUES('backup','portable_backup',?,?,?)",
            (json.dumps({"directory": str(directory)}), status, attempts),
        )


def job(store):
    with store.connection(read_only=True) as connection:
        return dict(connection.execute("SELECT * FROM jobs WHERE id='backup'").fetchone())


def test_background_start_resumes_interrupted_backup_without_foreground_command(tmp_path):
    catalog, company, store = make_company(tmp_path / "root")
    schedule_backup(store, tmp_path / "backups", status="running", attempts=1)
    runner = JobRunner(catalog, interval=0.01)
    runner.start()
    try:
        deadline = time.monotonic() + 10
        while job(store)["status"] != "succeeded":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        completed = job(store)
        assert completed["attempts"] == 2
        result = json.loads(completed["result"])
        assert verify_portable(result["path"])["identity"]["company_id"] == company["id"]
    finally:
        runner.stop()
    assert not runner.thread.is_alive()


def test_background_failures_stop_after_three_attempts_across_restart(tmp_path):
    catalog, _, store = make_company(tmp_path / "root")
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("synthetic filesystem failure")
    schedule_backup(store, blocker)
    for _ in range(5):
        JobRunner(Catalog(catalog.root, production_bundle())).run_once()
    failed = job(store)
    assert failed["status"] == "failed" and failed["attempts"] == 3
    assert failed["last_error"]
    assert failed["error_code"] == "storage_unavailable"
    assert failed["result"] is None


def test_interrupted_last_attempt_becomes_visible_failure_without_retry(tmp_path):
    catalog, _, store = make_company(tmp_path / "root")
    schedule_backup(store, tmp_path / "backups", status="running", attempts=3)
    JobRunner(catalog).run_once()
    failed = job(store)
    assert failed["status"] == "failed" and failed["attempts"] == 3
    assert failed["last_error"] == "interrupted_retry_exhausted"
    assert failed["error_code"] == "interrupted_retry_exhausted"
    assert not (tmp_path / "backups").exists()


def test_worker_does_not_mark_an_active_third_attempt_as_interrupted(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from ai_accounting.kernel import backup

    catalog, _, store = make_company(tmp_path / "root")
    schedule_backup(store, tmp_path / "backups", attempts=2)
    original = backup.create_portable
    started, release = threading.Event(), threading.Event()

    def pause(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(backup, "create_portable", pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(backup.run_backup_jobs, store.path)
        try:
            assert started.wait(5)
            JobRunner(catalog).run_once()
            assert job(store)["status"] == "running"
        finally:
            release.set()
        assert future.result(timeout=5)[0]["status"] == "succeeded"


@pytest.mark.parametrize("kind", ["create", "restore"])
@pytest.mark.parametrize(
    "stage",
    [
        "operation_recorded",
        "file_prepared",
        "file_published",
        "before_registration_commit",
        "registration_committed",
    ],
)
def test_company_operation_survives_real_process_exit(tmp_path, kind, stage):
    archive = ""
    source_company = None
    if kind == "restore":
        _, source_company, store = make_company(tmp_path / "source")
        from ai_accounting.kernel.engine import Engine

        engine = Engine(store)
        proof = engine.register_evidence(
            b"retained synthetic evidence", "text/plain", "proof", request_id="proof"
        )
        archive = create_portable(store.path, tmp_path / "backup")["path"]
    root = tmp_path / "destination"
    script = """
import os,sys
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.schema_bundle import production_bundle
root,kind,stage,archive = sys.argv[1:]
def crash(point):
    if point == stage:
        os._exit(76)
catalog = Catalog(root, production_bundle(), fault=crash)
if kind == 'create':
    catalog.create_company('91310000123456789A','合成恢复企业')
else:
    catalog.restore_company(archive,taxpayer_id='91310000123456789A',name='合成恢复企业')
"""
    process = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", script, str(root), kind, stage, archive],
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 76, process.stderr.decode("utf-8", errors="replace")
    recovered = Catalog(root, production_bundle())
    companies = recovered.companies()
    assert len(companies) == 1 and recovered.operations()[0]["status"] == "succeeded"
    with recovered.bind(companies[0]["id"]).connection(read_only=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if kind == "restore":
            assert companies[0]["id"] == source_company["id"]
            assert (
                connection.execute(
                    "SELECT content FROM evidence WHERE digest=?", (bytes.fromhex(proof["digest"]),)
                ).fetchone()[0]
                == b"retained synthetic evidence"
            )
    assert len(list(root.glob("*/company.sqlite"))) == 1
    assert Catalog(root, production_bundle()).companies() == companies
