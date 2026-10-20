"""Per-scope process and OS locks, released automatically when a worker dies."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock

from ai_accounting_modular.contracts.public import ContractError

_registry_guard = Lock()
_guards: dict[tuple[Path, int], RLock] = {}


@contextmanager
def locked(path: Path, part: int, *, blocking: bool = True):
    # POSIX record locks are process-owned: closing a second descriptor for the
    # same file would release its other locks. Separate files keep the two
    # independent capabilities safe on both Windows and POSIX.
    path = path.with_name(path.name + f".{part}")
    key = (path, part)
    with _registry_guard:
        guard = _guards.setdefault(key, RLock())
    if not guard.acquire(blocking=blocking):
        yield False
        return
    stream = None
    acquired = False
    try:
        if path.is_symlink() or path.is_junction():
            raise ContractError("unsafe_path", "文件任务锁不能经过链接。")
        stream = path.open("a+b")
        if stream.seek(0, os.SEEK_END) < 2:
            stream.write(b"\0\0")
            stream.flush()
        stream.seek(part)
        if os.name == "nt":
            import msvcrt

            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(stream.fileno(), mode, 1)
                acquired = True
            except OSError as exc:
                if blocking:
                    raise ContractError("busy", "文件范围正在完成交付，请稍后接续。") from exc
        else:
            import fcntl

            mode = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.lockf(stream.fileno(), mode, 1, part)
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        if stream is not None:
            try:
                if acquired:
                    stream.seek(part)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.lockf(stream.fileno(), fcntl.LOCK_UN, 1, part)
            finally:
                stream.close()
        guard.release()
