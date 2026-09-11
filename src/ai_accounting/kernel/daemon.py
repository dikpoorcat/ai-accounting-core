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
from pathlib import Path

from .build import calculator_build_id
from .contracts import KernelError
from .security.credentials import WindowsCredentialStore
from .security.service import credential_target
from .security.windows import read_protected_json, write_protected_json


def default_root():
    # Installed launchers pass this explicitly. Repository development shares the
    # same data root rather than creating a second root per adapter.
    return Path(os.environ.get("FINANCE_DATA_ROOT", "data")).resolve()


@contextmanager
def instance_lock(root):
    path = Path(root) / ".resident.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except ValueError:
            raise KernelError("service_protocol_error", "本地服务响应无效") from None


def _metadata_for_root(root):
    from .runtime import connect
    from .versions import verify_schema

    metadata = read_protected_json(root / ".service.json")
    path = root / "catalog.sqlite"
    if not path.is_file():
        raise KernelError("service_identity_mismatch", "本地服务与资料目录身份不一致")
    connection = connect(path, read_only=True)
    try:
        verify_schema(connection, kind="catalog")
        identity = connection.execute(
            "SELECT instance_id FROM catalog_identity WHERE id=1"
        ).fetchone()
        if identity is None or metadata.get("catalog_id") != identity[0]:
            raise KernelError("service_identity_mismatch", "本地服务与资料目录身份不一致")
    finally:
        connection.close()
    return metadata


def ensure_service(root, *, timeout=20):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
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
            if health and health.get("status") == "ready":
                if health.get("catalog_id") != metadata.get("catalog_id"):
                    raise KernelError("service_identity_mismatch", "本地服务目录身份不一致")
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
            with (root / "service.log").open("ab") as log:
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
    state = Path(root).resolve() / ".service.json"
    if not state.exists():
        return {"status": "stopped"}
    metadata = _metadata_for_root(Path(root).resolve())
    try:
        health = _request(metadata, "/api/health", timeout=1)
    except (OSError, urllib.error.URLError):
        return {"status": "stopped"}
    if health.get("catalog_id") != metadata.get("catalog_id"):
        raise KernelError("service_identity_mismatch", "本地服务目录身份不一致")
    return _request(metadata, "/api/shutdown", {})


class ServiceClient:
    def __init__(self, root=None, *, metadata=None, credential_store=None):
        self.root = Path(root or default_root()).resolve()
        self.metadata = metadata or ensure_service(self.root)
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


def run(root, *, port=0):
    from .http import create_server
    from .jobs import JobRunner
    from .security.batches import CloseBatchHost
    from .security.native import NativeSecurityController
    from .security.transport import launch_native_window
    from .service import LocalService

    root = Path(root).resolve()
    with instance_lock(root) as acquired:
        if not acquired:
            return
        service = LocalService(root)
        server, capability = create_server(service, port=port)

        # Controller owns native request state, separate from business command payloads.
        def inspect_close(request):
            company = next(
                (row for row in service.catalog.companies() if row["id"] == request["company_id"]),
                None,
            )
            if company is None:
                raise KernelError("unknown_company", "公司尚未登记")
            if request["database_id"] != company["database_id"]:
                raise KernelError("company_mismatch", "公司数据库身份不一致")
            preview = service.close_previews.get(
                (request["company_id"], request["database_id"], request["calculation_hash"])
            )
            if (
                preview is None
                or preview["manifest"]["period"] != request["period"]
                or preview["epochs"] != request["epochs"]
            ):
                raise KernelError("preview_expired", "请先在当前服务重新预览关账")
            return {"company_name": company["name"], "period_month": request["period"]}

        def issue_close(request, token, password):
            from .periods import Periods
            from .security.approval import insert_close_approval

            with service.security.authorized(token):
                inspect_close(request)
                engine = service.engine(request["company_id"])
                known = service.close_previews[
                    (request["company_id"], request["database_id"], request["calculation_hash"])
                ]
                preview = Periods(engine).preview_close(
                    request["period"], owner_confirmation=known["owner_confirmation"]
                )
                if (
                    preview["digest"] != request["calculation_hash"]
                    or preview["epochs"] != request["epochs"]
                ):
                    raise KernelError("preview_expired", "关账预览已变化，请重新预览并确认")
                authority = service.security.reauthenticate(token, password)
                with engine.store.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        if engine.store.epochs(connection) != request["epochs"]:
                            raise KernelError("preview_expired", "关账预览已变化")
                        result = insert_close_approval(
                            connection,
                            authority=authority,
                            now=service.security.now(),
                            company_id=engine.store.company_id,
                            database_id=engine.store.database_id,
                            period=request["period"],
                            preview_digest=request["calculation_hash"],
                            epochs=request["epochs"],
                        )
                        connection.commit()
                        return result
                    except BaseException:
                        connection.rollback()
                        raise

        service.security_controller = NativeSecurityController(
            service.security,
            window_opener=lambda request_id: launch_native_window(
                request_id,
                port=server.server_port,
                capability=capability,
                catalog_instance_id=service.security.catalog_instance_id,
            ),
            close_issuer=issue_close,
            inspect_close=inspect_close,
            batch_issuer=CloseBatchHost(service).issue,
            inspect_batches=CloseBatchHost(service).inspect,
        )
        metadata = {
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
