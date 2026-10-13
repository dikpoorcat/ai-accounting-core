"""Normal startup keeps the current source, even beside a retained runtime pointer."""

import os
import shutil
import sys
from pathlib import Path

import pytest

import ai_accounting
from ai_accounting.kernel import cli, daemon, runtime_snapshot
from ai_accounting.kernel.build import calculator_build_id
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.permissions import create_private_file, ensure_private_directory


def retained_pointer(root):
    snapshots = ensure_private_directory(root / ".runtime-snapshots")
    pointer = create_private_file(snapshots / "active")
    pointer.write_text("b" * 64 + "\n", encoding="ascii")
    return pointer


def test_normal_cli_daemon_does_not_bootstrap_retained_runtime(monkeypatch, tmp_path):
    root = ensure_private_directory(tmp_path / "synthetic-root")
    pointer = retained_pointer(root)
    calls = []
    monkeypatch.setattr(runtime_snapshot, "bootstrap_runtime",
                        lambda _root: pytest.fail("normal CLI selected a retained runtime"))
    monkeypatch.setattr(daemon, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(sys, "argv", ["finance-local", "--root", str(root), "daemon"])
    cli.main()
    assert calls == [((root,), {"port": 0, "replay_scope_file": None})]
    assert pointer.read_text(encoding="ascii") == "b" * 64 + "\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows resident process and protected metadata")
def test_stopped_service_automatically_restarts_with_selected_source_package(monkeypatch, tmp_path):
    source = Path(ai_accounting.__file__).resolve().parent
    selected = tmp_path / "selected-source" / "ai_accounting"
    shutil.copytree(source, selected, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # A distinct build proves that the child did not use the editable install's source.
    build = selected / "kernel" / "build.py"
    with build.open("a", encoding="utf-8") as output:
        output.write("\n# Synthetic selected-source startup marker.\n")
    expected_build = calculator_build_id(selected)
    catalog = Catalog(tmp_path / "synthetic-root")
    pointer = retained_pointer(catalog.root)
    with catalog.connection(read_only=True) as connection:
        identity = connection.execute(
            "SELECT instance_id FROM catalog_identity WHERE id=1"
        ).fetchone()[0]
    monkeypatch.setattr(ai_accounting, "__file__", str(selected / "__init__.py"))
    popen = daemon.subprocess.Popen
    processes = []

    def start(command, **options):
        process = popen(command, **options)
        if command[-1] == "daemon":
            processes.append(process)
        return process

    monkeypatch.setattr(daemon.subprocess, "Popen", start)
    try:
        first = daemon.ensure_service(catalog.root)
        assert first["build_id"] == expected_build
        assert first["catalog_id"] == identity
        assert daemon.stop_service(catalog.root)["status"] == "stopping"
        assert processes[-1].wait(timeout=10) == 0
        second = daemon.ensure_service(catalog.root)
        assert second["build_id"] == expected_build
        assert second["catalog_id"] == identity
        assert second["pid"] != first["pid"]
        assert pointer.read_text(encoding="ascii") == "b" * 64 + "\n"
        assert list(pointer.parent.iterdir()) == [pointer]
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    daemon.stop_service(catalog.root)
                    process.wait(timeout=10)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
