"""Stable transport errors without exposing database details or submitted secrets."""

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
        code = "storage_unavailable"
    elif isinstance(exc, RuntimeConfigurationError):
        code = "runtime_unsupported"
    elif isinstance(exc, (ValueError, TypeError, KeyError)):
        code = "invalid_command"
    else:
        code = "internal_error"
    return {"status": "rejected", "code": code, "message": "请求未完成，请检查输入或本机运行状态"}
