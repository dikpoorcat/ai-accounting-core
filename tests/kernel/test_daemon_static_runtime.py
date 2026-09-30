"""Daemon-only static loading precedes every catalog or company read."""

from contextlib import contextmanager

from ai_accounting.kernel import command_schema, daemon
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.service import LocalService


def test_daemon_prepares_static_runtime_once_before_root_access(monkeypatch, tmp_path):
    events = []
    bundle = type("Bundle", (), {"registry": object()})()
    models = {"synthetic": object()}
    monkeypatch.setattr(daemon, "_STATIC_RUNTIME", None)
    monkeypatch.setattr(daemon, "production_bundle", lambda: events.append("bundle") or bundle)
    monkeypatch.setattr(
        command_schema,
        "command_models",
        lambda registry: events.append("models") or models,
    )
    monkeypatch.setattr(daemon.gc, "collect", lambda generation: events.append("collect"))
    monkeypatch.setattr(daemon.gc, "freeze", lambda: events.append("freeze"))
    monkeypatch.setattr(
        daemon,
        "reject_reparse_path",
        lambda root: events.append("root") or root,
    )
    monkeypatch.setattr(daemon, "ensure_catalog", lambda root: events.append("catalog"))

    @contextmanager
    def unavailable_lock(root):
        events.append("lock")
        yield False

    monkeypatch.setattr(daemon, "instance_lock", unavailable_lock)
    daemon.run(tmp_path)
    daemon.run(tmp_path)
    assert events == [
        "bundle",
        "models",
        "collect",
        "freeze",
        "root",
        "catalog",
        "lock",
        "root",
        "catalog",
        "lock",
    ]
    assert daemon._STATIC_RUNTIME == (bundle, models)


def test_local_service_uses_prepared_models_without_rebuilding(monkeypatch, tmp_path):
    bundle = production_bundle()
    prepared = {"synthetic": object()}

    def unexpected(_registry):
        raise AssertionError("command models were rebuilt after the catalog opened")

    monkeypatch.setattr(command_schema, "command_models", unexpected)
    service = LocalService(tmp_path / "synthetic-root", _static_runtime=(bundle, prepared))
    try:
        assert service.bundle is bundle
        assert service.command_models is prepared
    finally:
        service.close()
