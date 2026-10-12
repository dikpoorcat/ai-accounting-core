from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ai_accounting.kernel import runtime_snapshot
from ai_accounting.kernel.permissions import ensure_private_directory


def _run(script, root):
    environment = dict(os.environ)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    return completed.stdout.strip()


def test_private_runtime_copy_is_selected_again_before_restart_imports(tmp_path):
    root = tmp_path / "company-root"
    activate = r"""
import json, sys
from html.parser import HTMLParser
from pathlib import Path
import ai_accounting
from ai_accounting.kernel.runtime_snapshot import activate_runtime_package, capture_runtime_snapshot
root = Path(sys.argv[1])
package, source_digest = activate_runtime_package(root)
snapshot = capture_runtime_snapshot()
dashboard = package / "static" / "dashboard"
index = dashboard / "index.html"
assert index.is_file()
class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []
    def handle_starttag(self, tag, attrs):
        self.paths.extend(
            value
            for key, value in attrs
            if key in {"src", "href"}
            and value
            and not value.startswith(("http:", "https:", "data:"))
        )
assets = Assets()
assets.feed(index.read_text(encoding="utf-8"))
assert assets.paths
assert all(
    (dashboard / value.lstrip("/")).is_file()
    for value in assets.paths
)
assert Path(ai_accounting.__file__).resolve().parent == package
assert snapshot.runtime_source_digest == source_digest
assert snapshot.runtime_path == str(package)
assert (root / ".runtime-snapshots" / "active").read_text(encoding="ascii").strip() == source_digest
print(json.dumps({"path": str(package), "digest": source_digest}))
"""
    first = _run(activate, root)
    expected = __import__("json").loads(first)

    bootstrap = r"""
import json, sys
from html.parser import HTMLParser
from pathlib import Path
import ai_accounting
from ai_accounting.kernel.runtime_snapshot import bootstrap_runtime, capture_runtime_snapshot
package, source_digest = bootstrap_runtime(Path(sys.argv[1]))
snapshot = capture_runtime_snapshot()
dashboard = package / "static" / "dashboard"
index = dashboard / "index.html"
assert index.is_file()
class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []
    def handle_starttag(self, tag, attrs):
        self.paths.extend(
            value
            for key, value in attrs
            if key in {"src", "href"}
            and value
            and not value.startswith(("http:", "https:", "data:"))
        )
assets = Assets()
assets.feed(index.read_text(encoding="utf-8"))
assert assets.paths
assert all(
    (dashboard / value.lstrip("/")).is_file()
    for value in assets.paths
)
assert Path(ai_accounting.__file__).resolve().parent == package
assert Path(ai_accounting.kernel.__file__).resolve().parent == package / "kernel"
assert snapshot.runtime_source_digest == source_digest
assert snapshot.runtime_path == str(package)
print(json.dumps({"path": str(package), "digest": source_digest}))
"""
    second = __import__("json").loads(_run(bootstrap, root))
    assert second == expected


@pytest.fixture
def synthetic_runtime_source(tmp_path, monkeypatch):
    import ai_accounting

    source = tmp_path / "checkout" / "src" / "ai_accounting"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("SYNTHETIC = True\n", encoding="utf-8")
    (source / "accounting.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(ai_accounting, "__file__", str(source / "__init__.py"))
    monkeypatch.setattr(runtime_snapshot, "_switch_loaded_package_paths", lambda path: None)
    yield source
    for path in source.rglob("*"):
        if path.is_file():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


@pytest.mark.parametrize("different", [False, True])
def test_runtime_publication_race_reuses_only_identical_target_and_preserves_active(
    tmp_path, monkeypatch, synthetic_runtime_source, different
):
    root = ensure_private_directory(tmp_path / "owned-runtime")
    snapshots = ensure_private_directory(root / ".runtime-snapshots")
    active = snapshots / "active"
    active.write_text("earlier-active-identity", encoding="ascii")
    original = runtime_snapshot.os.rename
    published = []

    def concurrent_publish(source, destination):
        shutil.copytree(source, destination)
        if different:
            changed = Path(destination) / "ai_accounting" / "accounting.py"
            changed.chmod(changed.stat().st_mode | stat.S_IWUSR)
            changed.write_text("VALUE = 999\n", encoding="utf-8")
        published.append(Path(destination))
        # On Windows a pre-existing nonempty directory raises WinError 5.
        # Other platforms similarly reject this concurrent publication.
        return original(source, destination)

    monkeypatch.setattr(runtime_snapshot.os, "rename", concurrent_publish)
    if different:
        with pytest.raises(RuntimeError, match="different contents"):
            runtime_snapshot.materialize_runtime_package(root)
        assert (published[0] / "ai_accounting" / "accounting.py").read_text() == "VALUE = 999\n"
    else:
        package, source_digest = runtime_snapshot.materialize_runtime_package(root)
        assert package.parent == published[0]
        assert runtime_snapshot._tree_digest(package.parent) == source_digest
    assert not list(snapshots.glob(".runtime-copy-*"))
    assert active.read_text(encoding="ascii") == "earlier-active-identity"


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only publication contract")
def test_windows_capture_clears_inherited_read_only_before_publication(
    tmp_path, monkeypatch, synthetic_runtime_source
):
    source = synthetic_runtime_source
    (source / "accounting.py").chmod(0o444)
    original = runtime_snapshot.os.rename

    def observe_writable(staging, destination):
        files = list((Path(staging) / "ai_accounting").rglob("*.py"))
        assert files and all(path.stat().st_mode & stat.S_IWUSR for path in files)
        return original(staging, destination)

    monkeypatch.setattr(runtime_snapshot.os, "rename", observe_writable)
    package, source_digest = runtime_snapshot.materialize_runtime_package(tmp_path / "root")
    assert (package / "accounting.py").stat().st_mode & stat.S_IWUSR
    assert runtime_snapshot._tree_digest(package.parent) == source_digest


def test_failed_runtime_publication_preserves_active_and_cleans_staging(
    tmp_path, monkeypatch, synthetic_runtime_source
):
    root = ensure_private_directory(tmp_path / "failed-runtime")
    snapshots = ensure_private_directory(root / ".runtime-snapshots")
    active = snapshots / "active"
    active.write_text("earlier-active-identity", encoding="ascii")

    def deny_publication(source, destination):
        raise PermissionError("synthetic denied publication")

    monkeypatch.setattr(runtime_snapshot.os, "rename", deny_publication)
    with pytest.raises(PermissionError, match="synthetic denied"):
        runtime_snapshot.materialize_runtime_package(root)
    assert active.read_text(encoding="ascii") == "earlier-active-identity"
    assert list(snapshots.iterdir()) == [active]
