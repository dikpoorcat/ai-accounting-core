"""The package smoke must check a live daemon without restarting it."""

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from ai_accounting.kernel import daemon
from ai_accounting.kernel.catalog import Catalog
from scripts import verify_local_package as verifier


@pytest.mark.parametrize("change_catalog", [False, True])
def test_daemon_upgrade_smoke_checks_cli_lock_and_committed_catalog_state(
    tmp_path, monkeypatch, change_catalog
):
    root = tmp_path / "isolated-daemon"
    Catalog(root)
    package = tmp_path / "package"
    package.mkdir()
    metadata = {"pid": os.getpid() + 1, "port": 12345}
    health_calls = []
    monkeypatch.setattr(verifier, "_process_alive", lambda pid: pid == metadata["pid"])

    def request(current, path, *, timeout):
        assert current == metadata and path == "/api/health" and timeout == 3
        health_calls.append(path)
        return {"healthy": True}

    monkeypatch.setattr(daemon, "_request", request)
    monkeypatch.setattr(daemon, "_check_health", lambda current, result: (
        current == metadata and result == {"healthy": True}
    ) or pytest.fail("wrong daemon health"))
    monkeypatch.setattr(daemon, "_metadata_for_root", lambda selected: (
        metadata if selected == root else pytest.fail("wrong daemon root")
    ))

    def cli(command, **kwargs):
        assert command == [
            str(package / "finance-local.cmd"), "--root", str(root), "upgrade"
        ]
        assert kwargs["cwd"] == package and kwargs["timeout"] == 30
        if change_catalog:
            with sqlite3.connect(root / "catalog.sqlite") as connection:
                connection.execute("CREATE TABLE synthetic_unexpected_change(value INTEGER)")
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"status": "rejected", "code": "service_active"}),
        )

    monkeypatch.setattr(verifier.subprocess, "run", cli)
    if change_catalog:
        with pytest.raises(AssertionError, match="changed directory data"):
            verifier.verify_daemon_upgrade_rejection(package, root, metadata)
    else:
        result = verifier.verify_daemon_upgrade_rejection(package, root, metadata)
        assert result == {
            "daemon_held_root_lock_rejected": "service_active",
            "daemon_alive_same_pid": True,
            "catalog_data_and_version_unchanged": True,
        }
    assert health_calls == ["/api/health", "/api/health"]


def test_daemon_upgrade_smoke_rejects_any_other_cli_result(tmp_path, monkeypatch):
    root = tmp_path / "isolated-daemon"
    Catalog(root)
    package = tmp_path / "package"
    package.mkdir()
    metadata = {"pid": os.getpid() + 1}
    monkeypatch.setattr(verifier, "_process_alive", lambda pid: pid == metadata["pid"])
    monkeypatch.setattr(daemon, "_request", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(daemon, "_check_health", lambda *_args: None)
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"status": "rejected", "code": "schema_version_unsupported"}),
        ),
    )
    with pytest.raises(AssertionError):
        verifier.verify_daemon_upgrade_rejection(package, root, metadata)
