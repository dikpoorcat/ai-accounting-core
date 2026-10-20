"""Controlled task paths and verification of the exact bytes being delivered."""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path, PurePosixPath
from tempfile import SpooledTemporaryFile

from ai_accounting_modular.contracts.public import ContractError


def relative(value: str) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 1000
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ContractError("unsafe_path", "文件位置必须是受控根内的明确相对路径。")
    return PurePosixPath(value).parts


def controlled(root: Path, *parts: str, directory: bool = False, create: bool = False) -> Path:
    if root.is_symlink() or root.is_junction():
        raise ContractError("unsafe_path", "受控文件根不能被链接替换。")
    root = root.resolve()
    path = root
    for part in parts:
        for item in relative(part):
            path = path / item
            if path.is_symlink() or path.is_junction():
                raise ContractError("unsafe_path", "文件位置不能通过链接越过受控目录。")
            if not path.resolve().is_relative_to(root):
                raise ContractError("unsafe_path", "文件位置已经离开受控目录。")
    if directory:
        if create:
            # Resolve every existing parent before mkdir follows it.
            cursor = root
            for part in path.relative_to(root).parts:
                cursor = cursor / part
                if cursor.is_symlink() or cursor.is_junction():
                    raise ContractError("unsafe_path", "任务目录不能经过链接。")
                cursor.mkdir(exist_ok=True)
        if path.exists() and not path.is_dir():
            raise ContractError("unsafe_path", "任务位置不是目录。")
    return path


def file_name(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 255
        or re.search(r'[\\/:*?"<>|\x00-\x1f]', value)
        or value in {".", ".."}
        or value.endswith((" ", "."))
    ):
        raise ContractError("invalid_input", "生产者须提供明确且安全的交付文件名。")


def remove_task_root(path: Path) -> None:
    """Remove only a derived delivery root; never follow a child link."""
    if path.is_symlink() or path.is_junction():
        raise ContractError("unsafe_path", "不能清理被链接替换的任务目录。")
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_junction():
            raise ContractError("unsafe_path", "任务临时目录中存在未允许的目录链接。")
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            remove_task_root(child)
    path.rmdir()


def captured_bytes(path: Path, expected_digest: str, expected_size: int) -> None:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    if size != expected_size or digest.hexdigest() != expected_digest:
        raise ContractError("capture_invalid", "捕获输入的字节数或摘要与已保存计划不符。")


def snapshot_file(path: Path):
    """Return an independent, rewindable verified-byte snapshot, bounded in memory."""
    snapshot = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
                snapshot.write(chunk)
        snapshot.seek(0)
        return io.BufferedReader(snapshot), digest.hexdigest(), size
    except BaseException:
        snapshot.close()
        raise
