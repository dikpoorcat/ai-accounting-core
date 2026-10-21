"""One independent modular resident assembled from the existing public owners."""

from __future__ import annotations

import secrets
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from ai_accounting_modular.agent.public import create_agent
from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.public import create_kernel

from . import _companies, _http, _state
from ._background import Background
from ._security import Security

__all__ = ["Runtime", "create_runtime"]


class Runtime:
    def __init__(self, catalog_path, *, port=0, static_directory=None, window_opener=None):
        self.catalog_path = _state.catalog_path(Path(catalog_path))
        self.kernel = create_kernel(self.catalog_path)
        self.catalog_id = self.kernel.storage.catalog_identity()
        self.agent = create_agent(self.kernel.application)
        self.capability = secrets.token_urlsafe(32)
        self.security = Security(self.kernel.storage, window_opener=window_opener)
        self.session_lock = threading.RLock()
        self.browser_tickets, self.browser_sessions, self.browser_flows = {}, {}, {}
        self.browser_surfaces = set()
        self.stopping = False
        self._lifetime = ExitStack()
        self._started = False
        static = static_directory or Path(__file__).resolve().parents[2] / "static/dashboard"
        self.server = _http.create_server(self, port=port, static_directory=static)
        self.background = Background(self)
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="modular-http", daemon=False
        )
        if window_opener is None:
            from ._native import launch

            self.security.window_opener = lambda request_id: launch(
                request_id,
                port=self.server.server_port,
                capability=self.capability,
                catalog_id=self.catalog_id,
            )

    @property
    def metadata(self):
        return {
            "protocol": _state.PROTOCOL,
            "catalog_id": self.catalog_id,
            "catalog_path": str(self.catalog_path),
            "port": self.server.server_port,
            "capability": self.capability,
        }

    def info(self):
        return {key: value for key, value in self.metadata.items() if key != "capability"}

    def start(self):
        if self._started or self.stopping:
            raise ContractError("busy", "此后台已经启动或退出。")
        try:
            self._lifetime.enter_context(_state.instance_lock(self.catalog_path))
            self.thread.start()
            self._started = True
            self.background.start()
            _state.publish(self.catalog_path, self.metadata)
        except BaseException:
            self.close()
            raise
        return self

    def wake(self):
        self.background.wake_event.set()

    def browser_url(self):
        try:
            token = self.security.token()
        except ContractError:
            token = None
        ticket = secrets.token_urlsafe(32)
        with self.session_lock:
            import time

            now = time.monotonic()
            self.browser_tickets = {
                key: value for key, value in self.browser_tickets.items() if value[1] > now
            }
            self.browser_tickets[ticket] = (token, now + 60)
        return f"http://127.0.0.1:{self.server.server_port}/#ticket={ticket}"

    @staticmethod
    def failure(error):
        if isinstance(error, ContractError):
            return error
        diagnostic = str(uuid4())
        # Never stringify an exception: file paths, payloads or secrets may be embedded.
        print(f"modular diagnostic {diagnostic}: {type(error).__name__}", file=sys.stderr)
        return ContractError(
            "internal_error", "本机处理未完成，请凭诊断编号核查。", diagnostic_id=diagnostic
        )

    def private(self, path, body):
        if path == "/private/info" and not body:
            return self.info()
        if path == "/private/contracts" and not body:
            return self.agent.contracts()
        if path == "/private/security" and set(body) == {"operation", "payload"}:
            return self.security.public(body["operation"], body["payload"], local=True)
        if path == "/private/native" and set(body) == {"operation", "payload"}:
            return self.security.native(body["operation"], body["payload"])
        if path == "/private/browser-ticket" and not body:
            return {"url": self.browser_url()}
        if path == "/private/stop" and not body:
            threading.Thread(target=self.close, name="modular-stop", daemon=False).start()
            return {"status": "stopping"}
        if path in {"/private/company-create", "/private/company-register"}:
            self.security.authorize(local=True)
            operation = _companies.create if path.endswith("create") else _companies.register
            result = operation(self.kernel.storage, body)
            self.wake()
            return result
        if path == "/private/command" and set(body) == {"command", "payload"}:
            if body["command"] != "accounting_schema":
                self.security.authorize(local=True)
            try:
                return self.agent.dispatch(body["command"], body["payload"])
            finally:
                self.wake()
        raise ContractError("not_found", "没有此本机入口。")

    def close(self):
        if self.stopping:
            return
        self.stopping = True
        try:
            if self._started:
                self.server.shutdown()
                self.thread.join()
            self.server.server_close()
            self.background.close()
            self.security.close()
            self.kernel.close()
            if self._started:
                try:
                    if _state.read(self.catalog_path).get("capability") == self.capability:
                        _state.state_path(self.catalog_path).unlink(missing_ok=True)
                except (ContractError, OSError):
                    pass
        finally:
            self._lifetime.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


def create_runtime(catalog_path, *, port=0, static_directory=None, window_opener=None) -> Runtime:
    """Bind the explicit catalog and loopback port; start and shutdown remain explicit."""
    return Runtime(
        catalog_path, port=port, static_directory=static_directory, window_opener=window_opener
    )
