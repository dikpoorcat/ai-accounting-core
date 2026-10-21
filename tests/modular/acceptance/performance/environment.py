"""Record the actual machine and content identities outside timed work."""

import json
import platform
import sqlite3
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[4]


def _tree_digest(directory):
    digest = sha256()
    paths = sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    for path in paths:
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        digest.update(sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def frontend_identity():
    return _tree_digest(WORKSPACE / "frontend/src")


def code_identity():
    """Identify the implementation and measurement code, including declared dependencies."""
    return {
        "source_sha256": _tree_digest(WORKSPACE / "src/ai_accounting_modular"),
        "frontend_source_sha256": frontend_identity(),
        "measurement_sha256": _tree_digest(Path(__file__).parent),
        "dependencies_sha256": {
            name: sha256((WORKSPACE / name).read_bytes()).hexdigest()
            for name in ("pyproject.toml", "uv.lock")
        },
    }


def capture():
    result = {
        "system": platform.platform(),
        "cpu": platform.processor(),
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "machine": platform.machine(),
        **code_identity(),
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
