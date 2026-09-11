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

from ai_accounting.kernel.backup import create_portable, verify_portable
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.http import create_server
from ai_accounting.kernel.jobs import JobRunner
from ai_accounting.kernel.security.credentials import InMemoryCredentialStore
from ai_accounting.kernel.security.native import NativeSecurityController
from ai_accounting.kernel.service import LocalService, default_registry

PASSWORD = SecretStr("Synthetic-resident-owner-123")
TAXPAYER = "91310000123456789A"


@pytest.fixture
def resident(tmp_path):
    service = LocalService(tmp_path / "root")
    service.security.provision("owner", PASSWORD)
    store = InMemoryCredentialStore()
    windows = []
    service.security_controller = NativeSecurityController(
        service.security, credential_store=store, window_opener=windows.append
    )
    static = tmp_path / "static"
    static.mkdir()
    (static / "local.html").write_text("<html>synthetic application surface</html>")
    server, capability = create_server(service, static_directory=static)
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
    other = Catalog(tmp_path / "different-root", default_registry())
    write_protected_json(
        other.root / ".service.json",
        {
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
    catalog = Catalog(root, default_registry())
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
        JobRunner(Catalog(catalog.root, default_registry())).run_once()
    failed = job(store)
    assert failed["status"] == "failed" and failed["attempts"] == 3
    assert failed["last_error"]
    assert failed["result"] is None


def test_interrupted_last_attempt_becomes_visible_failure_without_retry(tmp_path):
    catalog, _, store = make_company(tmp_path / "root")
    schedule_backup(store, tmp_path / "backups", status="running", attempts=3)
    JobRunner(catalog).run_once()
    failed = job(store)
    assert failed["status"] == "failed" and failed["attempts"] == 3
    assert failed["last_error"] == "interrupted_retry_exhausted"
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
from ai_accounting.kernel.service import default_registry
root,kind,stage,archive = sys.argv[1:]
def crash(point):
    if point == stage:
        os._exit(76)
catalog = Catalog(root, default_registry(), fault=crash)
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
    recovered = Catalog(root, default_registry())
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
    assert Catalog(root, default_registry()).companies() == companies
