"""Loopback dashboard reads, native login, and narrowly scoped report export tasks."""

from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from .build import calculator_build_id
from .diagnostics import error_response
from .security.primitives import IdentityError


def wire_money(value, key="", parent=""):
    if (
        type(value) is int
        and (key in {"debit", "credit", "total", "amount"} or key.endswith("_fen"))
        and (parent, key) != ("checks", "total")
    ):
        return str(value)
    if isinstance(value, dict):
        return {name: wire_money(item, name, key) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire_money(item) for item in value]
    return value


def dashboard_directory():
    packaged = Path(__file__).parents[1] / "static" / "dashboard"
    checkout = Path(__file__).parents[3] / "frontend" / "dist"
    return (packaged if (packaged / "local.html").is_file() else checkout).resolve()


def create_server(service, *, port=0, static_directory=None, token=None):
    secret = token or secrets.token_urlsafe(32)
    static = Path(static_directory or dashboard_directory()).resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Request URLs may contain business identifiers. The launcher prints only its own URL.
            pass

        def reply(
            self,
            status,
            body,
            content_type="application/json; charset=utf-8",
            *,
            cookie=None,
            filename=None,
        ):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if filename:
                self.send_header(
                    "Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename)
                )
            if cookie:
                for value in [cookie] if isinstance(cookie, str) else cookie:
                    self.send_header("Set-Cookie", value)
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def json_reply(self, status, result, **kwargs):
            self.reply(status, json.dumps(result, ensure_ascii=False).encode("utf-8"), **kwargs)

        def query_payload(self, query):
            parameters = parse_qs(query, strict_parsing=True, keep_blank_values=True)
            if any(len(values) != 1 for values in parameters.values()):
                raise ValueError("duplicate query parameter")
            payload = {key: values[0] for key, values in parameters.items()}
            for field in ("limit", "after_number", "year", "quarter"):
                if field in payload:
                    payload[field] = int(payload[field])
            return payload

        def dashboard_error(self, exc):
            result = error_response(exc)
            status = (
                401
                if isinstance(exc, IdentityError)
                else {
                    "preview_expired": 409,
                    "report_job_not_ready": 409,
                    "unknown_report_job": 404,
                    "report_download_invalid": 409,
                }.get(result.get("code"), 400)
            )
            self.json_reply(status, result)

        def browser_session_token(self):
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            cookie = cookies.get("finance_session")
            return server.browser_sessions.get(cookie.value) if cookie else None

        def session_token(self):
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth.removeprefix("Bearer ")
            return self.browser_session_token()

        def allowed_origin(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            return self.headers.get(
                "Host"
            ) == f"127.0.0.1:{self.server.server_port}" and self.headers.get("Origin") in (
                None,
                origin,
            )

        def has_capability(self):
            return hmac.compare_digest(self.headers.get("X-Local-Capability", ""), secret)

        def do_POST(self):
            if not self.allowed_origin():
                self.json_reply(403, {"code": "invalid_origin"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 30 * 1024 * 1024:
                    self.json_reply(413, {"code": "request_too_large"})
                    return
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    self.json_reply(415, {"code": "json_required"})
                    return
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("object required")
                path = urlsplit(self.path).path
                if path == "/api/browser-session":
                    if set(payload) != {"ticket"}:
                        raise ValueError("invalid ticket request")
                    ticket = server.browser_tickets.pop(payload["ticket"], None)
                    if ticket is None or ticket[1] < time.monotonic():
                        self.json_reply(401, {"code": "session_ticket_expired"})
                        return
                    surface_id = secrets.token_urlsafe(32)
                    server.browser_surfaces[surface_id] = True
                    cookies = [f"finance_surface={surface_id}; HttpOnly; SameSite=Strict; Path=/"]
                    authenticated = False
                    if ticket[0]:
                        service.security.authorize(ticket[0])
                        cookie_id = secrets.token_urlsafe(32)
                        server.browser_sessions[cookie_id] = ticket[0]
                        cookies.append(
                            f"finance_session={cookie_id}; HttpOnly; SameSite=Strict; Path=/"
                        )
                        authenticated = True
                    self.json_reply(
                        200, {"status": "ready", "authenticated": authenticated}, cookie=cookies
                    )
                    return
                if path == "/api/security-request":
                    if self.headers.get("Origin") != f"http://127.0.0.1:{server.server_port}":
                        self.json_reply(403, {"code": "same_origin_required"})
                        return
                    cookies = SimpleCookie()
                    cookies.load(self.headers.get("Cookie", ""))
                    surface = cookies.get("finance_surface")
                    if not surface or surface.value not in server.browser_surfaces:
                        self.json_reply(
                            403,
                            {"code": "launcher_required", "message": "请从本机记账启动器打开页面"},
                        )
                        return
                    if set(payload) != {"operation", "payload"} or payload["operation"] not in {
                        "request",
                        "status",
                        "cancel",
                        "session_status",
                    }:
                        raise ValueError("invalid security window request")
                    with service.catalog.connection(read_only=True):
                        pass
                    result = service.security_controller.dispatch(
                        payload["operation"], payload["payload"], private=False
                    )
                    if payload["operation"] == "session_status":
                        try:
                            service.security.authorize(self.session_token())
                            result["authenticated"] = True
                        except Exception:
                            result["authenticated"] = False
                    if payload["operation"] == "request":
                        flow_id = secrets.token_urlsafe(32)
                        server.browser_window_flows[flow_id] = (
                            result["request_id"],
                            time.monotonic() + 1800,
                        )
                        self.json_reply(
                            200,
                            result,
                            cookie=f"finance_window={flow_id}; HttpOnly; SameSite=Strict; Path=/",
                        )
                    elif payload["operation"] == "status" and result.get("login_completed"):
                        cookies = SimpleCookie()
                        cookies.load(self.headers.get("Cookie", ""))
                        flow_cookie = cookies.get("finance_window")
                        flow = (
                            server.browser_window_flows.get(flow_cookie.value)
                            if flow_cookie
                            else None
                        )
                        if flow and flow[0] == result["request_id"] and flow[1] > time.monotonic():
                            owner_token = service.security_controller.store.load_session_token()
                            service.security.authorize(owner_token)
                            cookie_id = secrets.token_urlsafe(32)
                            server.browser_sessions[cookie_id] = owner_token.get_secret_value()
                            server.browser_window_flows.pop(flow_cookie.value)
                            self.json_reply(
                                200,
                                {**result, "browser_authenticated": True},
                                cookie=(
                                    f"finance_session={cookie_id}; "
                                    "HttpOnly; SameSite=Strict; Path=/"
                                ),
                            )
                        else:
                            self.json_reply(200, result)
                    else:
                        self.json_reply(200, result)
                    return
                if path == "/api/local/report-export":
                    if self.headers.get("Origin") != f"http://127.0.0.1:{server.server_port}":
                        self.json_reply(403, {"code": "same_origin_required"})
                        return
                    owner_token = self.browser_session_token()
                    if not owner_token:
                        self.json_reply(401, {"code": "owner_session_required"})
                        return
                    try:
                        result = service.dispatch(
                            "confirm_browser_report_export", payload, session_token=owner_token
                        )
                        self.json_reply(200, result)
                    except Exception as exc:
                        self.dashboard_error(exc)
                    return
                if not self.has_capability():
                    self.json_reply(403, {"code": "local_capability_required"})
                    return
                if path == "/api/command":
                    if set(payload) != {"command", "payload"}:
                        raise ValueError("invalid command envelope")
                    result = service.dispatch(
                        payload["command"], payload["payload"], session_token=self.session_token()
                    )
                elif path == "/api/security":
                    with service.catalog.connection(read_only=True):
                        pass
                    operation = payload.pop("operation")
                    result = service.security_controller.dispatch(operation, payload, private=True)
                elif path == "/api/browser-ticket":
                    if payload:
                        raise ValueError("unexpected ticket fields")
                    owner_token = self.session_token()
                    if owner_token:
                        try:
                            service.security.authorize(owner_token)
                        except Exception:
                            owner_token = None
                    ticket_id = secrets.token_urlsafe(32)
                    server.browser_tickets[ticket_id] = (owner_token, time.monotonic() + 60)
                    result = {"url": f"http://127.0.0.1:{server.server_port}/#ticket={ticket_id}"}
                elif path == "/api/shutdown":
                    if payload:
                        raise ValueError("unexpected shutdown fields")
                    result = {"status": "stopping"}
                    threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    self.json_reply(404, {"code": "unknown_endpoint"})
                    return
                self.json_reply(200, result)
            except Exception as exc:
                self.json_reply(400, error_response(exc))

        def do_GET(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.reply(403, b'{"error":"invalid host"}')
                return
            if self.headers.get("Origin") not in (None, origin):
                self.reply(403, b'{"error":"invalid origin"}')
                return
            url = urlsplit(self.path)
            if url.path == "/api/health":
                if not self.has_capability():
                    self.json_reply(403, {"code": "local_capability_required"})
                    return
                self.json_reply(
                    200,
                    {
                        "status": "ready",
                        "build_id": server.build_id,
                        "catalog_id": service.security.catalog_instance_id,
                    },
                )
                return
            if url.path == "/favicon.ico":
                self.reply(204, b"", "image/x-icon")
                return
            if url.path.startswith("/api/dashboard/"):
                endpoints = {"context", "brief", "funds", "employees", "assets", "quarterly-report"}
                action = url.path.removeprefix("/api/dashboard/")
                if action not in endpoints:
                    self.json_reply(404, {"code": "unknown_endpoint"})
                    return
                owner_token = self.session_token()
                if not owner_token:
                    self.json_reply(401, {"code": "owner_session_required"})
                    return
                try:
                    result = service.dispatch(
                        "dashboard_" + action.replace("-", "_"),
                        self.query_payload(url.query),
                        session_token=owner_token,
                    )
                    self.json_reply(200, wire_money(result))
                except Exception as exc:
                    self.dashboard_error(exc)
                return
            if url.path.startswith("/api/local/report-export/") and url.path.endswith("/download"):
                try:
                    from .reports import Reports

                    service.security.authorize(self.session_token())
                    payload = self.query_payload(url.query)
                    if set(payload) != {"company_id"}:
                        raise ValueError("company is required")
                    job_id = url.path.removeprefix("/api/local/report-export/").removesuffix(
                        "/download"
                    )
                    if not job_id or "/" in job_id or len(job_id) > 200:
                        raise ValueError("invalid job identity")
                    name, content = Reports(
                        service.engine(payload["company_id"])
                    ).download_browser_report(job_id)
                    # A revoked session must not deliver a file after validation finishes.
                    service.security.authorize(self.session_token())
                    self.reply(
                        200,
                        content,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        filename=name,
                    )
                except Exception as exc:
                    self.dashboard_error(exc)
                return
            if url.path.startswith("/api/local/"):
                owner_token = self.session_token()
                if not owner_token:
                    self.reply(401, b'{"error":"owner session required"}')
                    return
                action = url.path.removeprefix("/api/local/")
                if action not in (
                    "companies",
                    "overview",
                    "ledger",
                    "trace",
                    "closed_report",
                    "jobs",
                ):
                    self.reply(404, b'{"error":"unknown read endpoint"}')
                    return
                try:
                    payload = self.query_payload(url.query)
                    result = service.dispatch(action, payload, session_token=owner_token)
                    if action == "jobs":
                        from .reports import Reports

                        result = Reports(service.engine(payload["company_id"])).browser_job_results(
                            result
                        )
                        service.security.authorize(owner_token)
                    self.reply(
                        200, json.dumps(wire_money(result), ensure_ascii=False).encode("utf-8")
                    )
                except Exception as exc:
                    self.dashboard_error(exc)
                return
            app_routes = {
                "/",
                "/index.html",
                "/local.html",
                "/funds",
                "/employees",
                "/assets",
                "/reports",
            }
            entry = "index.html" if (static / "index.html").is_file() else "local.html"
            target = (
                static / (entry if url.path in app_routes else url.path.lstrip("/"))
            ).resolve()
            if not target.is_relative_to(static) or not target.is_file():
                self.reply(404, "页面尚未构建。请先运行 frontend 的 npm run build。".encode())
                return
            self.reply(
                200,
                target.read_bytes(),
                mimetypes.guess_type(target)[0] or "application/octet-stream",
            )

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.build_id = calculator_build_id()
    server.browser_tickets = {}
    server.browser_sessions = {}
    server.browser_window_flows = {}
    server.browser_surfaces = {}
    return server, secret
