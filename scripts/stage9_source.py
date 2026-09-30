"""Keep synthetic Stage 9 tools on one source tree and one temporary workspace."""

from __future__ import annotations

import os
import sys
from pathlib import Path

WORKSPACE_ENV = "STAGE9_WORKSPACE_ROOT"


def workspace_root(default: Path, requested: Path | None = None) -> Path:
    selected = requested or os.environ.get(WORKSPACE_ENV) or default
    workspace = Path(selected).resolve()
    if not (workspace / ".tmp").is_dir():
        raise ValueError("Stage 9 workspace must have an existing .tmp directory")
    return workspace


def synthetic_path(path: Path, workspace: Path) -> Path:
    selected = path.resolve()
    if selected.parent != (workspace / ".tmp").resolve() or not selected.name.startswith("stage9-"):
        raise ValueError("Only named Stage 9 synthetic paths under workspace .tmp are allowed")
    return selected


def configure_source(source: Path, workspace: Path) -> Path:
    """Select implementation and fixtures before importing either package."""
    source = source.resolve()
    required = (
        source / "src/ai_accounting/__init__.py",
        source / "tests/kernel/stage9_book.py",
        source / "tests/kernel/stage9_independent_book.py",
        source / "tests/kernel/stage9_metrics.py",
        source / "scripts/benchmark_stage9_browser.py",
    )
    if any(not path.is_file() for path in required):
        raise ValueError("Stage 9 source needs implementation, fixtures, and browser harness")
    os.environ[WORKSPACE_ENV] = str(workspace)
    sys.path[:0] = [
        str(source / "tests/kernel"),
        str(source / "src"),
        str(source / "scripts"),
    ]
    return source


def require_source_module(module, source: Path, relative: str) -> None:
    expected = (source / relative).resolve()
    actual = Path(module.__file__).resolve()
    if actual != expected:
        raise ValueError(f"Stage 9 mixed source import: {actual} != {expected}")
