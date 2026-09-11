"""Stable transport errors without exposing database details or submitted secrets."""

import json
import sqlite3

from .contracts import KernelError
from .runtime import RuntimeConfigurationError
from .security.primitives import IdentityError


def error_response(exc):
    if isinstance(exc, KernelError):
        return exc.response()
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
