"""Explicit replay startup and public mode reporting for the resident service."""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_accounting.kernel import cli, daemon, http, offline_upgrade, service
from ai_accounting.kernel.contracts import KernelError


def test_cli_daemon_passes_only_explicit_replay_scope(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(daemon, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "finance-local",
            "--root",
            str(tmp_path),
            "daemon",
            "--replay-scope",
            str(tmp_path / "scope.json"),
        ],
    )
    cli.main()
    monkeypatch.setattr(sys, "argv", ["finance-local", "--root", str(tmp_path), "daemon"])
    cli.main()
    assert calls == [
        ((tmp_path,), {"port": 0, "replay_scope_file": tmp_path / "scope.json"}),
        ((tmp_path,), {"port": 0, "replay_scope_file": None}),
    ]


def test_run_loads_private_scope_only_when_explicit(monkeypatch, tmp_path):
    class StopAtService(Exception):
        pass

    seen = []
    reads = []
    scope_file = tmp_path / "scope.json"
    scope = {"synthetic": "reviewed"}
    scope_file.write_text(json.dumps(scope), encoding="utf-8")

    @contextmanager
    def available(_root):
        yield True

    def private_file(path):
        reads.append(path)
        return path

    class FakeService:
        def __init__(self, root, **options):
            seen.append((root, options))
            raise StopAtService

    monkeypatch.setattr(daemon, "_prepare_static_runtime", lambda: "prepared")
    monkeypatch.setattr(daemon, "reject_reparse_path", lambda root: root)
    monkeypatch.setattr(daemon, "ensure_catalog", lambda _root: None)
    monkeypatch.setattr(daemon, "instance_lock", available)
    monkeypatch.setattr(daemon, "assert_private_file", private_file)
    monkeypatch.setattr(offline_upgrade, "check_root_current", lambda _root: None)
    monkeypatch.setattr(service, "LocalService", FakeService)

    with pytest.raises(StopAtService):
        daemon.run(tmp_path)
    with pytest.raises(StopAtService):
        daemon.run(tmp_path, replay_scope_file=scope_file)

    assert reads == [scope_file]
    assert "replay_scope" not in seen[0][1]
    assert seen[1][1]["replay_scope"] == scope
    assert seen[0][1]["_static_runtime"] == seen[1][1]["_static_runtime"] == "prepared"


def test_explicit_replay_start_rejects_running_service(monkeypatch, tmp_path):
    @contextmanager
    def unavailable(_root):
        yield False

    monkeypatch.setattr(daemon, "_prepare_static_runtime", lambda: "prepared")
    monkeypatch.setattr(daemon, "reject_reparse_path", lambda root: root)
    monkeypatch.setattr(daemon, "ensure_catalog", lambda _root: None)
    monkeypatch.setattr(daemon, "instance_lock", unavailable)
    assert daemon.run(tmp_path) is None
    with pytest.raises(KernelError) as error:
        daemon.run(tmp_path, replay_scope_file=tmp_path / "scope.json")
    assert error.value.code == "replay_service_already_running"


def test_replay_scope_accepts_utf8_bom(monkeypatch, tmp_path):
    class StopAtService(Exception):
        pass

    @contextmanager
    def available(_root):
        yield True

    scope_file = tmp_path / "scope.json"
    scope_file.write_bytes(b'\xef\xbb\xbf{"synthetic": "reviewed"}')
    seen = []

    class FakeService:
        def __init__(self, _root, **options):
            seen.append(options["replay_scope"])
            raise StopAtService

    monkeypatch.setattr(daemon, "_prepare_static_runtime", lambda: "prepared")
    monkeypatch.setattr(daemon, "reject_reparse_path", lambda root: root)
    monkeypatch.setattr(daemon, "ensure_catalog", lambda _root: None)
    monkeypatch.setattr(daemon, "instance_lock", available)
    monkeypatch.setattr(daemon, "assert_private_file", lambda path: path)
    monkeypatch.setattr(offline_upgrade, "check_root_current", lambda _root: None)
    monkeypatch.setattr(service, "LocalService", FakeService)
    with pytest.raises(StopAtService):
        daemon.run(tmp_path, replay_scope_file=scope_file)
    assert seen == [{"synthetic": "reviewed"}]


@pytest.mark.parametrize("contents", [b"{private secret", b"\xff"])
def test_invalid_replay_json_has_safe_error(monkeypatch, tmp_path, contents):
    @contextmanager
    def available(_root):
        yield True

    scope_file = tmp_path / "scope.json"
    scope_file.write_bytes(contents)
    monkeypatch.setattr(daemon, "_prepare_static_runtime", lambda: "prepared")
    monkeypatch.setattr(daemon, "reject_reparse_path", lambda root: root)
    monkeypatch.setattr(daemon, "ensure_catalog", lambda _root: None)
    monkeypatch.setattr(daemon, "instance_lock", available)
    monkeypatch.setattr(daemon, "assert_private_file", lambda path: path)
    monkeypatch.setattr(offline_upgrade, "check_root_current", lambda _root: None)
    with pytest.raises(KernelError) as error:
        daemon.run(tmp_path, replay_scope_file=scope_file)
    assert error.value.code == "invalid_replay_scope"
    assert "private secret" not in str(error.value)


@pytest.mark.parametrize(
    ("scope", "expected_mode", "expected_digest"),
    [(None, "normal", None), (SimpleNamespace(scope_digest="a" * 64), "replay", "a" * 64)],
)
def test_health_reports_mode_without_scope_contents(scope, expected_mode, expected_digest):
    synthetic = SimpleNamespace(
        replay_close_scope=scope,
        catalog=SimpleNamespace(database_format=lambda: {"synthetic": True}),
        security=SimpleNamespace(catalog_instance_id="synthetic-catalog"),
    )
    server, capability = http.create_server(synthetic, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/health",
            headers={"X-Local-Capability": capability},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            raw = response.read()
        result = json.loads(raw)
        assert result["execution_mode"] == expected_mode
        assert result["replay_scope_digest"] == expected_digest
        assert capability.encode() not in raw
        assert b"synthetic-catalog" in raw
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_service_info_reads_health_without_printing_capability(monkeypatch, capsys, tmp_path):
    metadata = {
        "protocol": 2,
        "pid": 123,
        "port": 456,
        "capability": "private-capability",
        "catalog_id": "synthetic-catalog",
        "build_id": "build",
        "database_format": {"synthetic": True},
    }
    health = {
        "execution_mode": "replay",
        "replay_scope_digest": "b" * 64,
    }
    monkeypatch.setattr(daemon, "_metadata_for_root", lambda _root: metadata)
    monkeypatch.setattr(daemon, "_request", lambda *_args, **_kwargs: health)
    monkeypatch.setattr(daemon, "_check_health", lambda _metadata, _health: None)
    monkeypatch.setattr(sys, "argv", ["finance-local", "--root", str(tmp_path), "service-info"])
    cli.main()
    raw = capsys.readouterr().out
    result = json.loads(raw)
    assert result["execution_mode"] == "replay"
    assert result["replay_scope_digest"] == "b" * 64
    assert "capability" not in result
    assert "private-capability" not in raw


def test_automatic_start_never_passes_replay_scope(monkeypatch, tmp_path):
    class StopAtSpawn(Exception):
        pass

    arguments = []

    @contextmanager
    def available(_root):
        yield True

    def private_log(path, *, create):
        assert create
        path.touch()
        return path

    def spawn(command, **_kwargs):
        arguments.extend(command)
        raise StopAtSpawn

    monkeypatch.setattr(daemon, "reject_reparse_path", lambda root: Path(root))
    monkeypatch.setattr(daemon, "ensure_catalog", lambda _root: None)
    monkeypatch.setattr(daemon, "instance_lock", available)
    monkeypatch.setattr(daemon, "ensure_private_file", private_log)
    monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
    monkeypatch.setattr(offline_upgrade, "check_root_current", lambda _root: None)
    with pytest.raises(StopAtSpawn):
        daemon.ensure_service(tmp_path)
    assert arguments[-1] == "daemon"
    assert "--replay-scope" not in arguments
