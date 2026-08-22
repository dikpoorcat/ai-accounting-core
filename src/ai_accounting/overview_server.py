from __future__ import annotations

import argparse
import json
import threading
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from .database import make_engine
from .overview import build_overview_payload

LOCAL_OVERVIEW_HOST = "127.0.0.1"
DEFAULT_OVERVIEW_PORT = 8765


def render_overview_document(payload: dict[str, Any]) -> str:
    template = (
        resources.files("ai_accounting")
        .joinpath("templates/close_overview.html")
        .read_text(encoding="utf-8")
    )
    serialized = json.dumps(
        _stringify_fen_values(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    serialized = (
        serialized.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )
    return template.replace("__OVERVIEW_DATA__", serialized)


def _stringify_fen_values(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            item_key: _stringify_fen_values(item_value, key=item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_stringify_fen_values(item) for item in value]
    if (
        key is not None
        and key.endswith("_fen")
        and isinstance(value, int)
        and not isinstance(value, bool)
    ):
        return str(value)
    return value


def load_overview_document(
    engine: Engine,
    *,
    org_id: uuid.UUID | None = None,
) -> str:
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            with Session(bind=connection, expire_on_commit=False) as session:
                payload = build_overview_payload(session, org_id=org_id)
            return render_overview_document(payload)
        finally:
            transaction.rollback()


def make_overview_handler(
    engine: Engine,
    *,
    org_id: uuid.UUID | None = None,
) -> type[BaseHTTPRequestHandler]:
    class OverviewHandler(BaseHTTPRequestHandler):
        server_version = "FinanceOverview/0.1"

        def do_GET(self) -> None:
            self._serve(send_body=True)

        def do_HEAD(self) -> None:
            self._serve(send_body=False)

        def _serve(self, *, send_body: bool) -> None:
            path = urlsplit(self.path).path
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            if path not in {"/", "/index.html"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                document = load_overview_document(engine, org_id=org_id)
            except Exception as exc:
                print(f"OVERVIEW_RENDER_FAILED={type(exc).__name__}: {exc}")
                body = "本地经营概览读取失败，请检查数据库连接和迁移状态。".encode()
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if send_body:
                    self.wfile.write(body)
                return
            body = document.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; "
                "style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; "
                "img-src data:; "
                "base-uri 'none'; "
                "form-action 'none'; "
                "frame-ancestors 'none'",
            )
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            print("OVERVIEW_HTTP=" + (format % args))

    return OverviewHandler


def main() -> None:
    parser = argparse.ArgumentParser(
        description="serve a read-only owner overview on the local computer"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_OVERVIEW_PORT)
    parser.add_argument("--org-id", type=uuid.UUID)
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="do not open the default browser automatically",
    )
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    engine = make_engine()
    load_overview_document(engine, org_id=args.org_id)
    server = ThreadingHTTPServer(
        (LOCAL_OVERVIEW_HOST, args.port),
        make_overview_handler(engine, org_id=args.org_id),
    )
    actual_port = server.server_address[1]
    url = f"http://{LOCAL_OVERVIEW_HOST}:{actual_port}/"
    print(f"FINANCE_OVERVIEW_URL={url}")
    print("FINANCE_OVERVIEW_MODE=READ_ONLY_LOCAL")
    if not args.no_open:
        opener = threading.Timer(0.2, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.dispose()


if __name__ == "__main__":
    main()
