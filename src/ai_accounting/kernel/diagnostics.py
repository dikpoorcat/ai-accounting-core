"""Stable transport errors without exposing database details or submitted secrets."""

import errno
import json
import sqlite3

from .backup import BackupError
from .contracts import KernelError
from .permissions import PrivatePathError
from .runtime import RuntimeConfigurationError
from .security.primitives import IdentityError


def error_response(exc):
    if isinstance(exc, KernelError):
        return exc.response()
    if isinstance(exc, BackupError):
        return {
            "status": "rejected",
            "code": exc.code,
            "message": {
                "backup_target_exists": "备份或恢复目标已存在，请选择其他位置",
                "restore_target_exists": "恢复目标或其数据库辅助文件已存在，请选择其他位置",
                "backup_identity_mismatch": "备份中的公司或数据库身份不一致",
                "backup_content_invalid": "备份内容核验未通过，无法使用",
                "backup_manifest_invalid": "备份说明文件的字段或格式不符合要求",
                "backup_format_unsupported": "此备份格式不受当前系统支持",
                "backup_schema_unsupported": "此备份的数据库结构不受当前系统支持",
            }.get(exc.code, "备份或恢复未完成，请检查备份文件和目标位置"),
        }
    if isinstance(exc, PrivatePathError):
        return {
            "status": "rejected",
            "code": exc.code,
            "message": "内核文件权限不符合要求，请检查当前用户的文件所有权与访问权限",
        }
    if isinstance(exc, IdentityError):
        return {
            "status": "rejected",
            "code": exc.code,
            "message": "负责人身份验证未通过，请在本机安全窗口处理",
        }
    if isinstance(exc, json.JSONDecodeError):
        return {"status": "rejected", "code": "malformed_json", "message": "请求不是有效的 JSON"}
    if isinstance(exc, sqlite3.Error):
        number = getattr(exc, "sqlite_errorcode", 0) & 255
        code = {
            sqlite3.SQLITE_BUSY: "database_busy",
            sqlite3.SQLITE_LOCKED: "database_busy",
            sqlite3.SQLITE_READONLY: "storage_not_writable",
            sqlite3.SQLITE_FULL: "storage_full",
            sqlite3.SQLITE_IOERR: "storage_io_error",
            sqlite3.SQLITE_CANTOPEN: "storage_unavailable",
            sqlite3.SQLITE_CORRUPT: "database_corrupt",
            sqlite3.SQLITE_NOTADB: "database_corrupt",
            sqlite3.SQLITE_CONSTRAINT: "accounting_constraint",
        }.get(number, "database_error")
        return {"status": "rejected", "code": code, "message": "数据库操作未完成，账务事务已回滚"}
    if isinstance(exc, PermissionError):
        code = "storage_not_writable"
    elif isinstance(exc, OSError):
        code = "storage_full" if exc.errno == errno.ENOSPC else "storage_unavailable"
    elif isinstance(exc, RuntimeConfigurationError):
        code = "runtime_unsupported"
    elif isinstance(exc, (ValueError, TypeError, KeyError)):
        code = "internal_error"
    else:
        code = "internal_error"
    return {"status": "rejected", "code": code, "message": "请求未完成，请检查输入或本机运行状态"}


_JOB_MESSAGES = {
    "interrupted_retry_exhausted": "任务中断且自动重试次数已用尽，可检查后手动重试",
    "database_busy": "数据库暂时忙碌，可稍后重试",
    "storage_not_writable": "任务无法写入目标位置，请检查本机文件权限",
    "storage_full": "存储空间不足，任务未完成",
    "storage_io_error": "存储读写失败，任务未完成",
    "storage_unavailable": "任务所需的本机文件或目录不可用",
    "database_corrupt": "公司数据库完整性检查未通过",
    "backup_target_exists": "备份目标已存在，请选择其他位置",
    "backup_identity_mismatch": "备份中的公司身份不一致",
    "backup_content_invalid": "备份内容核验未通过",
    "backup_manifest_invalid": "备份说明文件格式不符合要求",
    "backup_format_unsupported": "备份格式不受当前系统支持",
    "backup_schema_unsupported": "备份数据库结构不受当前系统支持",
}


def job_error_code(exc: BaseException) -> str:
    """Classify a worker exception without persisting its text as a public code."""
    code = error_response(exc)["code"]
    return code if code in _JOB_MESSAGES else "job_failed"


def public_job_code(code: str | None) -> str:
    """Treat unknown stored codes as local diagnostics, never public text."""
    return code if code in _JOB_MESSAGES else "job_failed"


def job_error_message(code: str | None) -> str | None:
    if code is None:
        return None
    return _JOB_MESSAGES.get(code, "后台任务未完成，请检查任务设置或本机运行状态")


_OPERATION_MESSAGES = {
    **_JOB_MESSAGES,
    "company_exists": "公司已存在，无需重复创建",
    "company_mismatch": "公司身份与待处理数据库不一致",
    "company_path_mismatch": "公司数据库位置与登记信息不一致",
    "company_operation_busy": "公司操作正在进行，请稍后查看结果",
    "restore_source_changed": "恢复来源已变化，请重新提交恢复操作",
    "database_busy": "数据库暂时忙碌，请稍后重试",
}


def operation_error(code: str | None) -> tuple[str | None, str | None]:
    if code is None:
        return None, None
    safe = code if code in _OPERATION_MESSAGES else "operation_failed"
    return safe, _OPERATION_MESSAGES.get(safe, "公司操作未完成，请检查本机运行状态")
