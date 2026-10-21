"""Bounded request frames finish before authorization, JSON parsing and dispatch."""

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http.client import HTTPConnection
from io import BytesIO
from queue import Queue

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.runtime._state import MAX_REQUEST_BYTES

from .conftest import native_login
from .test_disconnects import transport as transport
from .test_transport import browser, http


def connection(runtime, *, path="/private/info", headers=()):
    client = HTTPConnection("127.0.0.1", runtime.server.server_port, timeout=5)
    client.putrequest("POST", path, skip_host=True)
    for name, value in headers:
        client.putheader(name, value)
    client.endheaders()
    return client


def private_headers(runtime):
    return (
        ("Host", f"127.0.0.1:{runtime.server.server_port}"),
        ("X-Modular-Capability", runtime.capability),
        ("Content-Type", "application/json"),
    )


def response(client):
    reply = client.getresponse()
    raw = reply.read()
    assert len(raw) == int(reply.getheader("Content-Length"))
    assert reply.read() == b""
    return reply.status, json.loads(raw)


@pytest.mark.parametrize(
    ("path", "changed", "body", "status", "code"),
    (
        ("/private/info", {}, b"{}", 200, None),
        ("/api/security-request", {}, b"not JSON", 400, "invalid_origin"),
        ("/private/info", {"Host": "foreign.invalid"}, b"not JSON", 400, "invalid_origin"),
        (
            "/private/info",
            {"X-Modular-Capability": "wrong"},
            b"not JSON",
            401,
            "owner_session_required",
        ),
        (
            "/private/info",
            {"Origin": "http://foreign.invalid"},
            b"{}",
            401,
            "owner_session_required",
        ),
        ("/private/info", {"Content-Type": "text/plain"}, b"{}", 400, "invalid_input"),
        ("/private/info", {}, b"not JSON", 400, "invalid_input"),
        ("/private/info", {}, b"\xff", 400, "invalid_input"),
        ("/private/info", {}, b"[]", 400, "invalid_input"),
    ),
)
def test_fragmented_frame_finishes_before_guard_and_json(
    local, monkeypatch, path, changed, body, status, code
):
    runtime, _, _ = local
    handler = runtime.server.RequestHandlerClass
    events = Queue()
    original_setup, original_guard = handler.setup, handler._guard

    class ObservedInput:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, length):
            events.put("read")
            raw = self.stream.read(length)
            events.put("read-complete")
            return raw

    def setup(value):
        original_setup(value)
        value.rfile = ObservedInput(value.rfile)

    def guard(value, **kwargs):
        events.put("guard")
        return original_guard(value, **kwargs)

    monkeypatch.setattr(handler, "setup", setup)
    monkeypatch.setattr(handler, "_guard", guard)
    runtime.start()
    headers = dict(private_headers(runtime)) | changed | {"Content-Length": str(len(body))}
    client = connection(runtime, path=path, headers=headers.items())
    try:
        # The server has the headers but no body yet. Observe actual blocking I/O,
        # then deliver two TCP writes; no arbitrary sleep chooses the ordering.
        first = events.get(timeout=5)
        client.send(body[:1])
        client.send(body[1:])
        actual_status, value = response(client)
        assert first == "read"
        assert events.get(timeout=5) == "read-complete"
        assert events.get(timeout=5) == "guard"
        assert actual_status == status
        if code is not None:
            assert value["code"] == code
    finally:
        client.close()


@pytest.mark.parametrize("body", (b"", b"{}"))
def test_eof_short_frame_never_dispatches_even_if_prefix_is_valid_json(local, monkeypatch, body):
    runtime, _, _ = local
    calls = []
    original = runtime.private

    def private(path, value):
        calls.append(path)
        return original(path, value)

    monkeypatch.setattr(runtime, "private", private)
    runtime.start()
    client = connection(
        runtime, headers=(*private_headers(runtime), ("Content-Length", str(len(body) + 1)))
    )
    try:
        if body:
            client.send(body)
        client.sock.shutdown(socket.SHUT_WR)
        status, value = response(client)
        assert status == 400 and value["code"] == "invalid_input"
        assert calls == []
    finally:
        client.close()


@pytest.mark.parametrize(
    ("framing", "code"),
    (
        ((), "invalid_input"),
        ((("Content-Length", "bad"),), "invalid_input"),
        ((("Content-Length", "+2"),), "invalid_input"),
        ((("Content-Length", "2_0"),), "invalid_input"),
        ((("Content-Length", "2"), ("Content-Length", "2")), "invalid_input"),
        ((("Content-Length", "2"), ("Content-Length", "3")), "invalid_input"),
        ((("Content-Length", "2, 2"),), "invalid_input"),
        ((("Transfer-Encoding", "chunked"), ("Content-Length", "2")), "invalid_input"),
        ((("Transfer-Encoding", ""), ("Content-Length", "2")), "invalid_input"),
        ((("Content-Length", "0"),), "batch_too_large"),
        ((("Content-Length", "-1"),), "invalid_input"),
        ((("Content-Length", str(MAX_REQUEST_BYTES + 1)),), "batch_too_large"),
    ),
)
def test_unsafe_or_out_of_bounds_frames_close_without_reading_or_dispatch(
    local, monkeypatch, framing, code
):
    runtime, _, _ = local

    def forbidden(*args, **kwargs):
        pytest.fail("an invalid frame must not reach authorization or dispatch")

    monkeypatch.setattr(runtime.server.RequestHandlerClass, "_guard", forbidden)
    monkeypatch.setattr(runtime, "private", forbidden)
    runtime.start()
    client = connection(runtime, headers=(*private_headers(runtime), *framing))
    try:
        status, value = response(client)
        assert status == 400 and value["code"] == code
        assert client.sock is None  # HTTP/1.0 response closes even for an unsafe unread frame.
    finally:
        client.close()


def test_existing_64_mib_limit_accepts_the_exact_frame_and_rejects_larger_before_read(transport):
    _, handler = transport
    value = handler("POST")
    assert MAX_REQUEST_BYTES == 64 * 1024 * 1024
    raw = b"{}" + b" " * (MAX_REQUEST_BYTES - 2)
    value.headers = Message()
    value.headers["Content-Length"] = str(len(raw))
    value.rfile = BytesIO(raw)
    assert value._read_body() == raw
    value.headers.replace_header("Content-Length", str(MAX_REQUEST_BYTES + 1))
    value.rfile = BytesIO(b"")
    with pytest.raises(ContractError) as failed:
        value._read_body()
    assert failed.value.code == "batch_too_large" and value.rfile.tell() == 0


def test_concurrent_missing_origin_rejections_keep_every_complete_response(local):
    runtime, _, _ = local
    runtime.start()
    native_login(runtime)
    cookies = browser(runtime)

    def reject(_):
        status, raw, _ = http(
            runtime,
            "POST",
            "/api/security-request",
            {"operation": "session_status"},
            cookies=cookies,
            origin=False,
        )
        return status, json.loads(raw)["code"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        observations = list(pool.map(reject, range(100)))
    assert observations == [(400, "invalid_origin")] * 100
