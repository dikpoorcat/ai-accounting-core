"""Read queries bind pages to the same company snapshot and scope."""

import base64
from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)


@dataclass(frozen=True)
class MoneyQuery:
    account_id: str
    period_from: str
    period_to: str
    limit: int = 500
    cursor: str | None = None


@dataclass(frozen=True)
class MoneyPage:
    items: tuple[dict, ...]
    total: int
    head: int
    next_cursor: str | None


def validate_query(query):
    if not isinstance(query, MoneyQuery):
        raise TypeError("query must be MoneyQuery")
    validate_contract("core.schema.json", "Id", query.account_id)
    validate_contract("core.schema.json", "Month", query.period_from)
    validate_contract("core.schema.json", "Month", query.period_to)
    if (
        query.period_from > query.period_to
        or type(query.limit) is not int
        or not 1 <= query.limit <= 500
    ):
        raise ContractError("invalid_input", "资金查询月份或每页数量无效。")


def scope(query, read, collection):
    validate_query(query)
    return content_digest(
        {
            "company": read.company_id,
            "database": read.database_id,
            "head": read.head,
            "view": read.view,
            "as_of": read.as_of,
            "account": query.account_id,
            "from": query.period_from,
            "to": query.period_to,
            "collection": collection,
        }
    )


def position(query, read, collection):
    signature = scope(query, read, collection)
    if not query.cursor:
        return None, signature
    try:
        value = loads_json(base64.urlsafe_b64decode(query.cursor.encode("ascii")).decode("utf-8"))
        if set(value) != {"scope", "position"} or not isinstance(value["position"], list):
            raise ValueError
    except (TypeError, ValueError, UnicodeError, ContractError) as error:
        raise ContractError("invalid_input", "资金分页标识无效。") from error
    if value["scope"] != signature:
        raise ContractError("snapshot_changed", "资金查询范围或读取版本已变化，请从第一页读取。")
    return value["position"], signature


def cursor(signature, value):
    return base64.urlsafe_b64encode(
        canonical_json({"scope": signature, "position": value}).encode("utf-8")
    ).decode("ascii")


def page_movements(query, read, definitions):
    after, signature = position(query, read, "movements")
    if after is not None and (len(after) != 3 or not all(type(x) is str for x in after)):
        raise ContractError("invalid_input", "资金分页位置无效。")
    ordered = sorted(
        definitions,
        key=lambda item: (
            item.value["actual_on"],
            item.reference["business_id"],
            item.reference["slot"],
        ),
    )
    values = [
        item
        for item in ordered
        if after is None
        or [item.value["actual_on"], item.reference["business_id"], item.reference["slot"]] > after
    ]
    has_more = len(values) > query.limit
    selected = values[: query.limit]
    return MoneyPage(
        tuple(
            {"reference": item.reference, "source": item.source, "movement": item.value}
            for item in selected
        ),
        len(ordered),
        read.head,
        cursor(
            signature,
            [
                selected[-1].value["actual_on"],
                selected[-1].reference["business_id"],
                selected[-1].reference["slot"],
            ],
        )
        if has_more
        else None,
    )
