from __future__ import annotations

import os
import subprocess
import sys


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
