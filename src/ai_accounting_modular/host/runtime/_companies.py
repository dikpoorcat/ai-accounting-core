"""Explicit installation operations; never part of AI's daily accounting command surface."""

from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.storage.public import CompanySpec

from ._state import _checked


def _paths(request, required):
    if (
        not isinstance(request, dict)
        or not required <= request.keys()
        or set(request) - required - {"backup_directory"}
    ):
        raise ContractError("invalid_input", "安装资料缺少必要字段或含无关字段。")
    if any(not isinstance(request[key], str) for key in required):
        raise ContractError("invalid_input", "公司身份、月份和位置须用文本表示。")
    root = Path(request["company_root"])
    if not root.is_absolute():
        raise ContractError("invalid_path", "公司资料根须为绝对路径。")
    backup = request.get("backup_directory")
    if backup is not None:
        if not isinstance(backup, str) or not Path(backup).is_absolute():
            raise ContractError("invalid_path", "备份位置须为绝对路径。")
        backup = _checked(Path(backup))
    return _checked(root), backup


def _result(company):
    return {
        key: str(value) if isinstance(value, Path) else value
        for key, value in asdict(company).items()
    }


def create(storage, request):
    root, backup = _paths(request, {"credit_code", "legal_name", "start_period", "company_root"})
    if root.exists():
        raise ContractError("target_exists", "新公司资料根已经存在，不能覆盖。")
    return _result(
        storage.create_company(
            CompanySpec(
                str(uuid4()),
                str(uuid4()),
                request["credit_code"],
                request["legal_name"],
                request["start_period"],
                root,
                backup,
            )
        )
    )


def register(storage, request):
    root, backup = _paths(request, {"company_root"})
    return _result(storage.register_company(root, backup_directory=backup))
