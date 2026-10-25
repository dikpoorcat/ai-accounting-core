"""Company selection and bounded current searches do not load foreign fact tables."""

import base64
import binascii
from collections import defaultdict
from contextlib import nullcontext

from ai_accounting_modular.contracts.public import (
    CONTRACT,
    ContractError,
    YearMonth,
    canonical_json,
    content_digest,
    fact_source_ids,
    loads_json,
    validate_contract,
)

from . import _notes


def months(first, last):
    if first > last:
        return []
    start, end = YearMonth(first).ordinal, YearMonth(last).ordinal
    return [str(YearMonth.from_ordinal(value)) for value in range(start, end + 1)]


def _read(application, company, read_context):
    if read_context is None:
        return application.storage.read_unit(company)
    if read_context.company_id != company or read_context.view != "current":
        raise ContractError(
            "invalid_input",
            "业务读取须使用同一公司的当前快照；历史正式结果须明确指定结果身份和代次。",
        )
    return nullcontext(read_context)


def context(application, request, read_context=None):
    validate_contract("core.schema.json", "ContextRequest", request)
    company = request.get("company_id")
    if company is None:
        return validate_contract(
            "core.schema.json",
            "ContextResult",
            {
                "contract": CONTRACT,
                "state": "no_company",
                "available_companies": [
                    {
                        "company_id": item.company_id,
                        "credit_code": item.credit_code,
                        "legal_name": item.legal_name,
                    }
                    for item in application.storage.list_companies()
                ],
            },
        )
    with _read(application, company, read_context) as read:
        metadata = application.storage.company_metadata(read)
        scope = application.work_scope(read)
        closed = scope.closed_through
        following = (
            YearMonth(closed).ordinal + 1 if closed else YearMonth(read.start_period).ordinal
        )
        first_open = str(YearMonth.from_ordinal(following)) if following < 9999 * 12 else None
        working = scope.working_period_to
        defaults = ["company", *(["period:" + first_open] if first_open else [])]
        note_scopes = request.get("note_scopes", defaults)
        issue_scopes = list(dict.fromkeys(request.get("issue_scopes", defaults)))
        heads = _notes.basis_heads(application, note_scopes, read)
        notes = application.records.current_notes(tuple(dict.fromkeys(note_scopes)), read)
        issues = [application.records.current_issues(scope, read) for scope in issue_scopes]
        status = application.records.document_status(read)
        result = {
            "contract": CONTRACT,
            "state": "selected",
            "company_id": company,
            "database_id": read.database_id,
            "company_identity": {
                "credit_code": metadata.credit_code,
                "legal_name": metadata.legal_name,
                "start_period": metadata.start_period,
                "working_period_to": working,
                "accounting_standard": "small_enterprise",
                "currency": "CNY",
            },
            "open_periods": months(first_open, max(first_open, working)) if first_open else [],
            "closed_periods": months(read.start_period, closed) if closed else [],
            "notes": list(notes),
            "issues": [item for scope in issues for item in scope["issues"]],
            "issue_scopes": issues,
            "supported_kinds": list(application.registry.supported_kinds),
            "work_defaults": {
                "payroll": {
                    "accrual_period": "service_month",
                    "planned_payment_period": "following_month",
                },
                "service_revenue": {
                    "recognition_period": "business_month",
                    "planned_receipt_period": "following_month",
                    "receipt_action": "settle_existing_receivable",
                    "vat_period_basis": "income_recognition_month",
                    "vat_preference": "retain_applicable_reliefs",
                },
            },
            "document_state": status["state"],
            "document_set_id": status["set_id"],
            "note_basis_heads": [{"scope": scope, "head": head} for scope, head in heads.items()],
        }
        return validate_contract("core.schema.json", "ContextResult", result)


def find(application, query, read_context=None):
    validate_contract("core.schema.json", "BusinessQuery", query)
    if query["period_from"] > query["period_to"]:
        raise ContractError("invalid_input", "检索起月不能晚于末月。")
    if query.get("kind"):
        if query["kind"].split(".", 1)[0] != query["family"]:
            raise ContractError("invalid_input", "业务种类必须属于指定业务家族。")
        application.registry.owner(query["kind"])
    if query.get("object_id") and query.get("status") == "withdrawn":
        raise ContractError("invalid_input", "撤去身份不保留旧对象关系，请按身份、种类或月份检索。")
    with _read(application, query["company_id"], read_context) as read:
        repo = read.repository("application")
        total, filtered, revision = repo.search_summary(query)
        version = content_digest(
            {
                "company": read.company_id,
                "database": read.database_id,
                "scope": {
                    **{key: query[key] for key in ("family", "period_from", "period_to")},
                    "status": query.get("status", "active"),
                    "kind": query.get("kind"),
                    "object_id": query.get("object_id"),
                },
                "total": total,
                "filtered": filtered,
                "revision": revision,
            }
        )
        after = _position(query.get("cursor"), version)
        rows = repo.search_page(query, after=after)
        selected = rows[: query["limit"]]
        has_more = len(rows) > len(selected)
        return validate_contract(
            "core.schema.json",
            "BusinessFindResult",
            {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "head": read.head,
                "items": [
                    {
                        **{key: row[key] for key in ("business_id", "kind", "seq", "state")},
                        "period": row["search_period"],
                        "label": f"{row['search_period']} {row['kind']}",
                    }
                    for row in selected
                ],
                "page": {
                    "total_count": total,
                    "filtered_count": filtered,
                    "returned_count": len(selected),
                    "has_more": has_more,
                    "next_cursor": _cursor(version, selected[-1]["business_id"])
                    if has_more
                    else None,
                    "collection_version": version,
                },
            },
        )


def _cursor(version, after):
    return base64.urlsafe_b64encode(
        canonical_json({"version": version, "after": after}).encode()
    ).decode()


def _position(token, version):
    if token is None:
        return None
    try:
        value = loads_json(base64.b64decode(token, altchars=b"-_", validate=True))
        if not isinstance(value, dict) or set(value) != {"version", "after"}:
            raise ValueError("invalid cursor")
        validate_contract("core.schema.json", "Digest", value["version"])
        validate_contract("core.schema.json", "Id", value["after"])
    except (ContractError, ValueError, TypeError, binascii.Error) as error:
        raise ContractError("invalid_input", "业务分页游标无效，请重新检索。") from error
    if value["version"] != version:
        raise ContractError("snapshot_changed", "检索范围内的业务已变化，请从第一页重新读取。")
    return value["after"]


def get(application, query, read_context=None):
    validate_contract("core.schema.json", "BusinessGet", query)
    with _read(application, query["company_id"], read_context) as read:
        heads = read.repository("application").heads(query["business_ids"])
        exact = "result_id" in query
        if exact:
            view = application.ledger.get_result(query["result_id"], query["generation"], read)
            business_id = view["reference"]["business_id"]
            if business_id not in query["business_ids"]:
                raise ContractError("invalid_input", "精确正式结果不属于本次指定业务身份。")
            if business_id not in heads:
                raise ContractError("storage_corrupt", "正式结果缺少所属业务身份。")
            views, selected, facts = (view,), [heads[business_id]], {}
        else:
            selected = [heads[key] for key in dict.fromkeys(query["business_ids"]) if key in heads]
            views = (
                application.ledger.get_results(list(heads), read)
                if {"results", "sources"} & set(query["include"])
                else ()
            )
            facts = {
                fact["business_id"]: fact
                for fact in application.registry.export_heads(selected, read)
            }
        grouped, required = defaultdict(list), defaultdict(set)
        for view in views:
            key = view["reference"]["business_id"]
            grouped[key].append(view)
            required[key].update(item["source_id"] for item in view["basis"]["source_locations"])
        if not exact:
            for key, fact in facts.items():
                required[key].update(fact_source_ids(fact))
        sources = {}
        if "sources" in query["include"]:
            ids = sorted({source for values in required.values() for source in values})
            for offset in range(0, len(ids), 500):
                sources.update(
                    (source["source_id"], source)
                    for source in application.records.resolve_sources(
                        ids[offset : offset + 500], read
                    )
                )
            if set(sources) != set(ids):
                raise ContractError(
                    "storage_corrupt", "正式读取引用的必要来源缺失，不能返回不完整资料。"
                )
        businesses = []
        references = (
            tuple(view["reference"] for view in views)
            if exact or {"results", "sources"} & set(query["include"])
            else None
        )
        for state in application._states(selected, read, references=references):
            key = state["business_id"]
            item = {
                "state": state,
                "results": grouped[key] if "results" in query["include"] else [],
            }
            if key in facts and "facts" in query["include"]:
                item["fact"] = facts[key]
            if "sources" in query["include"]:
                item["sources"] = [sources[source] for source in sorted(required[key])]
            businesses.append(item)
        return validate_contract(
            "core.schema.json",
            "BusinessGetResult",
            {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "head": read.head,
                "businesses": businesses,
                "missing_business_ids": [
                    key for key in dict.fromkeys(query["business_ids"]) if key not in heads
                ],
            },
        )
