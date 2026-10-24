"""A workbook plan contains only the exact scope, payable candidates and bank presentation."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    parse_fen,
    sum_fen,
    validate_contract,
)


def head(value):
    return content_digest(
        {
            key: value.get(key)
            for key in (
                "company_id",
                "database_id",
                "scope",
                "template",
                "candidates",
                "rows",
                "total_fen",
            )
        }
    )


def scope_key(scope):
    return (
        "payments:"
        + scope["period"]
        + ":"
        + ("period" if scope["selection"] == "period" else content_digest(scope["business_ids"]))
    )


def validate(plan):
    validate_contract("payment-export.schema.json", "PaymentExportPlan", plan)
    if plan["scope_head"] != head(plan):
        raise ContractError("payment_export_invalid", "代发计划摘要与完整内容不一致。")
    grouped = defaultdict(lambda: {"amount": 0, "refs": []})
    seen = set()
    for candidate in plan["candidates"]:
        for reference in candidate["obligation_refs"]:
            key = (reference["business_id"], reference["slot"])
            if reference["business_id"] != candidate["business_id"] or key in seen:
                raise ContractError(
                    "payment_export_invalid", "代发计划不能重复或错配同一付款义务。"
                )
            seen.add(key)
        amount = parse_fen(candidate["payable_fen"])
        if amount:
            if "party_id" not in candidate:
                raise ContractError("payment_export_invalid", "代发计划缺少实际收款对象。")
            group = grouped[(candidate["party_id"], candidate["purpose"])]
            group["amount"] = sum_fen((group["amount"], amount))
            group["refs"].extend(candidate["obligation_refs"])
    rows = {}
    for row in plan["rows"]:
        key = (row["party_id"], row["purpose"])
        if key in rows:
            raise ContractError("payment_export_invalid", "同一对象、同一用途须合并成一行。")
        rows[key] = row
    if set(rows) != set(grouped) or not rows:
        raise ContractError("payment_export_invalid", "代发行未完整覆盖全部正数可付业务。")
    for key, required in grouped.items():
        if parse_fen(rows[key]["amount_fen"]) != required["amount"] or (
            rows[key]["obligation_refs"] != required["refs"]
        ):
            raise ContractError("payment_export_invalid", "代发行金额和来源须完整对应正式可付额。")
    if sum_fen(parse_fen(row["amount_fen"]) for row in plan["rows"]) != parse_fen(
        plan["total_fen"]
    ):
        raise ContractError("payment_export_invalid", "代发表总额与各行金额不一致。")
    return plan
