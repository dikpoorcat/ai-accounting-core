"""Opt-in package source pinning for isolated stage 9 adapter acceptance.

Ordinary business regression checks origins without hashing the runtime package.
Use child_environment() and CHILD when an acceptance run needs exact source pins.
"""

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

from ai_accounting.kernel import backup

MODULES = (
    "cli",
    "mcp",
    "daemon",
    "dashboard_async",
    "dashboard_read_model",
    "dashboard_delivery",
    "response_contracts",
    "source24_history",
    "backup",
    "maintenance",
)


def source_manifest():
    source = Path(backup.__file__).resolve().parents[2]
    modules = {}
    for name in MODULES:
        module = importlib.import_module("ai_accounting.kernel." + name)
        path = Path(module.__file__).resolve()
        assert path.is_relative_to(source)
        modules[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return source, modules


# Pin both actual subprocess packages before entering their real CLI/MCP code.
# Writing a separate provenance file leaves their stdout/stdio protocol intact.
CHILD = """
import hashlib, importlib, importlib.abc, importlib.machinery, json, os, pathlib, sys
source = pathlib.Path(os.environ['P1_ADAPTER_SRC']).resolve()
expected = json.loads(os.environ['P1_ADAPTER_MODULES'])
manifest_path = source.parent / 'source-manifest.json'
manifest = json.loads(manifest_path.read_bytes())['files'] if manifest_path.is_file() else None
class ExactLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        return compile(pathlib.Path(self.path).read_bytes(), self.path, 'exec')
class Pin(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name != 'ai_accounting' and not name.startswith('ai_accounting.'):
            return None
        search = [str(source)] if name == 'ai_accounting' else path
        spec = importlib.machinery.PathFinder.find_spec(name, search)
        assert spec is not None, name
        paths = [spec.origin] if spec.origin else list(spec.submodule_search_locations or ())
        assert paths and all(
            pathlib.Path(p).resolve().is_relative_to(source / 'ai_accounting') for p in paths
        ), (name, paths)
        if spec.origin:
            origin = pathlib.Path(spec.origin).resolve()
            if manifest is not None:
                key = origin.relative_to(source.parent).as_posix()
                assert hashlib.sha256(origin.read_bytes()).hexdigest() == manifest[key]
            spec.loader = ExactLoader(name, str(origin))
        return spec
sys.meta_path.insert(0, Pin())
actual = {}
for name, record in expected.items():
    module = importlib.import_module('ai_accounting.kernel.' + name)
    path = pathlib.Path(module.__file__).resolve()
    assert path.is_relative_to(source), (name, path, source)
    actual[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
assert actual == expected, (actual, expected)
pathlib.Path(sys.argv[1]).write_text(json.dumps(actual), encoding='utf-8')
from ai_accounting.kernel import cli
sys.argv = [cli.__file__, *sys.argv[2:]]
cli.main()
"""


def child_environment():
    source, modules = source_manifest()
    return {
        **os.environ,
        "PYTHONPATH": str(source),
        "PYTHONUTF8": "1",
        "P1_ADAPTER_SRC": str(source),
        "P1_ADAPTER_MODULES": json.dumps(modules),
    }


def main(argv=None):
    """Run a real CLI/stdio entry with exact source checks for acceptance only."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    arguments = options.arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    return subprocess.call(
        [sys.executable, "-X", "utf8", "-c", CHILD, str(options.provenance), *arguments],
        env=child_environment(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
