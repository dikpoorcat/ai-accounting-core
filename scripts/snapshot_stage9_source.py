"""Copy a coherent synthetic benchmark source tree after development settles.

The target must be absent. A changed source or incomplete copy leaves its
target for diagnosis, without writing a successful source-manifest.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

if __package__:
    from .stage9_source import synthetic_path, workspace_root
else:
    from stage9_source import synthetic_path, workspace_root


def _selected(source: Path) -> tuple[Path, ...]:
    roots = (
        source / "src/ai_accounting",
        source / "tests/kernel",
        source / "tests/pure",
        source / "scripts",
        source / "frontend/src",
        source / "frontend/tests",
    )
    required = (
        source / "src/ai_accounting/__init__.py",
        source / "tests/kernel/stage9_book.py",
        source / "tests/kernel/stage9_independent_book.py",
        source / "scripts/benchmark_stage9.py",
        source / "scripts/benchmark_stage9_browser.py",
        source / "src/ai_accounting/static/dashboard/index.html",
        source / "frontend/tests/browser-stage9-hot-refresh.cjs",
        source / "frontend/package.json",
        source / "frontend/package-lock.json",
        source / "frontend/vite.config.ts",
        source / "frontend/local-api-proxy.ts",
        source / "frontend/tsconfig.json",
        source / "pyproject.toml",
    )
    if any(not item.is_file() for item in required) or any(not root.is_dir() for root in roots):
        raise ValueError("Stage 9 snapshot source is missing code, fixtures, or built frontend")
    diagnostic_dist = source / "frontend/dist"
    if diagnostic_dist.is_dir():
        roots += (diagnostic_dist,)
    selected = {
        Path("pyproject.toml"),
        Path("tests/conftest.py"),
        Path("frontend/package.json"),
        Path("frontend/package-lock.json"),
        Path("frontend/vite.config.ts"),
        Path("frontend/local-api-proxy.ts"),
        Path("frontend/tsconfig.json"),
    }
    for root in roots:
        for item in root.rglob("*"):
            if item.is_symlink():
                raise ValueError(f"Stage 9 snapshot cannot copy a symlink: {item}")
            if not item.is_file() or "__pycache__" in item.parts:
                continue
            relative = item.relative_to(source)
            if relative.parts[0] == "scripts" and item.suffix not in {".py", ".ps1"}:
                continue
            if relative.parts[0] == "tests" and item.suffix != ".py":
                continue
            selected.add(relative)
    if not (source / "tests/conftest.py").is_file():
        raise ValueError("Stage 9 snapshot needs tests/conftest.py")
    return tuple(sorted(selected))


def _file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _inventory(source: Path) -> dict[str, str]:
    return {path.as_posix(): _file_hash(source / path) for path in _selected(source)}


def _digest(files: dict[str, str]) -> str:
    content = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def snapshot_source(source: Path, target: Path, *, workspace: Path) -> dict:
    source = source.resolve()
    target = synthetic_path(target, workspace)
    if target.exists():
        raise ValueError("Stage 9 source snapshot target must not exist")
    before = _inventory(source)
    target.mkdir(parents=False, exist_ok=False)
    try:
        for name in before:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with (source / name).open("rb") as original, destination.open("xb") as copy:
                shutil.copyfileobj(original, copy)
        after = _inventory(source)
        copied = _inventory(target)
        if before != after or before != copied:
            raise RuntimeError("Stage 9 source changed during copy or snapshot bytes differ")
        manifest = {
            "status": "complete",
            "source": str(source),
            "target": str(target),
            "sha256": _digest(before),
            "file_count": len(before),
            "files": before,
        }
        pending = target / "source-manifest.json.pending"
        with pending.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, target / "source-manifest.json")
        return manifest
    except Exception as error:
        audit = {
            "status": "incomplete",
            "source": str(source),
            "target": str(target),
            "before_sha256": _digest(before),
            "error": {"type": type(error).__name__, "message": str(error)},
        }
        with (target / "copy-audit.json").open("x", encoding="utf-8") as handle:
            json.dump(audit, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
    result = snapshot_source(args.source, args.target, workspace=workspace)
    print(json.dumps({key: result[key] for key in ("target", "sha256", "file_count")}))


if __name__ == "__main__":
    main()
