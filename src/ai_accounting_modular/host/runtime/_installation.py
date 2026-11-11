"""Keep a catalog's CLI, MCP and resident on its explicitly installed runtime."""

import subprocess
import sys
from pathlib import Path

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.storage.public import Storage

from . import _state

FORMAT = "ai-accounting-installed-runtime/1"


def bound_python(catalog):
    catalog = Path(catalog)
    if not catalog.is_absolute():
        raise ContractError("invalid_path", "请用 --catalog 明确目录库的绝对路径。")
    catalog = _state.catalog_path(catalog)
    binding = _state._checked(catalog.with_name(catalog.name + ".runtime.json"))
    if not binding.exists():
        return None
    try:
        with binding.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError("oversized runtime binding")
        value = loads_json(raw)
        if not isinstance(value, dict) or set(value) != {
            "format",
            "catalog_path",
            "catalog_id",
            "python_path",
        }:
            raise ValueError("invalid runtime binding fields")
        if value["format"] != FORMAT:
            raise ValueError("unsupported runtime binding")
        expected_catalog = Path(value["catalog_path"])
        python = Path(value["python_path"])
        if not expected_catalog.is_absolute() or not python.is_absolute():
            raise ValueError("runtime binding paths must be absolute")
        if _state.catalog_path(expected_catalog) != catalog:
            raise ValueError("runtime binding belongs to another catalog path")
        # Keep a virtual environment's interpreter entry, including its final symlink.
        python = python.parent.resolve() / python.name
        if not python.is_file():
            raise ValueError("installed Python is missing")
    except (ContractError, OSError, TypeError, ValueError) as error:
        raise ContractError(
            "invalid_runtime_binding", "本机运行包绑定无效，请核对目录库旁的运行包清单。"
        ) from error
    if value["catalog_id"] != Storage(catalog).catalog_identity():
        raise ContractError("invalid_runtime_binding", "本机运行包绑定与当前目录身份不一致。")
    return python


def forward_bound_runtime(catalog):
    python = bound_python(catalog)
    current = Path(sys.executable)
    if python is None or python == current.parent.resolve() / current.name:
        return None
    try:
        return subprocess.call(
            [
                str(python),
                "-I",
                "-B",
                "-X",
                "utf8",
                "-m",
                "ai_accounting_modular.host.runtime.cli",
                *sys.argv[1:],
            ],
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as error:
        raise ContractError(
            "invalid_runtime_binding", "无法启动绑定的本机程序，请核对安装位置和权限。"
        ) from error
