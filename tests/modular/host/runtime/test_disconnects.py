"""A cancelled browser request unwinds its resources without a second socket write."""

import json
import threading
from contextlib import contextmanager
from email.message import Message
from io import BytesIO
from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.runtime._http import create_server
from ai_accounting_modular.host.runtime.public import Runtime

DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class DisconnectedBody(BytesIO):
    def __init__(self, error):
        super().__init__()
        self.error = error
        self.attempts = 0

    def write(self, data):
        self.attempts += 1
        if self.attempts > 1:
            raise self.error("synthetic browser cancellation")
        return super().write(data)


@pytest.fixture
def transport():
    runtime = SimpleNamespace(
        catalog_id="synthetic-catalog",
        capability="synthetic-capability",
        stopping=False,
        session_lock=threading.RLock(),
        browser_sessions={},
        security=SimpleNamespace(authorize=lambda _: None),
        kernel=SimpleNamespace(dashboard=SimpleNamespace()),
        failure=Runtime.failure,
    )
    server = create_server(runtime)

    def handler(method, *, writer=None):
        # Exercise the actual Handler and its header/body writes; only the
        # transport stream is controlled, so each disconnect class is repeatable.
        value = object.__new__(server.RequestHandlerClass)
        value.server = server
        value.command = method
        value.path = "/api/dashboard/context" if method == "GET" else "/private/command"
        value.request_version = "HTTP/1.1"
        value.requestline = method + " " + value.path + " HTTP/1.1"
        value.close_connection = False
        value.headers = Message()
        value.headers["Host"] = f"127.0.0.1:{server.server_port}"
        value.headers["Content-Type"] = "application/json"
        value.headers["Content-Length"] = "2"
        value.headers["X-Modular-Capability"] = runtime.capability
        value.rfile = BytesIO(b"{}")
        value.wfile = writer if writer is not None else BytesIO()
        return value

    try:
        yield runtime, handler
    finally:
        server.server_close()


@pytest.mark.parametrize("error", DISCONNECTS)
def test_cancelled_download_releases_lease_and_does_not_log_or_retry(transport, capsys, error):
    runtime, handler = transport
    stream, leases = BytesIO(b"synthetic download bytes"), []

    @contextmanager
    def download(*_):
        leases.append("acquired")
        try:
            yield SimpleNamespace(status=200, headers={}, body=stream)
        finally:
            stream.close()
            leases.append("released")

    runtime.kernel.dashboard.get = download
    writer = DisconnectedBody(error)
    value = handler("GET", writer=writer)
    value.do_GET()
    assert value.close_connection
    assert leases == ["acquired", "released"] and stream.closed
    assert writer.attempts == 2  # One header and one cancelled body; no error response.
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("error", DISCONNECTS)
def test_cancelled_post_response_does_not_replay_committed_command(transport, capsys, error):
    runtime, handler = transport
    calls = []

    def private(path, body):
        calls.append((path, body))
        return {"committed": True}

    runtime.private = private
    writer = DisconnectedBody(error)
    value = handler("POST", writer=writer)
    value.do_POST()
    assert value.close_connection and calls == [("/private/command", {})]
    assert writer.attempts == 2
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("method", ("GET", "POST"))
@pytest.mark.parametrize("disconnect", DISCONNECTS)
@pytest.mark.parametrize(
    "error", (ContractError("invalid_input", "合成错误。"), ValueError("secret"))
)
def test_disconnect_while_writing_error_stops_after_the_first_response(
    transport, capsys, method, disconnect, error
):
    runtime, handler = transport

    def reject(*_):
        raise error

    runtime.security.authorize = reject
    runtime.private = reject
    writer = DisconnectedBody(disconnect)
    value = handler(method, writer=writer)
    getattr(value, "do_" + method)()
    assert value.close_connection and writer.attempts == 2
    stderr = capsys.readouterr().err
    assert stderr.count("modular diagnostic") == (0 if isinstance(error, ContractError) else 1)
    assert "secret" not in stderr


@pytest.mark.parametrize("method", ("GET", "POST"))
@pytest.mark.parametrize(
    ("error", "status", "code"),
    (
        (ContractError("invalid_input", "合成错误。"), 400, "invalid_input"),
        (ValueError("secret"), 500, "internal_error"),
        (PermissionError("secret"), 500, "internal_error"),
    ),
)
def test_connected_error_responses_and_real_diagnostics_remain(
    transport, capsys, method, error, status, code
):
    runtime, handler = transport

    def reject(*_):
        raise error

    runtime.security.authorize = reject
    runtime.private = reject
    value = handler(method)
    getattr(value, "do_" + method)()
    headers, body = value.wfile.getvalue().split(b"\r\n\r\n", 1)
    assert headers.startswith(f"HTTP/1.0 {status} ".encode())
    assert json.loads(body)["code"] == code
    stderr = capsys.readouterr().err
    assert stderr.count("modular diagnostic") == (0 if isinstance(error, ContractError) else 1)
    assert "secret" not in stderr and "secret" not in body.decode()
