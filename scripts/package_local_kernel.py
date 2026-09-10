"""Build a Windows runtime from the already installed, controlled repository venv.

Only allowlisted application sources, public dependency distributions, standard
Python files and the built frontend are copied. No repository-wide copy occurs.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata as metadata
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPOSITORY = Path(__file__).resolve().parents[1]
ROOT_DEPENDENCIES = ("mcp", "pydantic", "openpyxl", "xlwt", "xlrd")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_file(source: Path, target: Path):
    if source.is_symlink():
        raise ValueError(f"Symlinks are not included in the runtime: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def dependency_closure():
    found = {}
    expanded_extras = {}
    pending = [Requirement(name) for name in ROOT_DEPENDENCIES]
    while pending:
        requested = pending.pop()
        name = canonicalize_name(requested.name)
        distribution = metadata.distribution(name)
        if requested.specifier and distribution.version not in requested.specifier:
            raise ValueError(f"Installed dependency does not satisfy {requested}")
        earlier_extras = expanded_extras.get(name)
        if earlier_extras is not None and requested.extras <= earlier_extras:
            continue
        active_extras = (earlier_extras or set()) | requested.extras
        expanded_extras[name] = active_extras
        found[name] = distribution
        for dependency in distribution.requires or ():
            requirement = Requirement(dependency)
            if requirement.marker is None or any(
                requirement.marker.evaluate({"extra": extra}) for extra in {"", *active_extras}
            ):
                pending.append(requirement)
    return found


def copy_runtime(output: Path):
    base = Path(sys.base_prefix).resolve()
    runtime = output / "runtime"
    runtime.mkdir()
    for source in base.iterdir():
        if source.is_file() and (
            source.suffix in {".exe", ".dll"} or source.name in {"BUILD", "LICENSE.txt"}
        ):
            copy_file(source, runtime / source.name)
    for directory in ("DLLs", "Lib"):
        for source in (base / directory).rglob("*"):
            relative = source.relative_to(base)
            if source.is_file() and not {"site-packages", "__pycache__"} & set(relative.parts):
                copy_file(source, runtime / relative)
    # Windows' embedded-path mechanism ignores registry, PYTHONPATH, PYTHONHOME
    # and user site-packages. Every import path remains relative to this bundle.
    (runtime / "python312._pth").write_text(
        "python312.zip\nDLLs\nLib\nLib/site-packages\n"
        "Lib/site-packages/win32\nLib/site-packages/win32/lib\n"
        "Lib/site-packages/pythonwin\n../app\nimport site\n",
        encoding="ascii",
    )
    return runtime


def copy_dependencies(output: Path):
    installed = Path(sysconfig.get_path("purelib")).resolve()
    target = output / "runtime/Lib/site-packages"
    dependencies = dependency_closure()
    for distribution in dependencies.values():
        for member in distribution.files or ():
            source = Path(distribution.locate_file(member)).resolve()
            if not source.is_relative_to(installed) or not source.is_file():
                continue
            relative = source.relative_to(installed)
            if (
                "__pycache__" in relative.parts
                or source.suffix in {".pth", ".pyc", ".egg-link"}
                or source.name == "direct_url.json"
            ):
                continue
            copy_file(source, target / relative)
    # pywin32 normally installs search paths via executable .pth code. Keep
    # that code excluded: explicit _pth entries and local DLLs provide the
    # required MCP Windows process support without importing host locations.
    for source in (target / "pywin32_system32").glob("*.dll"):
        copy_file(source, output / "runtime" / source.name)
    return {name: distribution.version for name, distribution in sorted(dependencies.items())}


def copy_application(output: Path):
    # Exercise all local public entry modules and generated schemas to discover
    # their shared pure imports. Optional dependency distributions are handled
    # separately above; legacy ORM/services are deliberately not imported.
    for name in ("cli", "http", "mcp", "exports", "reports", "workflow"):
        importlib.import_module("ai_accounting.kernel." + name)
    from ai_accounting.kernel.service import default_registry

    default_registry().schemas()
    source_package = REPOSITORY / "src/ai_accounting"
    selected = set((source_package / "kernel").rglob("*.py"))
    selected.update((source_package / "payroll").rglob("*.py"))
    for name, module in tuple(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if name.startswith("ai_accounting") and path:
            source = Path(path).resolve()
            if not source.is_relative_to(source_package) or source.suffix != ".py":
                raise ValueError("Application imports escaped the repository's source package")
            selected.add(source)
    if any(
        path.name in {"models.py", "database.py", "service.py"} and path.parent == source_package
        for path in selected
    ):
        raise ValueError("The local runtime unexpectedly imports legacy persistence")
    for source in sorted(selected):
        copy_file(source, output / "app/ai_accounting" / source.relative_to(source_package))
    from ai_accounting.financial_statement_template import TEMPLATE_FILE_NAME, _template_bytes

    _template_bytes()  # verifies the checked-in, blank report-template digest
    template = source_package / "templates/financial_reports" / TEMPLATE_FILE_NAME
    copy_file(
        template, output / "app/ai_accounting/templates/financial_reports" / TEMPLATE_FILE_NAME
    )
    frontend = REPOSITORY / "frontend/dist"
    if not (frontend / "local.html").is_file():
        raise ValueError("Build the frontend before packaging: npm run build")
    for source in frontend.rglob("*"):
        if source.is_file():
            copy_file(source, output / "frontend/dist" / source.relative_to(frontend))
    return sorted(path.relative_to(source_package).as_posix() for path in selected)


def bundle_manifest(output, dependencies, modules):
    probe = subprocess.run(
        [
            str(output / "runtime/python.exe"),
            "-I",
            "-X",
            "utf8",
            "-c",
            "import json,sqlite3,sys; from ai_accounting.kernel.build import calculator_build_id; "
            "print(json.dumps({'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,'build_id':calculator_build_id(),'isolated':sys.flags.isolated}))",
        ],
        cwd=output,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    runtime = json.loads(probe.stdout)
    if runtime["python"] != "3.12.13" or runtime["sqlite"] != "3.53.1" or runtime["isolated"] != 1:
        raise ValueError("Packaged interpreter does not match the controlled runtime")
    files = {
        path.relative_to(output).as_posix(): {"sha256": sha256(path), "bytes": path.stat().st_size}
        for path in sorted(output.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
    return {
        "format": "ai-accounting-local-runtime",
        "format_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "runtime": runtime,
        "dependencies": dependencies,
        "application_modules": modules,
        "files": files,
        "data_policy": "software only; company data and credentials are excluded",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-validation", action="store_true")
    args = parser.parse_args()
    if (
        sys.platform != "win32"
        or sys.version_info[:3] != (3, 12, 13)
        or sqlite3.sqlite_version != "3.53.1"
    ):
        raise SystemExit("Use the controlled Windows Python 3.12.13 / SQLite 3.53.1 runtime")
    output = args.output.resolve()
    archive_path = output.with_name(output.name + ".zip")
    relocated = output.with_name(output.name + "-relocated")
    if output.exists() or archive_path.exists() or relocated.exists():
        raise SystemExit("Packaging targets must be absent; existing builds are never overwritten")
    output.mkdir(parents=True)
    copy_runtime(output)
    dependencies = copy_dependencies(output)
    modules = copy_application(output)
    (output / "finance-local.cmd").write_text(
        '@echo off\nsetlocal\n"%~dp0runtime\\python.exe" -I -X utf8 '
        "-m ai_accounting.kernel.cli %*\nexit /b %errorlevel%\n",
        encoding="ascii",
    )
    (output / "finance-local.ps1").write_text(
        '& (Join-Path $PSScriptRoot "runtime/python.exe") -I -X utf8 '
        "-m ai_accounting.kernel.cli @args\nexit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    copy_file(
        REPOSITORY / "scripts/verify_local_package.py", output / "tools/verify_local_package.py"
    )
    (output / "使用说明.txt").write_text(
        "本地会计内核运行包（Windows x64）\n\n"
        "不需要安装 Python、SQLite、PostgreSQL 或 Node.js。\n"
        "所有命令通过包内 finance-local.cmd 或 finance-local.ps1 运行。\n"
        "示例：finance-local.cmd --root D:\\会计资料 serve\n"
        "MCP：finance-local.cmd --root D:\\会计资料 mcp\n"
        "命令说明：finance-local.cmd --help\n"
        "公司数据由 --root 指定，运行包中没有任何公司账务和登录凭据。\n"
        "不要直接复制活动 SQLite 文件作为备份，请使用内核 backup 命令。\n",
        encoding="utf-8",
    )
    manifest = bundle_manifest(output, dependencies, modules)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with zipfile.ZipFile(
        archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                if path.suffix in {".sqlite", ".sqlite3", ".env"} or path.name == "pyvenv.cfg":
                    raise ValueError("A non-software file entered the package")
                archive.write(path, path.relative_to(output).as_posix())
    validation = None
    if not args.skip_validation:
        relocated.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(relocated)  # own freshly generated, relative-only software archive
        poisoned = dict(
            os.environ,
            PYTHONPATH=str(REPOSITORY / "not-an-import-path"),
            PYTHONHOME=str(REPOSITORY / "not-a-python-home"),
            PYTHONNOUSERSITE="1",
        )
        result = subprocess.run(
            [
                str(relocated / "runtime/python.exe"),
                "-I",
                "-X",
                "utf8",
                str(relocated / "tools/verify_local_package.py"),
            ],
            cwd=relocated,
            env=poisoned,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode:
            raise RuntimeError("Relocated runtime verification failed:\n" + result.stderr)
        validation = json.loads(result.stdout)
        (output.parent / (output.name + "-verification.json")).write_text(
            json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "package": str(output),
                "archive": str(archive_path),
                "archive_bytes": archive_path.stat().st_size,
                "software_bytes": sum(item["bytes"] for item in manifest["files"].values()),
                "runtime": manifest["runtime"],
                "relocated": str(relocated) if validation else None,
                "validation": validation,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
