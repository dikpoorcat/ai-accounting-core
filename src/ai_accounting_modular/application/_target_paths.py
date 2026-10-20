"""Only explicitly owned isolated targets and fixed backup input paths."""

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

from ai_accounting_modular.contracts.public import ContractError


def checked(path):
    if "\x00" in os.fspath(path):
        raise ContractError("invalid_path", "资料位置不能包含空字符。")
    path = Path(path).absolute()
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink() or parent.is_junction():
            raise ContractError("invalid_path", "资料位置不能经过符号链接或目录联接。")
    return path


def beneath(root, relative):
    parts = PurePosixPath(relative)
    if (
        not relative
        or not parts.parts
        or "\\" in relative
        or ":" in relative
        or "\x00" in relative
        or parts.is_absolute()
        or any(part in {".", ".."} for part in parts.parts)
        or parts.as_posix() != relative
    ):
        raise ContractError("invalid_path", "资料位置必须是受控根内的规范相对路径。")
    root = checked(root)
    path = checked(root.joinpath(*parts.parts))
    if not path.resolve().is_relative_to(root.resolve()):
        raise ContractError("invalid_path", "资料位置不能越过指定目录。")
    return path


def file_digest(path):
    path = checked(path)
    with path.open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ContractError("invalid_path", "备份资料必须是普通文件。")
        digest, size = hashlib.sha256(), 0
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def copy_file(source, destination, expected_digest, expected_size):
    source, destination = checked(source), checked(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with source.open("rb") as origin, destination.open("xb") as output:
        if not stat.S_ISREG(os.fstat(origin.fileno()).st_mode):
            raise ContractError("invalid_path", "离线资料必须是普通文件。")
        while chunk := origin.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            if size > expected_size:
                raise ContractError("source_changed", "复制时源资料大小已变化。")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    checked(source)
    checked(destination)
    if (digest.hexdigest(), size) != (expected_digest, expected_size):
        raise ContractError("source_changed", "复制时源资料字节已变化。")
