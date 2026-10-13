"""A receipt has one real source identity and a proven allocation to each business."""

import json

import pytest
from test_tax_point_basis import (
    EVIDENCE,
    candidate,
    context,
    make_basis,
    original,
    payment,
    source_ref,
)

from ai_accounting.kernel.contracts import FactVersion, NeedsInformation
from ai_accounting.kernel.domains.banking import BankStatement
from ai_accounting.kernel.domains.transactions import Payment
from ai_accounting.kernel.tax_point_basis import resolve_tax_point
from ai_accounting.kernel.types import YearMonth


def bank_source():
    fact = BankStatement.model_validate_json(
        json.dumps(
            {
                "period": "2026-04",
                "bank_account_id": "bank",
                "opening_fen": 0,
                "closing_fen": 40000,
                "entries": [
                    {"reference": "line-1", "actual_date": "2026-04-02", "signed_fen": 40000}
                ],
            }
        )
    )
    return FactVersion("bank-f1", "bank-statement", 1, fact, (EVIDENCE,))


def source_receipt(source, start, end, location):
    return {
        "trigger": "receipt",
        "period": "2026-04",
        "actual_date": "2026-04-02",
        "gross_start_fen": start,
        "gross_end_fen": end,
        "source": source_ref(source, location=location),
        "receipt_stage": "during_or_after_transaction",
    }


@pytest.mark.parametrize("revised_source", [False, True])
def test_payment_cannot_be_consumed_twice_by_locations_or_fact_versions(revised_source):
    first = payment("received", "2026-04-02", 40000)
    second = (
        FactVersion("received-f2", first.subject_id, 2, first.fact, first.evidence)
        if revised_source
        else first
    )
    basis, contract = make_basis(
        candidates=[
            source_receipt(first, 0, 40000, "allocation-1"),
            source_receipt(second, 40000, 80000, "allocation-2"),
        ]
    )
    with pytest.raises(NeedsInformation) as error:
        resolve_tax_point(
            context(basis, contract, first, second),
            "service_sale",
            "sale",
            149400,
            YearMonth("2026-04"),
        )
    assert error.value.issues[0]["field"] == "tax_point_basis.candidates.source"
    assert "只能消费一次" in error.value.issues[0]["message"]


@pytest.mark.parametrize("revised_source", [False, True])
@pytest.mark.parametrize("source_kind", ["bank", "material"])
def test_original_row_cannot_be_consumed_twice_by_ranges_or_fact_versions(
    revised_source,
    source_kind,
):
    first = bank_source() if source_kind == "bank" else original()
    location = "line-1" if source_kind == "bank" else "contract"
    second = (
        FactVersion(first.id + "-revision", first.subject_id, 2, first.fact, first.evidence)
        if revised_source
        else first
    )
    basis, contract = make_basis(
        candidates=[
            source_receipt(first, 0, 40000, location),
            source_receipt(second, 40000, 80000, location),
        ]
    )
    with pytest.raises(NeedsInformation) as error:
        resolve_tax_point(
            context(basis, contract, first, second),
            "service_sale",
            "sale",
            149400,
            YearMonth("2026-04"),
        )
    assert error.value.issues[0]["field"] == "tax_point_basis.candidates.source"
    assert "只能消费一次" in error.value.issues[0]["message"]


@pytest.mark.parametrize("source_kind", ["bank", "material"])
def test_raw_original_cannot_assign_receipt_to_a_business_without_typed_allocation(source_kind):
    receipt = bank_source() if source_kind == "bank" else original()
    location = "line-1" if source_kind == "bank" else "contract"
    basis, contract = make_basis(candidates=[source_receipt(receipt, 0, 40000, location)])
    with pytest.raises(NeedsInformation) as error:
        resolve_tax_point(
            context(basis, contract, receipt),
            "service_sale",
            "sale",
            149400,
            YearMonth("2026-04"),
        )
    issue = error.value.issues[0]
    assert issue["field"] == "tax_point_basis.candidates.source"
    assert "本业务收款归属" in issue["message"]
    assert "payment.allocations" in issue["reusable_sources"]


def test_shared_typed_receipt_uses_only_each_business_allocation_without_calculations():
    fact = Payment.model_validate_json(
        json.dumps(
            {
                "period": "2026-04",
                "actual_date": "2026-04-02",
                "direction": "inflow",
                "bank_account_id": "bank",
                "counterparty_id": "customer",
                "amount_fen": 80000,
                "allocations": [
                    {
                        "source_kind": "service_sale",
                        "source_id": business,
                        "obligation": "primary",
                        "amount_fen": 40000,
                    }
                    for business in ("sale-a", "sale-b")
                ],
            }
        )
    )
    receipt = FactVersion("shared-f1", "shared-receipt", 1, fact, (EVIDENCE,))
    total = 0
    for business in ("sale-a", "sale-b"):
        basis, contract = make_basis(
            business_id=business,
            candidates=[source_receipt(receipt, 0, 40000, "allocation")],
        )
        result = resolve_tax_point(
            context(basis, contract, receipt),
            "service_sale",
            business,
            40000,
            YearMonth("2026-04"),
        )
        total += sum(i.gross_end_fen - i.gross_start_fen for i in result.intervals)
    assert total == fact.amount_fen
    basis, contract = make_basis(
        business_id="sale-a",
        candidates=[source_receipt(receipt, 0, 80000, "allocation")],
    )
    with pytest.raises(NeedsInformation) as error:
        resolve_tax_point(
            context(basis, contract, receipt),
            "service_sale",
            "sale-a",
            80000,
            YearMonth("2026-04"),
        )
    assert error.value.issues[0]["field"] == "tax_point_basis.candidates.source"


def test_distinct_receipts_keep_exact_business_ranges_and_invoice_union():
    first, second = payment("first", "2026-04-02", 40000), payment("second", "2026-05-02", 109400)
    basis, contract = make_basis(
        candidates=[
            candidate(first, 0, 40000),
            candidate(second, 40000, 149400),
            {
                "trigger": "invoice",
                "period": "2026-05",
                "gross_start_fen": 0,
                "gross_end_fen": 149400,
                "source": source_ref(original()),
            },
        ]
    )
    result = resolve_tax_point(
        context(basis, contract, first, second),
        "service_sale",
        "sale",
        149400,
        YearMonth("2026-05"),
    )
    assert [(i.gross_start_fen, i.gross_end_fen, str(i.period)) for i in result.intervals] == [
        (0, 40000, "2026-04"),
        (40000, 149400, "2026-05"),
    ]
