"""Explicit source retirement; tiny current markers bridge SQLite and original files."""

from __future__ import annotations

import heapq
from pathlib import PurePosixPath
from uuid import uuid4

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    ContractError,
    UnitOfWork,
    canonical_json,
    loads_json,
    validate_contract,
)

from . import _changes, _sources
from ._file_identity import identity
from ._types import SourceDiscardCleanup, SourceDiscardError, SourceDiscards

_DIRECTORY = "runtime/source-discards"
_FORMAT = "ai-accounting-source-discard/1"
_FIELDS = {
    "format",
    "company_id",
    "database_id",
    "marker_id",
    "source_id",
    "relative_path",
    "sha256",
    "byte_size",
    "deletion_head",
}


def _writer(uow):
    if not isinstance(uow, UnitOfWork) or uow.view != "current" or uow.as_of is not None:
        raise ContractError("invalid_operation", "来源退出和清理必须在当前公司的写事务中办理。")


def _protected(paths, root, publication):
    if not isinstance(paths, (list, tuple)):
        raise ContractError("invalid_input", "受保护原件必须使用公司根内的相对路径集合。")
    for path in paths:
        _sources.controlled_path(root, path)
    return set(paths) | set(publication.leases)


def _marker_path(root, marker_id):
    _marker_source(marker_id)
    return _sources.controlled_path(root, _DIRECTORY + "/" + marker_id + ".json")


def _marker_source(marker_id):
    parts = marker_id.split("--") if isinstance(marker_id, str) else ()
    if len(parts) != 2:
        raise ContractError("invalid_input", "待清理原件标识无效。")
    source_id, unique_id = parts
    validate_contract("core.schema.json", "Id", source_id)
    validate_contract("core.schema.json", "Id", unique_id)
    return source_id


def _marker_ids(root):
    directory = _sources.controlled_path(root, _DIRECTORY)
    if not directory.exists():
        return
    for path in directory.iterdir():
        if path.suffix != ".json":
            continue
        try:
            _marker_source(path.stem)
        except ContractError:
            continue  # Unknown files are never cleanup work.
        yield path.stem


def has_pending(root):
    return next(_marker_ids(root), None) is not None


def pending_for_period(root, period, read):
    validate_contract("core.schema.json", "Month", period)
    selected = []
    for marker_id in _marker_ids(root):
        try:
            value = _read_marker(root, marker_id, read)
        except FileNotFoundError:
            continue
        if PurePosixPath(value["relative_path"]).parts[1] == period:
            selected.append(marker_id)
    return tuple(sorted(selected))


def _read_marker(root, marker_id, read):
    path = _marker_path(root, marker_id)
    with path.open("rb") as stream:
        raw = stream.read(8193)
    try:
        value = loads_json(raw)
        if len(raw) > 8192 or not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError("marker_fields")
        if (
            value["format"] != _FORMAT
            or value["company_id"] != read.company_id
            or value["database_id"] != read.database_id
            or value["marker_id"] != marker_id
            or value["source_id"] != _marker_source(marker_id)
        ):
            raise ValueError("marker_identity")
        validate_contract("core.schema.json", "Digest", value["sha256"])
        if type(value["byte_size"]) is not int or value["byte_size"] < 0:
            raise ValueError("marker_size")
        if type(value["deletion_head"]) is not int or not 0 < value["deletion_head"] <= MAX_FEN:
            raise ValueError("marker_head")
        relative = value["relative_path"]
        _sources.controlled_path(root, relative)
        parts = PurePosixPath(relative).parts
        if len(parts) != 4 or parts[0] != "originals" or parts[2] != value["source_id"]:
            raise ValueError("marker_original_path")
        if parts[1] != "unassigned":
            validate_contract("core.schema.json", "Month", parts[1])
        _sources._file_name(parts[3])
    except (ValueError, TypeError) as exc:
        raise ContractError(
            "source_cleanup_invalid", "待清理原件标识与公司、数据库或原件位置不一致。"
        ) from exc
    return value


def _selection(source_ids, read, root, publication, *, retained_source_ids, protected_paths):
    wanted = _changes.source_ids(source_ids)
    retained = _changes.source_ids(retained_source_ids)
    protected = _protected(protected_paths, root, publication)
    repository = read.repository("records")
    stored = repository.sources(wanted)
    referenced = set(repository.referenced_source_ids(wanted)) | set(retained)
    protected_ids = set()
    for source_id, source in stored.items():
        _sources.validate_source(source)
        if source_id in referenced or source.get("relative_path") in protected:
            protected_ids.add(source_id)
    return wanted, stored, protected_ids


def protection(source_ids, read, root, publication, *, retained_source_ids, protected_paths):
    _, _, protected_ids = _selection(
        source_ids,
        read,
        root,
        publication,
        retained_source_ids=retained_source_ids,
        protected_paths=protected_paths,
    )
    return tuple(sorted(protected_ids))


def discard(
    source_ids,
    uow,
    root,
    publication,
    *,
    retained_source_ids,
    protected_paths,
    retain_protected=False,
):
    _writer(uow)
    if uow.next_head <= uow.head:
        raise ContractError("invalid_operation", "来源登记退出必须使用正式写事务。")
    if type(retain_protected) is not bool:
        raise ContractError("invalid_input", "保留受保护来源须使用明确布尔值。")
    wanted, stored, protected_ids = _selection(
        source_ids,
        uow,
        root,
        publication,
        retained_source_ids=retained_source_ids,
        protected_paths=protected_paths,
    )
    if protected_ids and not retain_protected:
        raise ContractError(
            "source_in_use",
            f"来源{min(protected_ids)}仍被当前工作、正式账本或原件任务使用，不能退出。",
        )
    selected = {key: value for key, value in stored.items() if key not in protected_ids}
    eligible_ids = set(wanted) - protected_ids
    markers = {
        marker_id for marker_id in _marker_ids(root) if _marker_source(marker_id) in eligible_ids
    }
    # Complete every durable marker before the single atomic DELETE statement.
    for source_id in sorted(selected):
        source = selected[source_id]
        if source["kind"] != "file":
            continue
        marker_id = source_id + "--" + str(uuid4())
        value = {
            "format": _FORMAT,
            "company_id": uow.company_id,
            "database_id": uow.database_id,
            "marker_id": marker_id,
            "source_id": source_id,
            "relative_path": source["relative_path"],
            "sha256": source["sha256"],
            "byte_size": source["byte_size"],
            "deletion_head": uow.next_head,
        }
        _sources.atomic_write(
            root,
            _DIRECTORY + "/" + marker_id + ".json",
            (canonical_json(value) + "\n").encode("utf-8"),
            replace=False,
        )
        if _read_marker(root, marker_id, uow) != value:
            raise ContractError("source_cleanup_invalid", "待清理原件标识已存在且内容不一致。")
        markers.add(marker_id)
    if selected:
        uow.repository("records").delete_sources(tuple(selected))
    return SourceDiscards(
        tuple(sorted(selected)),
        tuple(sorted(set(wanted) - stored.keys())),
        tuple(sorted(markers)),
        tuple(sorted(protected_ids)),
    )


def _selected(root, marker_ids, limit, after):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ContractError("invalid_input", "每次清理原件须选择1至500个当前标识。")
    if after is not None:
        _marker_source(after)
    if marker_ids is not None:
        if after is not None:
            raise ContractError("invalid_input", "明确选择清理标识时不同时使用目录续页。")
        if not isinstance(marker_ids, (list, tuple)) or len(marker_ids) > limit:
            raise ContractError("invalid_input", "明确选择的清理标识不能超过本次上限。")
        for marker_id in marker_ids:
            _marker_source(marker_id)
        return tuple(sorted(set(marker_ids))), None
    selected = heapq.nsmallest(
        limit + 1, (key for key in _marker_ids(root) if after is None or key > after)
    )
    return tuple(selected[:limit]), selected[limit - 1] if len(selected) > limit else None


def _unlink_original(root, value):
    relative = value["relative_path"]
    path = _sources.controlled_path(root, relative)
    try:
        before = identity(path)
    except FileNotFoundError:
        return
    _sources._check_file(root, value)
    _sources.controlled_path(root, relative)
    if identity(path) != before:
        raise ContractError("source_changed", "待清理原件在核对期间改变，暂时保留。")
    path.unlink()


def finish(
    uow, root, publication, *, protected_paths, marker_ids=None, limit=500, after_marker_id=None
):
    _writer(uow)
    protected = _protected(protected_paths, root, publication)
    selected, next_id = _selected(root, marker_ids, limit, after_marker_id)
    repository = uow.repository("records")
    completed, pending, source_ids, errors = [], [], set(), []
    for marker_id in selected:
        source_id = _marker_source(marker_id)
        try:
            try:
                value = _read_marker(root, marker_id, uow)
            except FileNotFoundError:
                completed.append(marker_id)
                continue
            # A rollback or reimport preserves the original, even at an earlier head.
            if repository.sources((value["source_id"],)):
                _marker_path(root, marker_id).unlink(missing_ok=True)
            elif uow.head < value["deletion_head"] or value["relative_path"] in protected:
                pending.append(marker_id)
                source_ids.add(value["source_id"])
                continue
            else:
                _unlink_original(root, value)
                _marker_path(root, marker_id).unlink(missing_ok=True)
            completed.append(marker_id)
        except (ContractError, OSError) as exc:
            pending.append(marker_id)
            source_ids.add(source_id)
            errors.append(
                SourceDiscardError(
                    marker_id,
                    exc.code if isinstance(exc, ContractError) else "source_cleanup_unavailable",
                    exc.message
                    if isinstance(exc, ContractError)
                    else "暂时无法清理这份原件，已保留待清理标识。",
                )
            )
    return SourceDiscardCleanup(
        tuple(completed), tuple(pending), tuple(sorted(source_ids)), tuple(errors), next_id
    )
