"""Select one actual business or an authenticated, already prepared member scope."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.ledger.public import AdoptedContent

from . import _brief_vouchers, _monthly
from ._brief_activity_parts import parts
from ._brief_activity_sources import Contents, pages
from ._collections import CollectionFilter, collection_page


@dataclass(frozen=True)
class SelectedBusiness:
    business: dict
    kind: str
    content: AdoptedContent | None
    current: AdoptedContent | None
    range: dict
    detail: dict | None
    cache: Contents


def _item(scope, component, collection, item_id, read):
    try:
        result = collection_page(
            _monthly.key(scope),
            component,
            collection,
            CollectionFilter(item_id=item_id),
            PageQuery(1),
            read,
        )
    except ContractError as error:
        if error.code in {"read_model_not_ready", "storage_corrupt"}:
            raise ContractError(
                "snapshot_changed", "原业务范围尚未就绪或已经更新，请刷新原页面。"
            ) from error
        raise
    if len(result.items) != 1:
        raise ContractError("snapshot_changed", "原业务分项已经更新，请刷新原页面。")
    return result.items[0]


def _member(reader, scope, query, read):
    key, voucher = query["detail_scope_key"], query["voucher_version_id"]
    subject = query["subject_id"]
    if key.startswith("open:"):
        item = _item(scope, "brief_open_items", "open_members", key[5:], read)
        selected = item["_detail_scope"]
        header = _brief_vouchers.effective_headers(reader, [selected["reference"]], read)[0]
        expected = "" if header is None else _brief_vouchers.token(_brief_vouchers.locator(header))
        amount = item["source_amount_fen"]
        title = "原应收" if selected["direction"] == "receivable" else "原应付"
        category = item["category_key"]
    elif key.startswith("voucher:"):
        item = _item(scope, "brief_activity", "brief_vouchers", key[8:], read)
        template = item["template"]
        if template["subject_id"] != subject or key[8:] != template["voucher_version_id"]:
            raise ContractError("snapshot_changed", "指定凭证不属于原业务范围，请刷新原页面。")
        expected = template["voucher_version_id"]
        selected = {"reference": item["reference"], "voucher": expected}
        amount, title, category = (
            template["business_amount_fen"],
            template["business_amount_label"],
            template["group"],
        )
    else:
        item = _item(scope, "brief_activity", "activity_members", key, read)
        row, selected = item["row"], item["scope"]
        if row["subject_id"] != subject:
            raise ContractError("snapshot_changed", "指定业务分项不属于原业务，请刷新原页面。")
        expected = row["voucher_version_id"]
        amount, title, category = row["amount_fen"], row["amount_label"], row["group"]
    if selected["reference"]["business_id"] != subject or expected != voucher:
        raise ContractError("snapshot_changed", "业务、分项与精确凭证不一致，请刷新原页面。")
    return selected, {
        "key": key,
        "voucher_version_id": voucher,
        "category": category,
        "amount_fen": amount,
        "amount_label": title,
    }


def _whole(content, cache):
    if content is None or content.target["kind"] == "system.withdrawal":
        return {}
    prepared = parts(content, cache)
    return {
        "reference": content.reference,
        "source": content.source,
        "allocations": [entry for part in prepared for entry in part.scope["allocations"]],
        "obligations": [],
    }


def select(reader, scope, query, read):
    subject = query["subject_id"]
    result = reader.businesses.get_businesses(
        {"company_id": read.company_id, "business_ids": [subject], "include": ["facts"]}, read
    )
    if (result["company_id"], result["database_id"]) != (read.company_id, read.database_id):
        raise ContractError("storage_corrupt", "业务身份不属于本次公司读取范围。")
    if not result["businesses"]:
        raise ContractError("not_found", "当前公司没有这笔业务。")
    business = result["businesses"][0]
    if business["state"]["business_id"] != subject:
        raise ContractError("storage_corrupt", "业务读取没有返回所指定的身份。")
    cache = Contents(reader.ledger, read)
    current_ref = next(
        (ref for ref in business["state"]["results"] if ref["result_slot"] == "main"), None
    )
    if current_ref is not None:
        cache.load([current_ref])
    current = cache.get(current_ref) if current_ref is not None else None
    detail = None
    if "detail_scope_key" in query:
        selected, detail = _member(reader, scope, query, read)
        cache.load([selected["reference"]])
        content = cache.get(selected["reference"])
        if query["detail_scope_key"].startswith("voucher:"):
            selected = _whole(content, cache)
        kind = content.target["kind"]
    else:
        selected_scope = dict(scope) | {"period_from": read.start_period}
        directory = list(
            pages(
                reader.ledger.query_adopted_directory,
                selected_scope,
                read,
                business_ids=(subject,),
                result_slots=("main",),
            )
        )
        if len(directory) > 1:
            raise ContractError("storage_corrupt", "同一业务的月末主结果不唯一。")
        reference = directory[0].reference if directory else None
        if reference is not None:
            cache.load([reference])
        content = cache.get(reference) if reference is not None else None
        kind = directory[0].business_kind if directory else business.get("fact", {}).get("kind")
        if kind is None:
            raise ContractError("not_found", "所选月份没有这笔业务的可查看正式资料。")
        selected = _whole(content, cache)
    return SelectedBusiness(business, kind, content, current, selected, detail, cache)
