"""A cold page's real asset connections survive an occupied accept loop."""

import socket
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPResponse


def test_cold_asset_connections_queue_until_the_resident_can_accept(local, tmp_path):
    runtime, _, _ = local
    assets = {}
    for index in range(16):
        extension = "css" if index % 2 else "js"
        path = f"/component-{index}.{extension}"
        body = f"/* 合成组件 {index} */\n".encode() + (
            b".amount{color:#123}" if extension == "css" else b"export const value=123;"
        )
        (tmp_path / "static" / path.lstrip("/")).write_bytes(body)
        assets[path] = body

    connections = []
    try:
        # Bind/listen is already real, but acceptance has not started. This
        # reproduces a busy resident without a timing-sensitive CPU race.
        def connect(_):
            return socket.create_connection(runtime.server.server_address, timeout=5)

        with ThreadPoolExecutor(max_workers=len(assets)) as pool:
            pending = [pool.submit(connect, index) for index in range(len(assets))]
            failures = []
            for future in pending:
                try:
                    connections.append(future.result())
                except OSError as error:
                    failures.append(type(error).__name__)
        assert not failures, failures
        assert len(connections) == len(assets)
        runtime.start()

        def fetch(item):
            stream, (path, body) = item
            stream.sendall(
                (
                    f"GET {path} HTTP/1.0\r\n"
                    f"Host: 127.0.0.1:{runtime.server.server_port}\r\n\r\n"
                ).encode()
            )
            response = HTTPResponse(stream)
            try:
                response.begin()
                assert response.status == 200
                assert response.read() == body
                media_type = response.getheader("Content-Type").split(";", 1)[0]
                assert media_type in (
                    {"text/css"}
                    if path.endswith(".css")
                    else {"text/javascript", "application/javascript"}
                )
            finally:
                response.close()

        with ThreadPoolExecutor(max_workers=len(assets)) as pool:
            list(pool.map(fetch, zip(connections, assets.items(), strict=True)))
    finally:
        for connection in connections:
            connection.close()
