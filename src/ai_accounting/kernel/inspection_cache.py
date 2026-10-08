"""Disposable source parsing files, separate from every formal accounting check.

One resident service owns one manager and one most-recent parsed document.
The disk files contain parsing output only, never adoption or completion state.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .contracts import KernelError
from .permissions import (
    assert_private_directory,
    assert_private_file,
    create_private_file,
    ensure_private_directory,
)
from .runtime import private_file_lock
from .stored_json import loads_unique
from .types import canonical, digest

CACHE_FORMAT = "ai-accounting-material-inspection/1"
MAX_CACHE_BYTES = 512 * 1024 * 1024
_CACHE_NAME = re.compile(r"inspection-[0-9a-f]{64}(?:\.used)?\.json")
_TEMP_NAME = re.compile(r"inspection-write-[0-9a-f]{32}\.tmp")


class InspectionCache:
    """Best-effort reading cache; a caller borrows and must not mutate its result."""

    def __init__(self, root: str | Path, *, build_id: str, max_bytes: int = MAX_CACHE_BYTES):
        if not isinstance(build_id, str) or not build_id:
            raise ValueError("an explicit parser build identity is required")
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("cache byte limit must be a nonnegative integer")
        # Cache availability is not a service startup gate. Its private path
        # is checked lazily before every disk read or write.
        self.directory = Path(os.path.abspath(Path(root) / ".inspection-cache"))
        self.build_id = build_id
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._last_key: str | None = None
        self._last_result: dict | None = None
        self._cleaned_temporaries = False

    def inspect(
        self,
        *,
        company_id: str,
        database_id: str,
        evidence_digest: str,
        specification: dict,
        raw: bytes,
        parse: Callable[[], dict],
    ) -> dict:
        # Check the current database source even on a hot cache hit. A damaged
        # original is an integrity failure, never a disposable cache miss.
        actual_digest = hashlib.sha256(raw).hexdigest()
        if actual_digest != evidence_digest:
            raise KernelError(
                "content_integrity_failed",
                "已保存的资料原件摘要不一致",
                component="evidence",
                record_id=evidence_digest,
                reason="evidence_digest_mismatch",
            )
        identity = {
            "cache_format": CACHE_FORMAT,
            "company_id": company_id,
            "database_id": database_id,
            "evidence_digest": actual_digest,
            "specification": specification,
            "parser_build": self.build_id,
        }
        key = digest(identity).hex()
        with self._lock:
            if self._last_key == key:
                self._try_touch(key)
                return self._last_result
            result = self._read(key, identity)
            if result is None:
                result = parse()
                self._try_store(key, identity, result)
            else:
                self._try_touch(key)
            self._last_key, self._last_result = key, result
            return result

    def _path(self, key: str, *, usage: bool = False) -> Path:
        return self.directory / f"inspection-{key}{'.used' if usage else ''}.json"

    def _read(self, key: str, identity: dict) -> dict | None:
        try:
            assert_private_directory(self.directory)
            path = assert_private_file(self._path(key))
            if path.stat().st_size > self.max_bytes:
                return None
            envelope = loads_unique(path.read_bytes())
            result = envelope["result"]
            if (
                envelope["cache_format"] != CACHE_FORMAT
                or envelope["identity"] != identity
                or envelope["key"] != key
                or envelope["result_digest"] != digest(result).hex()
                or not isinstance(result, dict)
                or any(
                    not isinstance(result.get(name), list)
                    or any(not isinstance(item, dict) for item in result[name])
                    for name in ("items", "issues", "coverage", "control_totals")
                )
            ):
                return None
            return result
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return None

    def _usage_bytes(self, key: str) -> bytes:
        return canonical(
            {"cache_format": CACHE_FORMAT, "key": key, "last_used_ns": time.time_ns()}
        ).encode("utf-8")

    def _try_touch(self, key: str) -> None:
        try:
            assert_private_directory(self.directory)
            with private_file_lock(self.directory / ".cache.lock") as acquired:
                if acquired:
                    if not self._cleaned_temporaries:
                        self._clear_temporaries()
                    assert_private_file(self._path(key))
                    self._atomic_write(self._path(key, usage=True), self._usage_bytes(key))
        except (OSError, ValueError):
            pass

    def _try_store(self, key: str, identity: dict, result: dict) -> None:
        try:
            payload = canonical(
                {
                    "cache_format": CACHE_FORMAT,
                    "key": key,
                    "identity": identity,
                    "result_digest": digest(result).hex(),
                    "result": result,
                }
            ).encode("utf-8")
            usage = self._usage_bytes(key)
            size = len(payload) + len(usage)
            if size > self.max_bytes:
                return
            ensure_private_directory(self.directory)
            with private_file_lock(self.directory / ".cache.lock") as acquired:
                if not acquired or not self._make_room(key, size):
                    return
                self._atomic_write(self._path(key), payload)
                self._atomic_write(self._path(key, usage=True), usage)
        except (OSError, ValueError, TypeError, RecursionError):
            # The result remains usable for this request and the one hot entry.
            pass

    def _make_room(self, key: str, size: int) -> bool:
        """Evict only private files in this tool's dedicated filename namespace."""
        assert_private_directory(self.directory)
        self._clear_temporaries()
        entries = []
        total = 0
        for path in self.directory.iterdir():
            if not _CACHE_NAME.fullmatch(path.name) or ".used.json" in path.name:
                continue
            try:
                assert_private_file(path)
                used_path = path.with_suffix(".used.json")
                entry_size = path.stat().st_size
                last_used = 0
                if used_path.exists():
                    assert_private_file(used_path)
                    entry_size += used_path.stat().st_size
                    if used_path.stat().st_size <= 1024:
                        usage = loads_unique(used_path.read_bytes())
                        candidate = usage.get("last_used_ns")
                        if (
                            usage.get("cache_format") == CACHE_FORMAT
                            and usage.get("key") == path.stem.removeprefix("inspection-")
                            and type(candidate) is int
                            and candidate >= 0
                        ):
                            last_used = candidate
                total += entry_size
                entries.append((last_used, path.name, path, used_path, entry_size))
            except (OSError, ValueError, TypeError, AttributeError):
                # A corrupt usage file makes the result oldest, not invisible
                # to the quota. Nonprivate files are neither read nor removed.
                if path.exists():
                    try:
                        assert_private_file(path)
                        entry_size = path.stat().st_size
                        used_path = path.with_suffix(".used.json")
                        if used_path.exists():
                            entry_size += assert_private_file(used_path).stat().st_size
                        total += entry_size
                        entries.append((0, path.name, path, used_path, entry_size))
                    except OSError:
                        return False
        # Orphaned usage files can be left by a interrupted disk write. They
        # contain no parsing result and can be removed in this same namespace.
        for path in self.directory.iterdir():
            if _CACHE_NAME.fullmatch(path.name) and path.name.endswith(".used.json"):
                source = path.with_name(path.name.removesuffix(".used.json") + ".json")
                if not source.exists():
                    self._remove(path)
        current = self._path(key)
        for _, _, path, used_path, entry_size in sorted(
            entries, key=lambda item: (item[2] != current, item[0], item[1])
        ):
            if path != current and total + size <= self.max_bytes:
                break
            self._remove(path)
            self._remove(used_path)
            total -= entry_size
        return total + size <= self.max_bytes

    def _clear_temporaries(self) -> None:
        # Called only while holding the process disk lock, so no live cache
        # writer can own these interrupted atomic-write files.
        assert_private_directory(self.directory)
        for path in self.directory.iterdir():
            if _TEMP_NAME.fullmatch(path.name):
                self._remove(path)
        self._cleaned_temporaries = True

    def _remove(self, path: Path) -> None:
        assert_private_directory(self.directory)
        if path.parent != self.directory or not (
            _CACHE_NAME.fullmatch(path.name) or _TEMP_NAME.fullmatch(path.name)
        ):
            raise ValueError("unexpected parsing cache filename")
        if path.exists():
            assert_private_file(path).unlink()

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        assert_private_directory(self.directory)
        if path.parent != self.directory or not _CACHE_NAME.fullmatch(path.name):
            raise ValueError("unexpected parsing cache filename")
        if path.exists():
            assert_private_file(path)
        temporary = create_private_file(self.directory / f"inspection-write-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                assert_private_directory(self.directory)
                assert_private_file(temporary).unlink()
