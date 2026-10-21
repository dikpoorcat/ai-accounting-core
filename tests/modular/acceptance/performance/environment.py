"""Record the actual machine and content identities outside timed work."""

import json
import platform
import sqlite3
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[4]

# Inputs outside the three source trees below. Keep the actual measurement,
# oracle and build dependencies together rather than duplicating their lists.
INPUT_FILES = (
    "docs/redesign/contracts/core.schema.json",
    "docs/redesign/contracts/dashboard-response-v2.json",
    "docs/redesign/contracts/review-materials.schema.json",
    "docs/redesign/examples/core-acceptance.json",
    "frontend/index.html",
    "frontend/local-api-proxy.ts",
    "frontend/local.html",
    "frontend/package-lock.json",
    "frontend/package.json",
    "frontend/scripts/generate-dashboard-contracts.mjs",
    "frontend/tests/browser-stage9-hot-refresh.cjs",
    "frontend/tests/helpers/dashboard-select.cjs",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
    "pyproject.toml",
    "tests/conftest.py",
    "uv.lock",
)


def _tree_digest(directory, *, ignore_python_cache=True):
    if not directory.is_dir():
        raise FileNotFoundError(f"Acceptance input directory is missing: {directory}")
    digest = sha256()
    paths = sorted(
        path
        for path in directory.rglob("*")
        if path.is_file()
        and (not ignore_python_cache or ("__pycache__" not in path.parts and path.suffix != ".pyc"))
    )
    for path in paths:
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        digest.update(sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def frontend_identity():
    return _tree_digest(WORKSPACE / "frontend/src")


def code_identity(*, static_directory=None):
    """Identify the implementation and measurement code, including declared dependencies."""
    result = {
        "source_sha256": _tree_digest(WORKSPACE / "src/ai_accounting_modular"),
        "frontend_source_sha256": frontend_identity(),
        "measurement_sha256": _tree_digest(WORKSPACE / "tests/modular/acceptance"),
        "input_files_sha256": {
            name: sha256((WORKSPACE / name).read_bytes()).hexdigest() for name in INPUT_FILES
        },
    }
    if static_directory is not None:
        selected = Path(static_directory).resolve()
        result["static_directory"] = {
            "path": str(selected),
            "sha256": _tree_digest(selected, ignore_python_cache=False),
        }
    return result


def capture(*, static_directory=None):
    options = {} if static_directory is None else {"static_directory": static_directory}
    result = {
        "system": platform.platform(),
        "cpu": platform.processor(),
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "machine": platform.machine(),
        **code_identity(**options),
    }
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=WORKSPACE, capture_output=True, text=True, check=False
    )
    result["git_sha"] = revision.stdout.strip() if revision.returncode == 0 else None
    if sys.platform == "win32":
        command = (
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
            "@{cpu=@(Get-CimInstance Win32_Processor | "
            "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors);"
            "memory=@(Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed);"
            "storage=@(Get-CimInstance Win32_DiskDrive | Select-Object Model,Size,MediaType);"
            "physical_storage=@(Get-PhysicalDisk | "
            "Select-Object FriendlyName,Size,MediaType,BusType)} "
            "| ConvertTo-Json -Depth 4 -Compress"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        try:
            result["hardware"] = json.loads(completed.stdout)
        except ValueError:
            result["hardware"] = {"state": "unavailable", "exit_code": completed.returncode}
    return result
