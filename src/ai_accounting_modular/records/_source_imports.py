"""Prepare one complete source batch; the existing records plan owns atomic registration."""

from ai_accounting_modular.contracts.public import ContractError, UnitOfWork, validate_contract

from . import _sources


def prepare(records, imports, read):
    if isinstance(read, UnitOfWork):
        raise ContractError("invalid_operation", "原件须在写事务前完整读取与校验。")
    if not isinstance(imports, (list, tuple)) or not 1 <= len(imports) <= 500:
        raise ContractError("invalid_input", "一次导入须包含1至500份来源。")
    for item in imports:
        validate_contract("core.schema.json", "SourceImport", item)
    repository, root = read.repository("records"), records._root(read)
    by_content, selected = {}, {}
    for item in imports:
        existing = (
            repository.sources((item["source_id"],)).get(item["source_id"])
            if "source_id" in item
            else None
        )

        def find(kind, digest, *, current=item):
            previous = by_content.get((kind, digest))
            if previous is not None:
                if "archive_month" in current and current["archive_month"] != previous.get(
                    "archive_month"
                ):
                    raise ContractError("conflict", "同一来源在一单中不能同时归入不同月份。")
                return previous
            return repository.find_source_content(kind, digest)

        source = _sources.prepare_source(item, root, existing, find, company_id=read.company_id)
        previous = selected.get(source["source_id"])
        if previous is not None and previous != source:
            raise ContractError("conflict", "同一来源身份在本单中给出了不同内容或归档位置。")
        selected[source["source_id"]] = source
        by_content.setdefault((source["kind"], source["sha256"]), source)
    return tuple(selected.values())
