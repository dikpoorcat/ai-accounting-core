"""Source facts determine VAT points without receipt-calculation dependencies."""

import json

import pytest

from ai_accounting.kernel.contracts import Context, FactVersion, NeedsInformation, Read
from ai_accounting.kernel.domains import transactions
from ai_accounting.kernel.materials import MaterialSource
from ai_accounting.kernel.tax_point_basis import (
    TaxPointBasis,
    resolve_business_tax_point,
    resolve_tax_point,
    tax_basis_reads,
    tax_basis_source_reads,
)
from ai_accounting.kernel.types import YearMonth

EVIDENCE = "a" * 64


def original():
    fact = MaterialSource.model_validate_json(
        json.dumps(
            {
                "period": "2026-06",
                "evidence_digest": EVIDENCE,
                "category": "tax",
                "purpose": "supporting",
                "supporting_purpose": "明确服务合同、收款权及完整候选核对",
                "specification": {
                    "format": "text",
                    "all_pages_reviewed": True,
                    "passages": [
                        {
                            "location": "contract",
                            "page": 1,
                            "excerpt": "合同付款期及实际服务开始与完成来源；不存在此前发票。",
                        }
                    ],
                },
            }
        )
    )
    return FactVersion("source-f1", "source", 1, fact, (EVIDENCE,))


def source_ref(version, *, location="contract"):
    return {
        "kind": version.fact.kind,
        "subject_id": version.subject_id,
        "fact_id": version.id,
        "evidence_digest": EVIDENCE,
        "location": location,
    }


def make_basis(**changes):
    source = original()
    fields = {
        "period": "2026-06",
        "business_kind": "service_sale",
        "business_id": "sale",
        "service_pattern": "single",
        "written_contract_status": "payment_date_set",
        "contract_payment_period": "2026-06",
        "contract_source": source_ref(source),
        "reviewed_through_period": "2026-06",
        "earlier_candidates_reviewed": True,
        "review_source": source_ref(source),
        **changes,
    }
    fact = TaxPointBasis.model_validate_json(json.dumps(fields))
    return FactVersion("basis-f1", "basis", 1, fact, (EVIDENCE,)), source


def context(basis, *sources):
    return Context(
        {
            tax_basis_reads(basis.fact.business_kind, basis.fact.business_id)[0]: (basis,),
            **{Read("fact", s.fact.kind, "#" + s.id): (s,) for s in sources},
        }
    )


def payment(subject, day, amount, *, business_kind="service_sale"):
    fact = transactions.Payment.model_validate_json(
        json.dumps(
            {
                "period": day[:7],
                "actual_date": day,
                "direction": "inflow",
                "bank_account_id": "bank",
                "counterparty_id": "customer",
                "amount_fen": amount,
                "allocations": [
                    {
                        "source_kind": business_kind,
                        "source_id": "sale",
                        "obligation": "primary",
                        "amount_fen": amount,
                    }
                ],
            }
        )
    )
    return FactVersion(subject + "-f1", subject, 1, fact, (EVIDENCE,))


def candidate(version, start, end, *, trigger="receipt", stage="during_or_after_transaction"):
    return {
        "trigger": trigger,
        "period": str(version.fact.period),
        "actual_date": str(version.fact.actual_date),
        "gross_start_fen": start,
        "gross_end_fen": end,
        "source": source_ref(version, location="actual receipt"),
        **({"receipt_stage": stage} if trigger == "receipt" else {}),
    }


def test_contract_month_is_not_converted_to_a_month_end_day():
    basis, source = make_basis()
    ctx = context(basis, source)
    result = resolve_tax_point(ctx, "service_sale", "sale", 149400, YearMonth("2026-03"))
    assert [
        (i.period, i.actual_date, i.gross_start_fen, i.gross_end_fen) for i in result.intervals
    ] == [("2026-06", None, 0, 149400)]
    assert all(r.source == "fact" for r in tax_basis_source_reads(ctx, "service_sale", "sale"))
    assert result.public()["rule_effective_from"] == "2026-01-01"


def test_reused_exact_source_is_verified_once_per_resolution():
    basis, source = make_basis(
        candidates=[
            {
                "trigger": "invoice",
                "period": "2026-06",
                "gross_start_fen": 0,
                "gross_end_fen": 10100,
                "source": source_ref(original()),
            }
        ]
    )

    class CountedContext(Context):
        def __init__(self):
            super().__init__(
                {
                    tax_basis_reads("service_sale", "sale")[0]: (basis,),
                    Read("fact", source.fact.kind, "#" + source.id): (source,),
                }
            )
            self.source_reads = 0

        def select(self, read):
            if read.key == "#" + source.id:
                self.source_reads += 1
            return super().select(read)

    ctx = CountedContext()
    for expected_reads in (1, 2):
        result = resolve_tax_point(ctx, "service_sale", "sale", 10100, YearMonth("2026-06"))
        assert ctx.source_reads == expected_reads
        assert [(i.gross_start_fen, i.gross_end_fen) for i in result.intervals] == [(0, 10100)]
        assert source.id in ctx.versions


@pytest.mark.parametrize(
    "source_change,expected_field",
    [
        ({"location": "unpreserved-contract"}, "tax_point_basis.source.location"),
        ({"evidence_digest": "b" * 64}, "tax_point_basis.source.evidence_digest"),
    ],
)
def test_same_fact_source_reuse_still_checks_each_location_and_digest(
    source_change, expected_field
):
    basis, source = make_basis(
        candidates=[
            {
                "trigger": "invoice",
                "period": "2026-06",
                "gross_start_fen": 0,
                "gross_end_fen": 10100,
                "source": source_ref(original()) | source_change,
            }
        ]
    )
    with pytest.raises(NeedsInformation) as error:
        resolve_tax_point(
            context(basis, source), "service_sale", "sale", 10100, YearMonth("2026-06")
        )
    assert error.value.issues[0]["field"] == expected_field


@pytest.mark.parametrize(
    "origin,expected_field",
    [
        ("contract", "contract_payment_date"),
        ("performance", "performance_completion_date"),
        ("first_service", "first_service_date"),
        ("invoice", "candidates.actual_date"),
    ],
)
def test_partial_month_policy_requires_the_actual_source_date_field(origin, expected_field):
    from test_business_domains import vat_policy

    changes = {"contract_payment_period": "2026-03"}
    sources = []
    kind = "service_sale"
    if origin == "performance":
        changes = {
            "written_contract_status": "no_payment_date",
            "contract_payment_period": None,
            "performance_completion_period": "2026-03",
            "performance_source": source_ref(original()),
        }
    elif origin == "first_service":
        kind = "advance"
        receipt = payment("prepaid", "2026-01-02", 149400, business_kind=kind)
        sources.append(receipt)
        changes = {
            "business_kind": kind,
            "service_pattern": "phased",
            "first_service_started": True,
            "first_service_period": "2026-03",
            "first_service_source": source_ref(original()),
            "candidates": [candidate(receipt, 0, 149400, stage="before_transaction")],
        }
    elif origin == "invoice":
        changes = {
            "candidates": [
                {
                    "trigger": "invoice",
                    "period": "2026-03",
                    "gross_start_fen": 0,
                    "gross_end_fen": 149400,
                    "source": source_ref(original()),
                }
            ]
        }
    basis, source = make_basis(**changes)
    point = resolve_tax_point(
        context(basis, source, *sources),
        kind,
        "sale",
        149400,
        YearMonth("2026-03"),
    )
    interval = point.intervals[0]
    policy = type(vat_policy()).model_validate_json(
        json.dumps(
            vat_policy().model_dump(mode="json") | {"effective_from": "2026-03-15"},
        )
    )
    with pytest.raises(NeedsInformation) as error:
        transactions._tax_policy_for_month(
            policy,
            interval.period,
            interval.actual_date,
            point=point,
            interval=interval,
        )
    issue = error.value.issues[0]
    assert issue["field"] == "tax_point_basis." + expected_field
    assert issue["allowed_precision"] == ["day"]
    assert issue["reusable_sources"] == [basis.id, source.subject_id, source.id]
    assert interval.actual_date is None


def test_explicit_tax_policy_precision_still_requires_the_actual_day_when_necessary():
    from test_business_domains import vat_policy

    policy = type(vat_policy()).model_validate_json(
        json.dumps(
            vat_policy().model_dump(mode="json") | {"effective_from": "2026-03-15"},
        )
    )
    with pytest.raises(NeedsInformation) as error:
        transactions._tax_policy_for_month(policy, YearMonth("2026-03"), None)
    assert error.value.issues[0]["field"] == "tax_obligation_date"


def test_zero_consideration_needs_an_explicit_taxable_basis_without_invalid_ranges():
    basis, source = make_basis(business_kind="asset_disposal")
    with pytest.raises(NeedsInformation) as error:
        resolve_tax_point(
            context(basis, source),
            "asset_disposal",
            "sale",
            0,
            YearMonth("2026-06"),
        )
    assert error.value.issues[0]["field"] == "tax_point_basis"
    assert error.value.issues[0]["reusable_sources"] == ["basis"]


def test_completion_fallback_requires_explicit_absent_contract_date_and_completion_source():
    basis, source = make_basis(
        written_contract_status="no_payment_date",
        contract_payment_period=None,
        performance_completion_period="2026-03",
        performance_source=source_ref(original()),
    )
    result = resolve_tax_point(
        context(basis, source), "service_sale", "sale", 10100, YearMonth("2026-03")
    )
    assert result.intervals[0].period == "2026-03"
    assert result.intervals[0].actual_date is None
    missing, source = make_basis(
        written_contract_status="no_written_contract", contract_payment_period=None
    )
    with pytest.raises(NeedsInformation) as error:
        resolve_tax_point(
            context(missing, source), "service_sale", "sale", 10100, YearMonth("2026-03")
        )
    assert error.value.issues[0]["field"] == "tax_point_basis.performance_completion_period"


@pytest.mark.parametrize(
    "changes", [{"earlier_candidates_reviewed": False}, {"reviewed_through_period": "2026-02"}]
)
def test_a_later_receipt_cannot_prove_absence_of_earlier_tax_points(changes):
    basis, source = make_basis(**changes)
    with pytest.raises(NeedsInformation) as error:
        resolve_tax_point(
            context(basis, source), "service_sale", "sale", 10100, YearMonth("2026-03")
        )
    assert error.value.issues[0]["field"] == "tax_point_basis.earlier_candidates_reviewed"


def test_two_partial_receipts_cover_only_their_gross_ranges_and_round_cumulatively():
    first, second = payment("first", "2026-04-02", 40000), payment("second", "2026-05-10", 109400)
    basis, source = make_basis(
        candidates=[candidate(first, 0, 40000), candidate(second, 40000, 149400)]
    )
    result = resolve_tax_point(
        context(basis, source, first, second), "service_sale", "sale", 149400, YearMonth("2026-05")
    )
    from decimal import Decimal

    april = transactions._tax_interval_amounts(result.intervals, Decimal("1"), YearMonth("2026-04"))
    may = transactions._tax_interval_amounts(result.intervals, Decimal("1"), YearMonth("2026-05"))
    assert april == (39604, 396)
    assert may == (108317, 1083)
    assert tuple(a + b for a, b in zip(april, may, strict=True)) == (147921, 1479)


def test_whole_contractual_entitlement_and_partial_receipt_are_not_counted_twice():
    receipt = payment("received", "2026-06-10", 40000)
    basis, source = make_basis(
        contract_payment_date="2026-06-01", candidates=[candidate(receipt, 0, 40000)]
    )
    result = resolve_tax_point(
        context(basis, source, receipt), "service_sale", "sale", 149400, YearMonth("2026-06")
    )
    assert sum(i.gross_end_fen - i.gross_start_fen for i in result.intervals) == 149400
    assert all(i.trigger == "contract" for i in result.intervals)


def test_before_transaction_ordinary_receipt_is_not_automatically_taxable():
    receipt = payment("prepaid", "2026-03-10", 149400, business_kind="advance")
    basis, source = make_basis(
        business_kind="advance",
        written_contract_status="no_payment_date",
        contract_payment_period=None,
        candidates=[candidate(receipt, 0, 149400, stage="before_transaction")],
    )
    result = resolve_tax_point(
        context(basis, source, receipt), "advance", "sale", 149400, YearMonth("2026-03")
    )
    assert result.intervals == ()


def test_phased_prepaid_service_taxes_all_received_amount_at_first_actual_service():
    receipt = payment("prepaid", "2026-03-10", 149400, business_kind="advance")
    basis, source = make_basis(
        business_kind="advance",
        service_pattern="phased",
        first_service_started=True,
        first_service_period="2026-04",
        first_service_source=source_ref(original()),
        candidates=[candidate(receipt, 0, 149400, stage="before_transaction")],
    )
    result = resolve_tax_point(
        context(basis, source, receipt), "advance", "sale", 149400, YearMonth("2026-04")
    )
    assert (
        sum(i.gross_end_fen - i.gross_start_fen for i in result.intervals if i.period == "2026-04")
        == 149400
    )
    assert not any(i.period == "2026-03" for i in result.intervals)


def test_phased_service_contractual_payment_date_precedes_first_actual_service():
    receipt = payment("prepaid", "2026-03-10", 149400, business_kind="advance")
    basis, source = make_basis(
        business_kind="advance",
        service_pattern="phased",
        contract_payment_period="2026-03",
        first_service_started=True,
        first_service_period="2026-04",
        first_service_source=source_ref(original()),
        candidates=[candidate(receipt, 0, 149400, stage="before_transaction")],
    )
    result = resolve_tax_point(
        context(basis, source, receipt), "advance", "sale", 149400, YearMonth("2026-03")
    )
    assert all(i.period == "2026-03" for i in result.intervals)


def test_phased_service_does_not_invent_a_missing_first_start():
    basis, source = make_basis(service_pattern="phased")
    with pytest.raises(NeedsInformation) as error:
        resolve_tax_point(
            context(basis, source), "service_sale", "sale", 10100, YearMonth("2026-03")
        )
    assert error.value.issues[0]["field"] == "tax_point_basis.first_service_started"


def test_receipt_source_date_and_allocation_must_match_the_exact_fact():
    receipt = payment("received", "2026-04-02", 40000)
    bad = candidate(receipt, 0, 41000)
    basis, source = make_basis(candidates=[bad])
    with pytest.raises(NeedsInformation) as error:
        resolve_tax_point(
            context(basis, source, receipt), "service_sale", "sale", 149400, YearMonth("2026-04")
        )
    assert error.value.issues[0]["field"] == "tax_point_basis.candidates.source"


def test_receipt_ranges_cannot_overlap_and_hide_the_second_real_receipt():
    first, second = payment("first", "2026-04-02", 40000), payment("second", "2026-05-10", 109400)
    basis, source = make_basis(
        candidates=[candidate(first, 0, 40000), candidate(second, 0, 109400)]
    )
    with pytest.raises(NeedsInformation) as error:
        resolve_tax_point(
            context(basis, source, first, second),
            "service_sale",
            "sale",
            149400,
            YearMonth("2026-05"),
        )
    assert error.value.issues[0]["field"] == "tax_point_basis.candidates.gross_start_fen"


@pytest.mark.parametrize(
    "model,fields",
    [
        (transactions.ServiceSale, {"customer_id": "customer", "gross_fen": 10100}),
        (
            transactions.Advance,
            {"counterparty_id": "customer", "amount_fen": 10100, "side": "customer"},
        ),
        (transactions.AdvanceFulfillment, {"advance_id": "advance", "fulfilled_gross_fen": 10100}),
    ],
)
@pytest.mark.parametrize("tax_date", [None, "2026-04-02"])
def test_registration_accepts_explicit_source_tax_month_without_contract_or_invoice_basis(
    model, fields, tax_date
):
    fact = model.model_validate_json(
        json.dumps(
            {
                "period": "2026-03",
                "tax_obligation_period": "2026-04",
                "tax_obligation_date": tax_date,
                **fields,
            }
        )
    )
    fact.validate_registration()
    assert fact.tax_obligation_period == "2026-04"
    assert fact.tax_obligation_date == tax_date
    version = FactVersion("business-f1", "business", 1, fact, (EVIDENCE,))
    context_without_basis = Context({tax_basis_reads(fact.kind, version.subject_id)[0]: ()})
    assert resolve_business_tax_point(version, context_without_basis, 10100) is None
    schema = model.model_json_schema()["properties"]
    assert not schema["tax_obligation_period"].get("readOnly", False)
    assert not schema["tax_obligation_date"].get("readOnly", False)
