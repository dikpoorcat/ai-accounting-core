"""Small loopback read adapter for the local Vue page; writes stay typed MCP/CLI commands."""

from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .contracts import KernelError


def wire_money(value, key=""):
    if type(value) is int and (
        key in {"debit", "credit", "total", "amount"} or key.endswith("_fen")
    ):
        return str(value)
    if isinstance(value, dict):
        return {name: wire_money(item, name) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire_money(item) for item in value]
    return value


def create_server(service, *, port=0, static_directory=None, token=None):
    secret = token or secrets.token_urlsafe(32)
    packaged = Path(__file__).parents[1] / "static" / "dashboard"
    checkout = Path(__file__).parents[3] / "frontend" / "dist"
    static = Path(
        static_directory or (packaged if (packaged / "local.html").is_file() else checkout)
    ).resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Request URLs may contain business identifiers. The launcher prints only its own URL.
            pass

        def reply(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.reply(403, b'{"error":"invalid host"}')
                return
            if self.headers.get("Origin") not in (None, origin):
                self.reply(403, b'{"error":"invalid origin"}')
                return
            url = urlsplit(self.path)
            if url.path == "/favicon.ico":
                self.reply(204, b"", "image/x-icon")
                return
            if url.path.startswith("/api/local/"):
                supplied = self.headers.get("Authorization", "")
                if not hmac.compare_digest(supplied, "Bearer " + secret):
                    self.reply(401, b'{"error":"owner session required"}')
                    return
                action = url.path.removeprefix("/api/local/")
                if action not in ("companies", "overview", "ledger", "trace", "closed_report"):
                    self.reply(404, b'{"error":"unknown read endpoint"}')
                    return
                try:
                    parameters = parse_qs(url.query, strict_parsing=True)
                    if any(len(values) != 1 for values in parameters.values()):
                        raise ValueError("duplicate query parameter")
                    payload = {key: values[0] for key, values in parameters.items()}
                    for field in ("limit", "after_number"):
                        if field in payload:
                            payload[field] = int(payload[field])
                    result = service.dispatch(action, payload)
                    self.reply(
                        200, json.dumps(wire_money(result), ensure_ascii=False).encode("utf-8")
                    )
                except KernelError as exc:
                    self.reply(400, json.dumps(exc.response(), ensure_ascii=False).encode("utf-8"))
                except (KeyError, ValueError, TypeError):
                    self.reply(400, b'{"error":"invalid query"}')
                return
            target = (
                static / ("local.html" if url.path == "/" else url.path.lstrip("/"))
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
    return server, secret


def serve(service, *, port=0, static_directory=None):
    server, token = create_server(service, port=port, static_directory=static_directory)
    print(
        f"本地会计界面：http://127.0.0.1:{server.server_port}/local.html#token={token}", flush=True
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
