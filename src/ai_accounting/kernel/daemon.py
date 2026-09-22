"""Single resident service and thin local clients for one material root."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

from .build import calculator_build_id
from .catalog import ensure_catalog
from .contracts import KernelError
from .permissions import ensure_private_file, reject_reparse_path
from .runtime import private_file_lock
from .schema_bundle import production_bundle, valid_database_format
from .security.credentials import WindowsCredentialStore
from .security.service import credential_target
from .security.windows import read_protected_json, write_protected_json
from .types import digest

SERVICE_PROTOCOL = 2


def default_root():
    root = os.environ.get("FINANCE_DATA_ROOT")
    if not root:
        raise KernelError(
            "data_root_required", "请使用启动器，或明确指定 --root / FINANCE_DATA_ROOT"
        )
    return reject_reparse_path(root)


@contextmanager
def instance_lock(root):
    root = reject_reparse_path(root)
    with private_file_lock(root / ".resident.lock") as acquired:
        yield acquired


def _request(metadata, path, payload=None, *, token=None, timeout=30):
    headers = {"X-Local-Capability": metadata["capability"]}
    if token:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{metadata['port']}{path}",
        data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        if payload is not None
        else None,
        headers=headers,
    )

    # Loopback IPC must never follow a machine-wide HTTP proxy or a redirect.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def decode(response):
        try:
            result = json.load(response)
        except ValueError:
            raise KernelError("service_protocol_error", "本地服务响应无效") from None
        if path in {"/api/health", "/api/shutdown"} and not isinstance(result, dict):
            raise KernelError("service_protocol_error", "本地服务响应无效")
        return result

    try:
        with opener.open(request, timeout=timeout) as response:
            return decode(response)
    except urllib.error.HTTPError as exc:
        return decode(exc)


def _metadata_for_root(root, metadata=None):
    from .runtime import connect
    from .versions import database_format, verify_schema

    root = reject_reparse_path(root)
    path = root / "catalog.sqlite"
    if not path.is_file():
        raise KernelError("service_identity_mismatch", "本地服务与资料目录身份不一致")
    bundle = production_bundle()
    connection = connect(
        path,
        read_only=True,
        validator=lambda c: verify_schema(c, bundle=bundle, kind="catalog"),
    )
    try:
        connection.execute("BEGIN")
        actual_format = database_format(connection, bundle=bundle, kind="catalog")
        identity = connection.execute(
            "SELECT instance_id FROM catalog_identity WHERE id=1"
        ).fetchone()[0]
    finally:
        connection.close()
    if metadata is None:
        metadata = read_protected_json(root / ".service.json")
    required = {
        "protocol",
        "pid",
        "port",
        "capability",
        "catalog_id",
        "build_id",
        "database_format",
    }
    if (
        not isinstance(metadata, dict)
        or set(metadata) != required
        or type(metadata["protocol"]) is not int
        or metadata["protocol"] != SERVICE_PROTOCOL
        or type(metadata["pid"]) is not int
        or metadata["pid"] <= 0
        or type(metadata["port"]) is not int
        or not 1 <= metadata["port"] <= 65535
        or not isinstance(metadata["capability"], str)
        or not metadata["capability"]
        or not isinstance(metadata["build_id"], str)
        or not metadata["build_id"]
        or metadata["catalog_id"] != identity
        or not valid_database_format(metadata["database_format"])
        or metadata["database_format"] != actual_format
    ):
        raise KernelError("service_identity_mismatch", "本地服务协议或目录身份不一致")
    return metadata


def _check_health(metadata, health):
    if (
        not isinstance(health, dict)
        or type(health.get("protocol")) is not int
        or not valid_database_format(health.get("database_format"))
        or any(
            health.get(key) != metadata[key]
            for key in ("protocol", "catalog_id", "database_format", "build_id")
        )
    ):
        raise KernelError("service_identity_mismatch", "本地服务身份或版本不一致")


def ensure_service(root, *, timeout=20):
    root = reject_reparse_path(root)
    ensure_catalog(root)
    state = root / ".service.json"
    expected = calculator_build_id()
    started = time.monotonic()
    spawned = False
    stopping = False
    while time.monotonic() - started < timeout:
        if state.exists():
            metadata = _metadata_for_root(root)
            try:
                health = _request(metadata, "/api/health", timeout=0.75)
            except (OSError, urllib.error.URLError):
                health = None
            else:
                _check_health(metadata, health)
            if health is not None and health.get("status") == "ready":
                if health.get("build_id") != expected:
                    if not stopping:
                        result = _request(metadata, "/api/shutdown", {})
                        if result.get("status") != "stopping":
                            raise KernelError(
                                "service_version_mismatch",
                                "运行中的本地服务版本已变化，请重新启动服务",
                            )
                        stopping = True
                    time.sleep(0.1)
                    continue
                return metadata
        if not spawned:
            with instance_lock(root) as available:
                if not available:
                    time.sleep(0.1)
                    continue
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            # Only safe status is written to this log; requests and tokens are never logged.
            log_path = ensure_private_file(root / "service.log", create=True)
            with log_path.open("ab") as log:
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "ai_accounting.kernel.cli",
                        "--root",
                        str(root),
                        "daemon",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    creationflags=flags,
                    close_fds=True,
                )
            spawned = True
        time.sleep(0.1)
    raise KernelError("service_start_failed", "本地服务未能启动，请检查资料根目录的运行状态")


def stop_service(root):
    """Stop only the service proven by this root's protected capability and identity."""
    root = reject_reparse_path(root)
    state = root / ".service.json"
    if not state.exists():
        return {"status": "stopped"}
    metadata = _metadata_for_root(root)
    try:
        health = _request(metadata, "/api/health", timeout=1)
    except (OSError, urllib.error.URLError):
        return {"status": "stopped"}
    _check_health(metadata, health)
    return _request(metadata, "/api/shutdown", {})


class ServiceClient:
    def __init__(self, root=None, *, metadata=None, credential_store=None):
        self.root = reject_reparse_path(root or default_root())
        self.metadata = (
            _metadata_for_root(self.root, metadata)
            if metadata is not None
            else ensure_service(self.root)
        )
        self.credentials = credential_store or WindowsCredentialStore(
            target_name=credential_target(self.root / "catalog.sqlite", self.metadata["catalog_id"])
        )

    def _token(self):
        token = self.credentials.load_session_token()
        return token.get_secret_value() if token else None

    def dispatch(self, command, payload):
        return self._call(
            "/api/command",
            {"command": command, "payload": payload},
            timeout=300,
        )

    def _call(self, path, payload, *, timeout=30):
        try:
            return _request(self.metadata, path, payload, token=self._token(), timeout=timeout)
        except (OSError, urllib.error.URLError):
            # The stdio adapter may outlive a restarted resident. Retain the
            # original request key and retry only a newly proven same-catalogue endpoint.
            fresh = ensure_service(self.root)
            if fresh == self.metadata or fresh["catalog_id"] != self.metadata["catalog_id"]:
                raise KernelError("service_unavailable", "本地服务连接已变化，请重新连接") from None
            self.metadata = fresh
            return _request(self.metadata, path, payload, token=self._token(), timeout=timeout)

    def security(self, operation, payload):
        if operation not in {"request", "status", "cancel", "session_status"}:
            raise KernelError("private_security_operation", "密码操作只能在原生安全窗口完成")
        return self._call("/api/security", {"operation": operation, **payload})

    def browser_url(self):
        return self._call("/api/browser-ticket", {})


def build_native_security_controller(
    service, server, capability, *, credential_store=None, window_opener=None
):
    """Build the production private controller around one synthetic or resident service."""
    from .periods import Periods
    from .security.approval import insert_close_approval
    from .security.native import NativeSecurityController
    from .security.transport import launch_native_window

    def inspect_close(request):
        company = next(
            (row for row in service.catalog.companies() if row["id"] == request["company_id"]),
            None,
        )
        if company is None:
            raise KernelError("unknown_company", "公司尚未登记")
        if request["database_id"] != company["database_id"]:
            raise KernelError("company_mismatch", "公司数据库身份不一致")
        preview = service.require_active_close_preview(
            request["company_id"],
            request["database_id"],
            request["period"],
            request["preview_digest"],
        )
        if preview["epochs"] != request["epochs"]:
            raise KernelError("preview_expired", "请先在当前服务重新预览关账")
        return {
            "company_name": company["name"],
            "period_month": request["period"],
            "owner_review": preview["manifest"]["owner_review"],
        }

    def issue_close(request, token, password):
        with service.security.authorized(token):
            engine = service.engine(request["company_id"])
            known = service.require_active_close_preview(
                request["company_id"],
                request["database_id"],
                request["period"],
                request["preview_digest"],
            )
            if known["epochs"] != request["epochs"]:
                raise KernelError("preview_expired", "关账预览已变化，请重新预览并确认")
            authority = service.security.reauthenticate(token, password)
            with engine.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # The password may have remained open while a repair changed
                    # immutable-source interpretation without advancing a business
                    # lane.  Rebuild the complete manifest under the company write
                    # lock and compare its digest, including read_repair_revision.
                    active = service.require_active_close_preview(
                        request["company_id"],
                        request["database_id"],
                        request["period"],
                        request["preview_digest"],
                    )
                    manifest = Periods(engine)._manifest(
                        connection,
                        request["period"],
                        active["owner_confirmation"],
                    )
                    current = engine.store.epochs(connection)
                    preview_digest = digest(
                        [
                            manifest,
                            current["accounting"],
                            current["material"],
                            current["management"],
                        ]
                    ).hex()
                    if preview_digest != request["preview_digest"] or current != request["epochs"]:
                        raise KernelError("preview_expired", "关账预览已变化")
                    result = insert_close_approval(
                        connection,
                        authority=authority,
                        now=service.security.now(),
                        company_id=engine.store.company_id,
                        database_id=engine.store.database_id,
                        period=request["period"],
                        preview_digest=request["preview_digest"],
                        epochs=request["epochs"],
                    )
                    connection.commit()
                    return result
                except BaseException:
                    connection.rollback()
                    raise

    opener = window_opener or (
        lambda request_id: launch_native_window(
            request_id,
            port=server.server_port,
            capability=capability,
            catalog_instance_id=service.security.catalog_instance_id,
        )
    )
    return NativeSecurityController(
        service.security,
        credential_store=credential_store,
        window_opener=opener,
        close_issuer=issue_close,
        inspect_close=inspect_close,
    )


def run(root, *, port=0):
    from .http import create_server
    from .jobs import JobRunner
    from .service import LocalService

    root = reject_reparse_path(root)
    ensure_catalog(root)
    with instance_lock(root) as acquired:
        if not acquired:
            return
        service = LocalService(root)
        server, capability = create_server(service, port=port)

        # Controller owns native request state, separate from business command payloads.
        service.security_controller = build_native_security_controller(service, server, capability)
        metadata = {
            "protocol": SERVICE_PROTOCOL,
            "database_format": service.catalog.database_format(),
            "pid": os.getpid(),
            "port": server.server_port,
            "capability": capability,
            "catalog_id": service.security.catalog_instance_id,
            "build_id": server.build_id,
        }
        write_protected_json(root / ".service.json", metadata)
        runner = JobRunner(service.catalog)
        runner.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
            runner.stop()
