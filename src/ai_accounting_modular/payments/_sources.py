"""Resolve payment classifications to owner-defined obligation keys, including opening members."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import BusinessRef, ContractError, PageQuery
from ai_accounting_modular.ledger.opening.public import (
    OpeningOrdinaryReference,
    opening_obligation_references,
    opening_payroll_references,
)

SLOTS = {
    "operations.expense": ("primary", "费用报销"),
    "operations.reimbursement": ("reimbursement", "代垫款归还"),
    "operations.pass_through": ("remittance", "代收款转付"),
    "operations.pass_through_return": ("refund", "代收款退回"),
}


def batches(values):
    values = list(values)
    for offset in range(0, len(values), 500):
        yield values[offset : offset + 500]


@dataclass(frozen=True)
class PaymentSource:
    reference: BusinessRef
    kind: str
    purpose: str
    recipient_id: str | None = None


def opening_sources(ledger, identities, read, balance_period):
    scope = {
        "company_id": read.company_id,
        "period_from": read.start_period,
        "period_to": balance_period,
        "view": "current",
    }
    page = PageQuery(500)
    seen = set()
    while True:
        selected = ledger.query_adopted_targets(
            scope,
            ("opening.package",),
            page,
            read,
            business_ids=identities,
            result_slots=("main",),
        )
        contents = ledger.get_adopted_contents([item.reference for item in selected.items], read)
        for content in contents:
            seen.add(content.reference["business_id"])
            for item in opening_payroll_references(content):
                if item.component == "net":
                    yield PaymentSource(
                        item.source,
                        "opening.package",
                        f"{item.payroll_period} 工资",
                        item.recipient_id,
                    )
            for item in opening_obligation_references(content):
                if isinstance(item, OpeningOrdinaryReference) and item.nature in {
                    "employee_reimbursement",
                    "owner_reimbursement",
                }:
                    yield PaymentSource(item.source, "opening.package", "期初费用报销")
        if selected.next_cursor is None:
            if seen != set(identities):
                raise ContractError(
                    "storage_corrupt", "当前期初包缺少对应的正式主结果，不能当作没有待付业务。"
                )
            return
        page = PageQuery(500, selected.next_cursor, selected.version)


def ordinary_sources(owner, facts, read, balance_period):
    result = []
    opening_ids = []
    for fact in facts:
        if fact["kind"] == "opening.package":
            opening_ids.append(fact["business_id"])
        elif fact["kind"] in SLOTS:
            slot, label = SLOTS[fact["kind"]]
            result.append(
                PaymentSource(
                    {"business_id": fact["business_id"], "slot": slot},
                    fact["kind"],
                    f"{fact['period']} {label}",
                )
            )
    for batch in batches(opening_ids):
        result.extend(opening_sources(owner.ledger, batch, read, balance_period))
    return sorted(result, key=lambda item: (item.reference["business_id"], item.reference["slot"]))
