"""Strict plan identity and controlled input metadata; no financial calculations."""

from __future__ import annotations

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    validate_contract,
)

from . import _paths
from ._types import FileScope


def copy_json(value):
    return loads_json(canonical_json(value))


def describe(producer, plan):
    scope = producer.describe(copy_json(plan))
    if not isinstance(scope, FileScope) or not isinstance(scope.key, str) or not scope.key:
        raise ContractError("invalid_input", "生产者须声明明确文件范围。")
    if plan["kind"] == "backup" and scope.key != "company":
        raise ContractError("invalid_input", "整公司备份生产者须使用唯一 company 范围。")
    validate_contract("core.schema.json", "Digest", scope.head)
    _paths.file_name(scope.file_name)
    if scope.head != plan["scope_head"]:
        raise ContractError("conflict", "生产者语义头与文件计划不一致。")
    return scope


def is_current(producer, plan, context):
    return current_guard(producer, plan, context)()


def current_guard(producer, plan, context):
    """Prepare a fresh predicate input before taking SQLite's writer lock."""
    isolated = copy_json(plan)

    def check():
        value = producer.is_current(isolated, context)
        if type(value) is not bool:
            raise ContractError("invalid_input", "生产者须明确判断文件计划是否有效。")
        return value

    return check


def validate(metadata, value):
    plan = validate_contract("core.schema.json", "FilePlan", copy_json(value))
    identity = (metadata.company_id, metadata.database_id)
    if (plan["company_id"], plan["database_id"]) != identity:
        raise ContractError("conflict", "文件计划不属于当前公司数据库。")
    if plan["kind"] == "backup":
        manifest = plan["manifest"]
        if (manifest["company_id"], manifest["database_id"]) != identity:
            raise ContractError("conflict", "备份清单与计划的公司数据库身份不一致。")
        inputs = {}
        for item in plan["inputs"]:
            _paths.relative(item["relative_path"])
            _paths.relative(item["archive_path"])
            if item["archive_path"] in inputs:
                raise ContractError("invalid_input", "备份包内位置不能重复。")
            inputs[item["archive_path"]] = (item["sha256"], item["byte_size"])
        files = {}
        for item in manifest["files"]:
            _paths.relative(item["path"])
            if item["path"] in files:
                raise ContractError("invalid_input", "备份清单不能重复列出同一位置。")
            files[item["path"]] = (item["sha256"], item["byte_size"])
        if inputs != files:
            raise ContractError("invalid_input", "捕获输入须完整对应备份清单的每项字节。")
    else:
        snapshot = plan["snapshot"]
        if (
            snapshot["scope"]["company_id"] != metadata.company_id
            or (snapshot["company_id"], snapshot["database_id"]) != identity
        ):
            raise ContractError("conflict", "报表快照不属于当前公司数据库。")
    return plan


def load(metadata, row, producer):
    try:
        plan = validate(metadata, loads_json(row["plan_json"]))
        expected = "BackupPlan" if row["kind"] == "backup" else "ReportExportPlan"
        if (
            row["plan_schema"] != expected
            or plan["kind"] != row["kind"]
            or plan["scope_head"] != row["scope_head"]
            or plan.get("capture_id") != row["capture_id"]
            or describe(producer, plan).key != row["scope_key"]
        ):
            raise ValueError("plan_identity")
        return plan
    except (ContractError, ValueError, TypeError) as exc:
        raise ContractError("storage_corrupt", "保存的文件计划与任务身份不一致。") from exc
