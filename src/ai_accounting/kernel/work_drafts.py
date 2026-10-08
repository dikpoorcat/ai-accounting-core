"""Private durable working documents, separate from replaceable parse caches."""

from __future__ import annotations

import base64
import binascii
import os
import re
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .contracts import KernelError
from .materials import PageLimit
from .permissions import (
    assert_private_directory,
    assert_private_file,
    create_private_file,
    ensure_private_directory,
    reject_reparse_path,
)
from .runtime import private_file_lock
from .stored_json import loads_unique
from .types import YearMonth, canonical, digest
from .work_context import WorkArea
from .work_draft_contract import (
    WorkDraft,
    draft_json,
    preserve_pending_requests,
    target_warnings,
    validate_targets,
)

WORK_DRAFT_FORMAT = "ai-accounting-work-draft/1"
MAX_WORK_DRAFT_BYTES = 8 * 1024 * 1024
_AREAS = frozenset({"bank", "payroll", "transactions", "tax", "assets", "financing"})
_NAME = re.compile(
    r"([0-9]{4}-(?:0[1-9]|1[0-2]))--(bank|payroll|transactions|tax|assets|financing)\.json"
)
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class WorkDraftStore:
    def __init__(
        self,
        root: str | Path,
        catalog_instance_id: str,
        company_id: str,
        database_id: str,
        *,
        registry,
        commands,
        request_result=None,
    ):
        if any(
            not isinstance(value, str) or not value
            for value in (catalog_instance_id, company_id, database_id)
        ):
            raise ValueError("work draft storage requires explicit bound identities")
        self.identity = {
            "catalog_instance_id": catalog_instance_id,
            "company_id": company_id,
            "database_id": database_id,
        }
        self.base = Path(os.path.abspath(Path(root) / ".work-drafts"))
        self.directory = self.base / digest(self.identity).hex()
        self.company_id, self.database_id = company_id, database_id
        self.registry, self.commands = registry, commands
        self.request_result = request_result

    def _scope(self, period, work_area):
        period = str(YearMonth(period))
        if work_area not in _AREAS:
            raise KernelError("work_draft_scope_invalid", "工作稿必须指定现有清单事项")
        return period, work_area

    def _response(self, period, work_area):
        return {
            "schema_version": 1,
            "company_id": self.company_id,
            "database_id": self.database_id,
            "period": period,
            "work_area": work_area,
        }

    def _directory(self, *, create=False):
        # exists() is false for dangling links. Check lexical components before
        # treating any namespace as absent or creating a replacement.
        reject_reparse_path(self.directory)
        if create:
            for path in (self.base, self.directory):
                if path.exists():
                    assert_private_directory(path)
                else:
                    try:
                        ensure_private_directory(path)
                    except FileExistsError:
                        assert_private_directory(path)
                    self._sync_path(path.parent)
        elif self.directory.exists():
            assert_private_directory(self.base)
            assert_private_directory(self.directory)
        else:
            if self.base.exists():
                assert_private_directory(self.base)
            return False
        return True

    @contextmanager
    def _locked(self, period, work_area, *, create=False):
        path = self.directory / f"{period}--{work_area}.json"
        key = str(path)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
        with lock:
            if not self._directory(create=create):
                yield path
                return
            with private_file_lock(path.with_suffix(".lock")) as acquired:
                if not acquired:
                    raise KernelError(
                        "work_draft_busy", "工作稿正被另一进程读取或保存，请重试", retryable=True
                    )
                yield path

    def _read(self, path, period, work_area):
        reject_reparse_path(path)
        if not path.exists():
            return None
        try:
            assert_private_file(path)
            if path.stat().st_size > MAX_WORK_DRAFT_BYTES:
                raise ValueError("file exceeds working document limit")
            envelope = loads_unique(path.read_bytes())
            if not isinstance(envelope, dict):
                raise ValueError("envelope must be an object")
            if envelope.get("format") != WORK_DRAFT_FORMAT:
                raise KernelError(
                    "work_draft_format_unsupported", "工作稿格式不受当前程序支持；文件已保留"
                )
            if envelope.get("identity") != self.identity:
                raise KernelError(
                    "work_draft_identity_mismatch",
                    "工作稿与当前目录、公司或数据库身份不符；文件已保留",
                )
            if set(envelope) != {
                "format",
                "identity",
                "period",
                "work_area",
                "revision",
                "draft",
                "draft_digest",
            }:
                raise ValueError("unexpected envelope fields")
            if envelope["period"] != period or envelope["work_area"] != work_area:
                raise ValueError("file scope differs from its generated path")
            revision = envelope["revision"]
            if not isinstance(revision, str) or str(uuid.UUID(revision)) != revision:
                raise ValueError("revision must be a canonical UUID")
            raw = draft_json(envelope["draft"])
            if envelope["draft_digest"] != digest(raw).hex():
                raise ValueError("draft digest mismatch")
            return envelope
        except KernelError:
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
            UnicodeError,
        ) as exc:
            raise KernelError("work_draft_corrupt", "工作稿损坏或正文结构无效；文件已保留") from exc

    def _revision(self, previous, expected_revision):
        revision = previous["revision"] if previous else None
        if expected_revision != revision:
            raise KernelError(
                "work_draft_revision_conflict",
                "工作稿版本已变化，请重新读取",
                current_revision=revision,
            )

    def list(
        self,
        period: YearMonth | None = None,
        work_area: WorkArea | None = None,
        limit: PageLimit = 100,
        cursor: str | None = None,
    ):
        if period is not None:
            period = str(YearMonth(period))
        if work_area is not None and work_area not in _AREAS:
            raise KernelError("work_draft_scope_invalid", "工作稿必须指定现有清单事项")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise KernelError("invalid_command", "工作稿分页数量须为1至500")
        try:
            names = (
                sorted(path.name for path in self.directory.iterdir() if _NAME.fullmatch(path.name))
                if self._directory()
                else []
            )
            names = [
                name
                for name in names
                if (period is None or name.startswith(period + "--"))
                and (work_area is None or name.endswith("--" + work_area + ".json"))
            ]
            scope = digest(
                {
                    "identity": self.identity,
                    "period": period,
                    "work_area": work_area,
                    "names": names,
                }
            ).hex()
            offset = 0
            if cursor is not None:
                try:
                    if not isinstance(cursor, str) or len(cursor) > 2048:
                        raise ValueError("invalid cursor")
                    value = loads_unique(
                        base64.b64decode(cursor.encode(), altchars=b"-_", validate=True)
                    )
                    if (
                        set(value) != {"scope", "offset"}
                        or type(value["offset"]) is not int
                        or not 0 <= value["offset"] <= len(names)
                    ):
                        raise ValueError("invalid cursor fields")
                    if value["scope"] != scope:
                        raise KernelError(
                            "work_draft_cursor_stale", "工作稿范围或目录已变化，请重新读取首页"
                        )
                    offset = value["offset"]
                except KernelError:
                    raise
                except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as exc:
                    raise KernelError("work_draft_cursor_invalid", "工作稿分页游标无效") from exc
            page = names[offset : offset + limit]
            next_offset = offset + len(page)
            next_cursor = (
                base64.urlsafe_b64encode(
                    canonical({"scope": scope, "offset": next_offset}).encode()
                ).decode()
                if next_offset < len(names)
                else None
            )
            return {
                "schema_version": 1,
                "company_id": self.company_id,
                "database_id": self.database_id,
                "items": [
                    {"period": _NAME.fullmatch(name)[1], "work_area": _NAME.fullmatch(name)[2]}
                    for name in page
                ],
                "limit": limit,
                "next_cursor": next_cursor,
            }
        except OSError as exc:
            raise KernelError("work_draft_unavailable", "工作稿私有目录不可读取") from exc

    def read(self, period: YearMonth, work_area: WorkArea):
        period, work_area = self._scope(period, work_area)
        try:
            with self._locked(period, work_area) as path:
                envelope = self._read(path, period, work_area)
                return {
                    **self._response(period, work_area),
                    "status": "present" if envelope else "absent",
                    "revision": envelope["revision"] if envelope else None,
                    "draft": envelope["draft"] if envelope else None,
                    "warnings": target_warnings(
                        envelope["draft"], registry=self.registry, commands=self.commands
                    )
                    if envelope
                    else [],
                }
        except OSError as exc:
            raise KernelError(
                "work_draft_unavailable", "工作稿私有文件不可读取；原文件已保留"
            ) from exc

    def save(
        self,
        period: YearMonth,
        work_area: WorkArea,
        expected_revision: str | None,
        draft: WorkDraft,
    ):
        period, work_area = self._scope(period, work_area)
        try:
            raw = draft_json(draft)
            validate_targets(
                raw, registry=self.registry, commands=self.commands, company_id=self.company_id
            )
        except KernelError:
            raise
        except (ValueError, TypeError, RecursionError, OverflowError, UnicodeError) as exc:
            raise KernelError(
                "work_draft_invalid", "工作稿必须符合正文结构并仅包含合法、可保存的JSON"
            ) from exc
        saved = False
        try:
            with self._locked(period, work_area, create=True) as path:
                previous = self._read(path, period, work_area)
                self._revision(previous, expected_revision)
                preserve_pending_requests(
                    previous["draft"] if previous else None,
                    raw,
                    request_result=self.request_result,
                    company_id=self.company_id,
                    database_id=self.database_id,
                )
                revision = str(uuid.uuid4())
                envelope = {
                    "format": WORK_DRAFT_FORMAT,
                    "identity": self.identity,
                    "period": period,
                    "work_area": work_area,
                    "revision": revision,
                    "draft": raw,
                    "draft_digest": digest(raw).hex(),
                }
                payload = canonical(envelope).encode("utf-8")
                if len(payload) > MAX_WORK_DRAFT_BYTES:
                    raise KernelError(
                        "work_draft_too_large",
                        "完整工作稿文件超过8MiB；旧稿已保留",
                        max_bytes=MAX_WORK_DRAFT_BYTES,
                    )
                self._write(path, payload)
                saved = True
                return {
                    **self._response(period, work_area),
                    "status": "saved",
                    "revision": revision,
                }
        except OSError as exc:
            if saved:
                raise KernelError(
                    "work_draft_result_unconfirmed",
                    "工作稿已替换但返回结果未确认，请重读版本",
                    action="read_work_draft",
                ) from exc
            raise KernelError(
                "work_draft_save_failed", "工作稿保存失败；替换前的旧稿已保留"
            ) from exc
        except KernelError:
            raise
        except (ValueError, TypeError, RecursionError, OverflowError, UnicodeError) as exc:
            raise KernelError("work_draft_invalid", "工作稿不能编码为合法UTF-8 JSON") from exc

    def _write(self, path: Path, payload: bytes):
        temporary = None
        replaced = False
        try:
            temporary = create_private_file(self.directory / f".write-{uuid.uuid4().hex}.tmp")
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            replaced = True
            with assert_private_file(path).open("r+b") as handle:
                os.fsync(handle.fileno())
            self._sync_directory()
        except OSError as exc:
            if replaced:
                raise KernelError(
                    "work_draft_result_unconfirmed",
                    "工作稿已替换但持久保存结果未确认，请重读版本",
                    action="read_work_draft",
                ) from exc
            raise
        finally:
            if temporary is not None and temporary.exists():
                try:
                    assert_private_file(temporary).unlink()
                except OSError:
                    # A failed cleanup must never obscure the save outcome.
                    pass

    def _sync_directory(self):
        self._sync_path(self.directory)

    @staticmethod
    def _sync_path(path):
        if os.name != "nt":
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def delete(self, period: YearMonth, work_area: WorkArea, expected_revision: str):
        period, work_area = self._scope(period, work_area)
        deleted = False
        try:
            with self._locked(period, work_area) as path:
                previous = self._read(path, period, work_area)
                if previous is None:
                    raise KernelError("work_draft_absent", "指定事项没有已保存工作稿")
                self._revision(previous, expected_revision)
                assert_private_file(path).unlink()
                deleted = True
                self._sync_directory()
                return {
                    **self._response(period, work_area),
                    "status": "deleted",
                    "revision": previous["revision"],
                }
        except OSError as exc:
            if deleted:
                raise KernelError(
                    "work_draft_result_unconfirmed",
                    "工作稿已删除但持久保存结果未确认，请重读版本",
                    action="read_work_draft",
                ) from exc
            raise KernelError("work_draft_delete_failed", "工作稿删除失败；原文件已保留") from exc
