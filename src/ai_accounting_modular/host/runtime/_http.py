"""Loopback transport: private local commands and same-origin read-only dashboard."""

from __future__ import annotations

import json
import secrets
import shutil
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from ai_accounting_modular.contracts.public import ContractError, loads_json

from ._state import MAX_REQUEST_BYTES, PROTOCOL


def create_server(runtime, *, port=0, static_directory=None):
    static = Path(static_directory).resolve() if static_directory else None
    cookie_scope = PROTOCOL.replace("/", "-") + "-" + runtime.catalog_id
    session_cookie, surface_cookie = cookie_scope + "-session", cookie_scope + "-surface"

    class Handler(BaseHTTPRequestHandler):
        # Each request has a finite lifetime; idle browser connections must not hold shutdown.
        protocol_version = "HTTP/1.0"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_):
            # Requests contain source locators or one-time tickets; never write them to logs.
            pass

        def _reply(self, status, raw=b"", headers=None, cookies=()):
            self.send_response(status)
            for key, value in (headers or {}).items():
                if key.lower() not in {"content-length", "cache-control", "x-content-type-options"}:
                    self.send_header(key, value)
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def _json(self, status, result, *, cookies=()):
            self._reply(
                status,
                json.dumps(result, ensure_ascii=False).encode(),
                {"Content-Type": "application/json; charset=utf-8"},
                cookies,
            )

        def _error(self, error):
            status = 401 if error.code == "owner_session_required" else 400
            if error.code in {"not_found", "unsupported_business"}:
                status = 404
            elif error.code in {"internal_error", "storage_corrupt"}:
                status = 500
            self._json(status, error.to_response())

        def _guard(self, *, private=False, post=False):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != origin.removeprefix("http://"):
                raise ContractError("invalid_origin", "本机地址与服务不一致。")
            if private:
                capability = self.headers.get("X-Modular-Capability", "")
                if self.headers.get("Origin") is not None or not secrets.compare_digest(
                    capability, runtime.capability
                ):
                    raise ContractError("owner_session_required", "缺少本机私有连接。")
            elif (
                self.headers.get("Origin") not in (origin, None)
                or post
                and self.headers.get("Origin") != origin
            ):
                raise ContractError("invalid_origin", "仅接受同源页面请求。")
            if runtime.stopping:
                raise ContractError("busy", "后台正在退出，请稍后重新连接。")

        def _body(self):
            if (
                self.headers.get("Transfer-Encoding")
                or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json"
            ):
                raise ContractError("invalid_input", "请求须为有明确长度的 JSON。")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise ContractError("invalid_input", "请求长度不正确。") from None
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ContractError("batch_too_large", "请求内容为空或超过本机入口限制。")
            body = self.rfile.read(length)
            value = loads_json(body)
            if not isinstance(value, dict):
                raise ContractError("invalid_input", "请求须为一个 JSON 对象。")
            return value

        def _cookie(self, name):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                return None
            return cookie[name].value if name in cookie else None

        def _token(self):
            with runtime.session_lock:
                return runtime.browser_sessions.get(self._cookie(session_cookie))

        def _cookie_value(self, key, value):
            return f"{key}={value}; HttpOnly; SameSite=Strict; Path=/"

        def _security(self, body):
            if set(body) - {"operation", "payload"} or "operation" not in body:
                raise ContractError("invalid_input", "本机身份请求不正确。")
            surface = self._cookie(surface_cookie)
            with runtime.session_lock:
                if surface not in runtime.browser_surfaces:
                    raise ContractError("owner_session_required", "请通过本机入口重新打开页面。")
            operation, payload = body["operation"], body.get("payload", {})
            result = runtime.security.public(operation, payload, token=self._token())
            cookies = []
            with runtime.session_lock:
                if operation == "request":
                    runtime.browser_flows[surface] = result["request_id"]
                elif (
                    operation == "status"
                    and result.get("login_completed")
                    and result.get("status") == "succeeded"
                    and runtime.browser_flows.get(surface) == payload.get("request_id")
                ):
                    cookie = secrets.token_urlsafe(32)
                    runtime.browser_sessions[cookie] = runtime.security.token()
                    runtime.browser_flows.pop(surface, None)
                    cookies.append(self._cookie_value(session_cookie, cookie))
            self._json(200, result, cookies=cookies)

        def _dashboard(self, response):
            if hasattr(response.body, "read"):
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                shutil.copyfileobj(response.body, self.wfile, 65536)
            else:
                raw = json.dumps(response.body, ensure_ascii=False).encode()
                self._reply(
                    response.status,
                    raw,
                    {
                        key: value
                        for key, value in response.headers.items()
                        if key.lower() != "content-length"
                    },
                )

        def do_POST(self):
            try:
                path = urlsplit(self.path).path
                private = path.startswith("/private/")
                # browser-session consumes the launch ticket, before a browser surface exists.
                self._guard(private=private, post=not private)
                body = self._body()
                if private:
                    result = runtime.private(path, body)
                    self._json(
                        200,
                        {"protocol": PROTOCOL, "catalog_id": runtime.catalog_id, "result": result},
                    )
                elif path == "/api/browser-session":
                    if set(body) != {"ticket"} or not isinstance(body["ticket"], str):
                        raise ContractError("invalid_input", "页面票据不正确。")
                    with runtime.session_lock:
                        ticket = runtime.browser_tickets.pop(body["ticket"], None)
                        if ticket is None or ticket[1] <= time.monotonic():
                            raise ContractError(
                                "owner_session_required", "页面票据已失效，请重新打开页面。"
                            )
                        surface = secrets.token_urlsafe(32)
                        runtime.browser_surfaces.add(surface)
                        cookies = [self._cookie_value(surface_cookie, surface)]
                        authenticated = False
                        if ticket[0] is not None:
                            runtime.security.authorize(ticket[0])
                            cookie = secrets.token_urlsafe(32)
                            runtime.browser_sessions[cookie] = ticket[0]
                            cookies.append(self._cookie_value(session_cookie, cookie))
                            authenticated = True
                    self._json(
                        200, {"status": "ready", "authenticated": authenticated}, cookies=cookies
                    )
                elif path == "/api/security-request":
                    self._security(body)
                else:
                    runtime.security.authorize(self._token())
                    self._dashboard(runtime.kernel.dashboard.post(path, body))
                    runtime.wake()
            except ContractError as error:
                self.close_connection = True
                self._error(error)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as error:
                self.close_connection = True
                self._error(runtime.failure(error))

        def do_GET(self):
            try:
                self._guard()
                target = urlsplit(self.path)
                if target.path.startswith("/api/"):
                    runtime.security.authorize(self._token())
                    with runtime.kernel.dashboard.get(
                        target.path, parse_qsl(target.query, keep_blank_values=True)
                    ) as response:
                        self._dashboard(response)
                    runtime.wake()
                    return
                if static is None:
                    raise ContractError("not_found", "当前包没有已构建的看板文件。")
                name = unquote(target.path).lstrip("/")
                path = (static / name).resolve()
                if not path.is_relative_to(static) or "\x00" in name:
                    raise ContractError("not_found", "文件不存在。")
                if not path.is_file():
                    if "." in Path(name).name:
                        raise ContractError("not_found", "文件不存在。")
                    path = static / "index.html"
                if not path.is_file():
                    raise ContractError("not_found", "当前包没有已构建的看板文件。")
                import mimetypes

                self._reply(
                    200,
                    path.read_bytes(),
                    {
                        "Content-Type": mimetypes.guess_type(path.name)[0]
                        or "application/octet-stream"
                    },
                )
            except ContractError as error:
                self._error(error)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                self._error(runtime.failure(error))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = False
    return server
