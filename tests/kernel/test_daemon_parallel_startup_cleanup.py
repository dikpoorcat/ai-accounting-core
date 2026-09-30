"""Resident startup releases its spawned reader group on every partial failure."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from ai_accounting.kernel import daemon, http, jobs, offline_upgrade, service


@pytest.mark.parametrize("failure", ("server", "controller", "metadata", "runner", None))
def test_daemon_cleans_partial_parallel_startup(tmp_path, monkeypatch, failure):
    root = tmp_path / "synthetic-resident"
    root.mkdir()
    events = []

    @contextmanager
    def available(_root):
        events.append("lock")
        try:
            yield True
        finally:
            events.append("unlock")

    class FakeService:
        def __init__(self, selected, *, enable_read_pool, enable_parallel_brief, _static_runtime):
            assert selected == root
            assert enable_read_pool and enable_parallel_brief
            assert _static_runtime == "static"
            self.catalog = SimpleNamespace(database_format=lambda: "format")
            self.security = SimpleNamespace(catalog_instance_id="catalog")
            events.append("service")

        def close(self):
            events.append("service.close")

    class FakeServer:
        server_port = 12345
        build_id = "build"

        def serve_forever(self):
            events.append("server.serve")

        def shutdown(self):
            events.append("server.shutdown")

        def server_close(self):
            events.append("server.close")

    class FakeRunner:
        def __init__(self, catalog):
            assert catalog is not None
            events.append("runner")

        def start(self):
            events.append("runner.start")
            if failure == "runner":
                raise RuntimeError("synthetic runner failure")

        def stop(self):
            events.append("runner.stop")

    def create_server(_service, *, port):
        assert port == 0
        events.append("server")
        if failure == "server":
            raise RuntimeError("synthetic server failure")
        return FakeServer(), "capability"

    def controller(*_args):
        events.append("controller")
        if failure == "controller":
            raise RuntimeError("synthetic controller failure")
        return object()

    def metadata(*_args):
        events.append("metadata")
        if failure == "metadata":
            raise RuntimeError("synthetic metadata failure")

    monkeypatch.setattr(daemon, "_prepare_static_runtime", lambda: "static")
    monkeypatch.setattr(daemon, "reject_reparse_path", lambda selected: selected)
    monkeypatch.setattr(daemon, "ensure_catalog", lambda _root: None)
    monkeypatch.setattr(daemon, "instance_lock", available)
    monkeypatch.setattr(offline_upgrade, "check_root_current", lambda _root: None)
    monkeypatch.setattr(service, "LocalService", FakeService)
    monkeypatch.setattr(http, "create_server", create_server)
    monkeypatch.setattr(daemon, "build_native_security_controller", controller)
    monkeypatch.setattr(daemon, "write_protected_json", metadata)
    monkeypatch.setattr(jobs, "JobRunner", FakeRunner)

    if failure is None:
        daemon.run(root)
    else:
        with pytest.raises(RuntimeError, match=f"synthetic {failure} failure"):
            daemon.run(root)

    assert events[-2:] == ["service.close", "unlock"]
    assert events.count("service.close") == 1
    if failure == "server":
        assert "server.close" not in events
    else:
        assert events.count("server.close") == 1
    if failure in {"server", "controller", "metadata"}:
        assert "runner.stop" not in events
    else:
        assert events.count("runner.stop") == 1
    assert events.count("server.shutdown") == (1 if failure is None else 0)
