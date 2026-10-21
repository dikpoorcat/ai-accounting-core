"""Hash outside the writer, then check the same registered input without rereading it."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
)

from . import _sources
from ._file_identity import identity
from ._types import PreparedSources


def values(sources):
    result = tuple(loads_json(canonical_json(source)) for source in sources)
    seen = set()
    for source in result:
        _sources.validate_source(source)
        if source["source_id"] in seen:
            raise ContractError("invalid_input", "同一来源身份不能在一单中重复出现。")
        seen.add(source["source_id"])
    return tuple(sorted(result, key=lambda source: source["source_id"]))


def _identity(root, relative):
    try:
        result = identity(_sources.controlled_path(root, relative))
        _sources.controlled_path(root, relative)
    except OSError as error:
        raise ContractError("source_unavailable", "预核验的来源位置已缺失或无法读取。") from error
    return result


def prepare(sources, read, root):
    identities = []
    for source in sources:
        relative = source.get("relative_path")
        before = _identity(root, relative) if relative is not None else None
        _sources.check_source(root, source)
        if relative is not None:
            if _identity(root, relative) != before:
                raise ContractError("source_changed", "来源在预核验过程中改变，请重新准备。")
            identities.append((relative, before))
    result = PreparedSources(
        read.company_id, read.database_id, root, canonical_json(list(sources)), tuple(identities)
    )
    object.__setattr__(result, "_active", True)
    return result


def check(prepared, sources, read, root):
    if not isinstance(prepared, PreparedSources) or not prepared._active:
        raise ContractError("invalid_operation", "来源预核验已经结束，请重新准备资料。")
    if (prepared.company_id, prepared.database_id, prepared.root) != (
        read.company_id,
        read.database_id,
        root,
    ):
        raise ContractError("conflict", "来源预核验不属于当前公司及资料位置。")
    try:
        supplied = canonical_json(sorted(sources, key=lambda source: source["source_id"]))
    except (KeyError, TypeError) as error:
        raise ContractError("invalid_input", "来源提交须使用完整规范来源。") from error
    if supplied != prepared._sources_json:
        raise ContractError("conflict", "提交的来源集合与预核验内容不一致。")
    for relative, expected in prepared._file_identities:
        if _identity(root, relative) != expected:
            raise ContractError("source_changed", "原件在预核验后改变，请重新准备资料。")
