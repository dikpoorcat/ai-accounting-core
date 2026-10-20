"""Real tax, operating, treasury and accounting modules in temporary companies."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.application.public import create_application, create_backup_verifier
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.domains.taxes.public import create_taxes
from ai_accounting_modular.domains.taxes.public import repository_factory as taxes_repository
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.public import LedgerQuery, create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.money.public import create_money
from ai_accounting_modular.money.public import repository_factory as money_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog

from ._fixtures import (
    ACCOUNT,
    CUSTOMER,
    LOCATION,
    actual_vat,
    assessment,
    confirmed_income,
    evidence,
    fact,
    income_actual,
    income_inputs,
    policy,
    sale,
    surtax,
    uid,
)


@pytest.fixture
def assembled(tmp_path, request):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-password-hash"))
    storage = Storage(
        catalog,
        repositories=[
            application_repository(),
            identity_repository(),
            ledger_repository(),
            records_repository(),
            operations_repository(),
            money_repository(),
            taxes_repository(),
        ],
    )
    company_id = str(uuid4())
    storage.create_company(
        CompanySpec(
            company_id,
            str(uuid4()),
            "913100000000000001",
            "合成税务公司",
            getattr(request, "param", "2026-01"),
            tmp_path / "company",
        )
    )
    identity, ledger = create_identity(), create_ledger()
    money = create_money(identity=identity, ledger=ledger)
    app = create_application(
        storage=storage,
        identity=identity,
        ledger=ledger,
        money=money,
        records=create_records(root_for=lambda read: storage.company_metadata(read).company_root),
        domains=[create_taxes(), create_operations(), money],
    )
    return app, company_id, storage, ledger


def request(company_id, *facts, mutations=()):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company_id,
        "command_id": str(uuid4()),
        "objects": [
            {
                "object_id": CUSTOMER,
                "kind": "organization",
                "name": "合成客户",
                "expected_seq": 0,
                "data": {},
            },
            {
                "object_id": ACCOUNT,
                "kind": "bank_account",
                "name": "合成银行",
                "expected_seq": 0,
                "data": {
                    "bank_name": "合成银行",
                    "account_number": "synthetic-tax-bank",
                    "currency": "CNY",
                    "owner": "company",
                },
            },
        ],
        "sources": [evidence()],
        "mutations": [
            *({"op": "create", "expected_seq": 0, "fact": deepcopy(row)} for row in facts),
            *deepcopy(mutations),
        ],
    }


def get(app, company_id, identity):
    result = app.accounting_get(
        {
            "company_id": company_id,
            "business_ids": [identity],
            "include": ["facts", "results", "sources"],
        }
    )
    return result["businesses"][0]


def main(app, company_id, current):
    return next(
        row
        for row in get(app, company_id, current["business_id"])["results"]
        if row["reference"]["result_slot"] == "main"
    )


def obligations(app, company_id, current):
    with app.storage.read_unit(company_id) as read:
        page = app.ledger.query_obligations(LedgerQuery((current["business_id"],)), read)
    return [row["obligation"] for row in page.items]


def remaining(app, company_id, current, slot="tax"):
    with app.storage.read_unit(company_id) as read:
        page = app.ledger.query_obligations(LedgerQuery((current["business_id"],)), read)
    return next(
        row["balance"]["remaining_fen"] for row in page.items if row["obligation"]["slot"] == slot
    )


def tax_cash(name, on, direction, allocations):
    return fact(
        name,
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": ACCOUNT,
            "direction": direction,
            "amount_fen": str(sum(int(value) for _, _, value in allocations)),
            "payment_method": "individual",
            "allocations": [
                {"source": {"business_id": row["business_id"], "slot": slot}, "amount_fen": value}
                for row, slot, value in allocations
            ],
        },
        on[:7],
        effective_on=on,
    )


def netting(app, company_id, current):
    return next(
        row
        for row in get(app, company_id, current["business_id"])["results"]
        if row["reference"]["result_slot"] == "income-tax-netting"
    )


def replace(fact, seq=1):
    return {"op": "replace", "expected_seq": seq, "fact": deepcopy(fact)}


def test_real_same_unit_vat_sources_and_later_new_publisher_recalculate_aggregate(assembled):
    app, company_id, _, _ = assembled
    current = assessment()
    created = app.accounting_apply(request(company_id, current, sale(), surtax(), policy()))
    assert created["completion"] == "complete", created
    original = main(app, company_id, current)
    assert original["target"]["values"]["payable_vat_fen"] == "3000"
    assert original["target"]["values"]["surtax_fen"] == "180"
    assert len(original["basis"]["adopted_quantities"]) == 1
    extra = sale("extra", "10300")
    created = app.accounting_apply(request(company_id, extra))
    assert created["completion"] == "complete", created
    updated = main(app, company_id, current)
    assert updated["target"]["values"]["payable_vat_fen"] == "3300"
    assert updated["reference"]["voucher_number"] == original["reference"]["voucher_number"]
    assert len(updated["basis"]["adopted_quantities"]) == 2


def return_fact(original, name="return", gross="10300", vat="300"):
    return fact(
        name,
        "operations.sale_return",
        {
            "sale_id": original["business_id"],
            "customer_id": CUSTOMER,
            "gross_fen": gross,
            "credit_note_vat_fen": vat,
            "original_tax_period": original["period"],
        },
        "2026-02",
    )


def scenario(app, company_id, *, positive=True, actual_zero=False):
    original, jan, feb = (
        sale("original", "10300"),
        assessment("january"),
        assessment("february", "2026-02"),
    )
    result = app.accounting_apply(
        request(company_id, original, jan, policy(), surtax(zero=True), actual_vat(jan, "300"))
    )
    assert result["completion"] == "complete", result.get("message", result)
    paid = fact(
        "january-tax-payment",
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": ACCOUNT,
            "direction": "outflow",
            "amount_fen": "300",
            "payment_method": "individual",
            "allocations": [
                {"source": {"business_id": jan["business_id"], "slot": "vat"}, "amount_fen": "300"}
            ],
        },
        effective_on="2026-01-31",
    )
    result = app.accounting_apply(request(company_id, paid))
    assert result["completion"] == "complete", result
    returned = return_fact(original)
    rows = [returned, feb]
    if positive:
        rows.append(sale("feb-sale", "30900", "2026-02"))
    if actual_zero:
        rows.append(actual_vat(feb, "0"))
    result = app.accounting_apply(request(company_id, *rows))
    assert result["completion"] == "complete", result
    return original, jan, feb, returned


def confirmed_credit(original, jan, returned, *, partial=False, surtax_credit="0", **extra):
    item = {
        "return_id": returned["business_id"],
        "original_business_id": original["business_id"],
        "gross_fen": "3433" if partial else "10300",
        "vat_fen": "100" if partial else "300",
    }
    if partial:
        item["tax_ranges"] = [{"from_fen": "0", "to_fen": "3433"}]
    return fact(
        "credit",
        "taxes.tax_credit",
        {
            "original_assessment_id": jan["business_id"],
            "start_month": "2026-01",
            "end_month": "2026-01",
            "returns": [item],
            "original_filing_source": LOCATION,
            "authority_source": LOCATION,
            "disposition": "refund",
            "vat_credit_fen": "100" if partial else "300",
            "surtax_credit_fen": surtax_credit,
            "exemption_reversal_fen": "0",
            **extra,
        },
        "2026-02",
    )


def test_real_cross_period_return_normal_route_is_600_not_old_period_reassessment(assembled):
    app, company_id, _, _ = assembled
    _, jan, feb, _ = scenario(app, company_id)
    assert main(app, company_id, feb)["target"]["values"]["payable_vat_fen"] == "600"
    assert main(app, company_id, jan)["target"]["values"]["payable_vat_fen"] == "300"
    assert {row["slot"] for row in obligations(app, company_id, feb)} == {"vat", "surtax"}


@pytest.mark.parametrize("partial,payable,claim", [(False, "900", "300"), (True, "700", "100")])
def test_real_confirmed_full_and_partial_credit_do_not_double_count(
    assembled, partial, payable, claim
):
    app, company_id, _, _ = assembled
    original, jan, feb, returned = scenario(app, company_id)
    credit = confirmed_credit(original, jan, returned, partial=partial)
    feb["data"]["credit_ids"] = [credit["business_id"]]
    result = app.accounting_apply(request(company_id, credit, mutations=[replace(feb)]))
    assert result["completion"] == "complete", result
    current = main(app, company_id, feb)
    assert current["target"]["values"]["payable_vat_fen"] == payable
    receivables = [
        row for row in obligations(app, company_id, feb) if row["direction"] == "receivable"
    ]
    assert len(receivables) == 1 and receivables[0]["amount_fen"] == claim
    assert receivables[0]["allowed_settlements"] == ["money"]
    assert main(app, company_id, returned)["target"]["values"]["vat_reversed_fen"] == "300"
    assert main(app, company_id, credit)["vouchers"] == []
    assert main(app, company_id, jan)["target"]["values"]["payable_vat_fen"] == "300"


def test_real_negative_return_actual_zero_preserves_vat_debit_not_claim_or_expense(assembled):
    app, company_id, storage, ledger = assembled
    _, _, feb, _ = scenario(app, company_id, positive=False, actual_zero=True)
    current = main(app, company_id, feb)
    values = current["target"]["values"]
    assert (values["net_sales_fen"], values["accrued_vat_fen"], values["payable_vat_fen"]) == (
        "-10000",
        "-300",
        "0",
    )
    assert "rounding_adjustment_fen" not in values and current["vouchers"] == []
    assert all(
        row["direction"] == "payable" and row["amount_fen"] == "0"
        for row in obligations(app, company_id, feb)
    )
    with storage.read_unit(company_id) as read:
        vouchers = ledger.query_vouchers(LedgerQuery(), read)
    assert (
        sum(
            int(line["debit_fen"]) - int(line["credit_fen"])
            for voucher in vouchers.items
            for line in voucher["lines"]
            if line["account_code"] == "222101"
        )
        == 300
    )


def test_duplicate_tax_credit_range_rolls_back_and_partial_range_is_required(assembled):
    app, company_id, _, _ = assembled
    original, jan, feb, returned = scenario(app, company_id)
    first = confirmed_credit(original, jan, returned, partial=True)
    assert app.accounting_apply(request(company_id, first))["completion"] == "complete"
    duplicate = deepcopy(first)
    duplicate["business_id"] = uid("duplicate-credit")
    result = app.accounting_apply(request(company_id, duplicate))
    assert result["completion"] == "not_saved", result
    missing = deepcopy(first)
    missing["business_id"] = uid("missing-range-credit")
    missing["data"]["returns"][0].pop("tax_ranges")
    result = app.accounting_apply(request(company_id, missing))
    assert result["completion"] == "not_saved" and result["error"] == "needs_information", result


def test_real_actual_vat_can_be_adopted_without_reference_policies(assembled):
    app, company_id, _, _ = assembled
    current = assessment(policies=False)
    result = app.accounting_apply(
        request(
            company_id,
            current,
            actual_vat(current, "900"),
            sale("actual-source", "30900"),
            policy(),
        )
    )
    assert result["completion"] == "complete", result
    values = main(app, company_id, current)["target"]["values"]
    assert values["payable_vat_fen"] == "900" and values["adopted_basis"] == "actual"
    assert values["filing_state"] == "partial"


def test_new_actual_variance_does_not_replace_a_valid_assessment_or_fact(assembled):
    app, company_id, _, _ = assembled
    current = assessment()
    result = app.accounting_apply(
        request(company_id, current, sale("actual-source", "20600"), policy(), surtax(zero=True))
    )
    assert result["completion"] == "complete", result
    before = main(app, company_id, current)
    declared = actual_vat(current, "900")
    result = app.accounting_apply(request(company_id, declared))
    assert result["completion"] == "not_saved" and result["error"] == "needs_information", result
    after = main(app, company_id, current)
    assert after == before
    absent = app.accounting_get(
        {"company_id": company_id, "business_ids": [declared["business_id"]], "include": ["facts"]}
    )
    assert absent["businesses"] == [] and absent["missing_business_ids"] == [
        declared["business_id"]
    ]


def test_overlapping_vat_periods_are_not_two_independent_tax_debts(assembled):
    app, company_id, _, _ = assembled
    result = app.accounting_apply(
        request(company_id, policy(), surtax(), assessment(), assessment("duplicate"))
    )
    assert result["completion"] == "not_saved", result


def test_real_income_tax_reads_shared_profit_after_vat_without_counting_itself(assembled):
    app, company_id, _, _ = assembled
    income, basis, income_policy = income_inputs()
    earned = sale("income-source", "2060000")
    result = app.accounting_apply(
        request(
            company_id,
            income,
            basis,
            income_policy,
            assessment(),
            earned,
            policy(),
            surtax(zero=True),
        )
    )
    assert result["completion"] == "complete", result.get("message", result)
    values = main(app, company_id, income)["target"]["values"]
    assert values["profit_before_tax_fen"] == "2000000"
    assert values["cumulative_tax_fen"] == "100000"
    refs = main(app, company_id, income)["basis"]["source_results"]
    assert earned["business_id"] in {ref["business_id"] for ref in refs}
    assert income["business_id"] not in {ref["business_id"] for ref in refs}
    extra = sale("later-income-source", "10300", "2026-02")
    result = app.accounting_apply(request(company_id, extra))
    assert result["completion"] == "complete", result.get("message", result)
    values = main(app, company_id, income)["target"]["values"]
    assert values["profit_before_tax_fen"] == "2010000"
    assert values["cumulative_tax_fen"] == "100500"


def test_real_later_income_actual_adopts_90000_not_30000_tax_due(assembled):
    app, company_id, storage, _ = assembled
    income, _, _ = income_inputs()
    declared = fact(
        "income-declaration",
        "taxes.actual_declaration",
        {
            "type": "income_tax",
            "assessment_id": income["business_id"],
            "tax_year": 2026,
            "tax_period_end": "2026-03",
            "basis_type": "filed_return",
            "source": LOCATION,
            "cumulative_tax_fen": "90000",
            "prepaid_tax_fen": "60000",
            "tax_due_fen": "30000",
        },
        "2026-04",
    )
    result = app.accounting_apply(request(company_id, income, declared))
    assert result["completion"] == "complete", result.get("message", result)
    values = main(app, company_id, income)["target"]["values"]
    assert values["cumulative_tax_fen"] == values["change_fen"] == "90000"
    assert main(app, company_id, declared)["vouchers"] == []
    explicit = {income["data"]["policy_id"], income["data"]["prepayment_basis_id"]}
    with storage.read_unit(company_id) as read:
        repository = read.repository("application")
        assert {
            row["producer_id"] for row in repository.relations([income["business_id"]])
        } == explicit
        assert not explicit.intersection(
            row["producer_id"] for row in repository.dependencies([income["business_id"]])
        )
        # The fact keeps these references; the actual filing needs neither missing producer.
        assert repository.heads(explicit) == {}
        verifier = create_backup_verifier(
            identity=app.identity,
            ledger=app.ledger,
            records=app.records,
            domains=tuple(
                dict.fromkeys(app.registry.owner(kind) for kind in app.registry.supported_kinds)
            ),
            repositories=(),
        )
        verifier.verify_snapshot(read)


def test_real_earlier_income_cumulative_correction_recomputes_later_difference(assembled):
    app, company_id, _, _ = assembled
    first = fact(
        "income-first",
        "taxes.assessment",
        {
            "type": "income_tax",
            "method": "confirmed_provision",
            "tax_year": 2026,
            "tax_period_end": "2026-03",
            "cumulative_tax_fen": "100000",
            "source": LOCATION,
        },
        "2026-03",
    )
    second = fact(
        "income-second",
        "taxes.assessment",
        {**first["data"], "tax_period_end": "2026-06", "cumulative_tax_fen": "200000"},
        "2026-06",
    )
    result = app.accounting_apply(request(company_id, second, first))
    assert result["completion"] == "complete", result.get("message", result)
    assert main(app, company_id, second)["target"]["values"]["change_fen"] == "100000"
    first["data"]["cumulative_tax_fen"] = "80000"
    result = app.accounting_apply(request(company_id, mutations=[replace(first)]))
    assert result["completion"] == "complete", result.get("message", result)
    assert main(app, company_id, second)["target"]["values"]["change_fen"] == "120000"


def test_income_quarter_can_be_added_after_later_quarter_and_recomputes_its_difference(assembled):
    app, company_id, _, _ = assembled
    first = confirmed_income("quarter-first", "2026-03", "100000")
    second = confirmed_income("quarter-second", "2026-06", "200000")
    result = app.accounting_apply(request(company_id, second))
    assert result["completion"] == "complete", result
    assert main(app, company_id, second)["target"]["values"]["change_fen"] == "200000"
    result = app.accounting_apply(request(company_id, first))
    assert result["completion"] == "complete", result
    assert main(app, company_id, first)["target"]["values"]["change_fen"] == "100000"
    assert main(app, company_id, second)["target"]["values"]["change_fen"] == "100000"


def test_income_cumulative_boundaries_allow_same_economic_month_and_reverse_ids(assembled):
    app, company_id, _, _ = assembled
    first = confirmed_income("quarter-first", "2026-03", "100000", period="2026-06")
    second = confirmed_income("quarter-second", "2026-06", "200000")
    first["business_id"] = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    second["business_id"] = "00000000-0000-4000-8000-000000000001"
    result = app.accounting_apply(request(company_id, second, first))
    assert result["completion"] == "complete", result
    assert main(app, company_id, first)["target"]["values"]["change_fen"] == "100000"
    assert main(app, company_id, second)["target"]["values"]["change_fen"] == "100000"


@pytest.mark.parametrize("second_exists", [False, True])
def test_income_late_information_cannot_put_q1_economic_period_after_q2(second_exists, assembled):
    app, company_id, _, _ = assembled
    first = confirmed_income("quarter-first", "2026-03", "100000", period="2026-12")
    second = confirmed_income("quarter-second", "2026-06", "200000")
    if second_exists:
        result = app.accounting_apply(request(company_id, second))
        assert result["completion"] == "complete", result
        before = main(app, company_id, second)
        result = app.accounting_apply(request(company_id, first))
    else:
        result = app.accounting_apply(request(company_id, second, first))
    assert result["completion"] == "not_saved" and result["error"] == "dependency_conflict", result
    assert result["fact_issues"][0]["business_id"] == first["business_id"]
    assert result["fact_issues"][0]["field"] == "/period"
    if second_exists:
        assert main(app, company_id, second) == before


def test_surtax_credit_aggregate_does_not_invent_three_components(assembled):
    app, company_id, _, _ = assembled
    original, jan = sale("original", "10300"), assessment("january")
    declared = actual_vat(jan, "300", urban_fen="11", education_fen="5", local_education_fen="3")
    result = app.accounting_apply(request(company_id, original, jan, policy(), surtax(), declared))
    assert result["completion"] == "complete", result.get("message", result)
    returned, feb = return_fact(original), assessment("february", "2026-02")
    credit = confirmed_credit(original, jan, returned, surtax_credit="18")
    feb["data"]["credit_ids"] = [credit["business_id"]]
    result = app.accounting_apply(request(company_id, returned, feb, credit))
    assert result["completion"] == "complete", result.get("message", result)
    with app.storage.read_unit(company_id) as read:
        profit = app.ledger.pre_tax("2026-02", "2026-02", read)
    assert profit.fields["profit_before_tax"] == {"state": "known", "amount_fen": "-9982"}
    target = main(app, company_id, credit)["target"]["values"]
    assert not any(
        field in target
        for field in ("urban_credit_fen", "education_credit_fen", "local_education_credit_fen")
    )
    claim = [
        row for row in obligations(app, company_id, feb) if row["slot"].startswith("surtax_credit")
    ]
    assert claim[0]["amount_fen"] == "18"


def test_surtax_credit_known_components_cannot_borrow_another_taxs_capacity(assembled):
    app, company_id, _, _ = assembled
    original, jan = sale("original", "10300"), assessment("january")
    declared = actual_vat(jan, "300", urban_fen="11", education_fen="5", local_education_fen="3")
    result = app.accounting_apply(request(company_id, original, jan, policy(), surtax(), declared))
    assert result["completion"] == "complete", result
    returned = return_fact(original)
    credit = confirmed_credit(original, jan, returned, surtax_credit="18")
    credit["data"].update(
        urban_credit_fen="18", education_credit_fen="0", local_education_credit_fen="0"
    )
    result = app.accounting_apply(request(company_id, returned, credit))
    assert result["completion"] == "not_saved" and "同一附加税种" in result["message"], result
    credit["data"].update(
        urban_credit_fen="11", education_credit_fen="4", local_education_credit_fen="3"
    )
    result = app.accounting_apply(request(company_id, returned, credit))
    assert result["completion"] == "complete", result


def test_detailed_tax_point_publishes_tax_in_future_month_without_moving_january_revenue(assembled):
    app, company_id, _, ledger = assembled
    earned = sale(
        "future-tax-sale",
        "10300",
        tax_point={"type": "derived", "tax_point_id": uid("future-point")},
    )
    point = fact(
        "future-point",
        "taxes.tax_point",
        {
            "target_business_id": earned["business_id"],
            "transaction_mode": "ordinary_service",
            "service_state": "started",
            "contractual_entitlement": "established",
            "events": [
                {
                    "event_id": uid("start"),
                    "type": "service_start",
                    "month": "2026-01",
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "10300"}],
                },
                {
                    "event_id": uid("entitlement"),
                    "type": "contractual_entitlement",
                    "month": "2026-02",
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "10300"}],
                },
            ],
        },
    )
    jan, feb = assessment("january"), assessment("february", "2026-02")
    result = app.accounting_apply(
        request(company_id, jan, feb, earned, point, policy(), surtax(zero=True))
    )
    assert result["completion"] == "complete", result.get("message", result)
    assert main(app, company_id, point)["vouchers"] == []
    assert main(app, company_id, jan)["target"]["values"]["payable_vat_fen"] == "0"
    assert main(app, company_id, feb)["target"]["values"]["payable_vat_fen"] == "300"
    with app.storage.read_unit(company_id) as read:
        january = ledger.pre_tax("2026-01", "2026-01", read)
        february = ledger.tax_sales("2026-02", "2026-02", read)
    assert january.fields["operating_revenue"] == {"state": "known", "amount_fen": "10000"}
    assert [
        (row.sale["source_business_id"], row.sale["tax_period"], row.sale["vat_fen"])
        for row in february
    ] == [(earned["business_id"], "2026-02", "300")]


def test_receipt_derived_tax_point_uses_typed_cash_fact_without_result_dependency_cycle(assembled):
    app, company_id, _, _ = assembled
    earned = sale(
        "receipt-tax-sale",
        "10300",
        tax_point={"type": "derived", "tax_point_id": uid("receipt-point")},
    )
    received = fact(
        "service-receipt",
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": ACCOUNT,
            "direction": "inflow",
            "amount_fen": "10300",
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": earned["business_id"], "slot": "primary"},
                    "amount_fen": "10300",
                }
            ],
        },
        effective_on="2026-01-10",
    )
    point = fact(
        "receipt-point",
        "taxes.tax_point",
        {
            "target_business_id": earned["business_id"],
            "transaction_mode": "ordinary_service",
            "service_state": "started",
            "contractual_entitlement": "not_set",
            "events": [
                {
                    "event_id": uid("start"),
                    "type": "service_start",
                    "month": "2026-01",
                    "on": "2026-01-01",
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "10300"}],
                },
                {
                    "event_id": uid("receipt"),
                    "type": "receipt",
                    "month": "2026-01",
                    "on": "2026-01-10",
                    "receipt_source": {"business_id": received["business_id"], "slot": "funds"},
                    "receipt_stage": "during_or_after_transaction",
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "10300"}],
                },
            ],
        },
    )
    current = assessment()
    result = app.accounting_apply(
        request(company_id, current, point, received, earned, policy(), surtax(zero=True))
    )
    assert result["completion"] == "complete", result.get("message", result)
    assert main(app, company_id, current)["target"]["values"]["payable_vat_fen"] == "300"
    resolution = main(app, company_id, point)["target"]["values"]
    assert resolution["segments"][0]["trigger"] == "receipt"
    assert resolution["segments"][0]["tax_on"] == "2026-01-10"


@pytest.mark.parametrize("already_paid", [False, True])
def test_cumulative_income_reduction_automatically_nets_unpaid_or_carries_paid_credit(
    already_paid, assembled
):
    app, company_id, _, _ = assembled
    first = confirmed_income("income-first", "2026-03", "10000")
    lower = confirmed_income("income-lower", "2026-06", "8000")
    rows = [first]
    if already_paid:
        rows.append(tax_cash("first-paid", "2026-03-31", "outflow", [(first, "tax", "10000")]))
    result = app.accounting_apply(request(company_id, *rows))
    assert result["completion"] == "complete", result
    result = app.accounting_apply(request(company_id, lower))
    assert result["completion"] == "complete", result
    assert remaining(app, company_id, first) == ("0" if already_paid else "8000")
    assert remaining(app, company_id, lower) == ("2000" if already_paid else "0")
    assert netting(app, company_id, lower)["target"]["values"]["matched_fen"] == (
        "0" if already_paid else "2000"
    )
    assert netting(app, company_id, lower)["vouchers"] == []
    third = confirmed_income("income-third", "2026-09", "11000")
    result = app.accounting_apply(request(company_id, third))
    assert result["completion"] == "complete", result
    assert remaining(app, company_id, third) == ("1000" if already_paid else "3000")
    assert remaining(app, company_id, lower) == "0"


def test_actual_refund_transfers_exact_rights_and_does_not_eat_its_own_receipt(assembled):
    app, company_id, _, _ = assembled
    first = confirmed_income("income-first", "2026-03", "10000")
    paid = tax_cash("first-paid", "2026-03-31", "outflow", [(first, "tax", "10000")])
    result = app.accounting_apply(request(company_id, first, paid))
    assert result["completion"] == "complete", result
    lower = confirmed_income("income-lower", "2026-06", "8000")
    declared = income_actual(lower, "8000", "9000", "-1000")
    received = tax_cash("first-refund", "2026-06-30", "inflow", [(lower, "tax_refund", "1000")])
    command = request(company_id, lower, declared, received)
    result = app.accounting_apply(command)
    assert result["completion"] == "complete", result
    assert remaining(app, company_id, lower) == "1000"
    assert remaining(app, company_id, lower, "tax_refund") == "0"
    assert (
        next(row for row in obligations(app, company_id, lower) if row["slot"] == "tax_refund")[
            "amount_fen"
        ]
        == "1000"
    )
    assert app.accounting_apply(command)["completion"] == "complete"
    annual = fact(
        "annual",
        "taxes.assessment",
        {**lower["data"], "method": "annual_settlement", "tax_period_end": "2026-12"},
        "2027-05",
    )
    annual_actual = income_actual(annual, "8000", "10000", "-2000")
    received_annual = tax_cash(
        "second-refund", "2027-05-20", "inflow", [(annual, "tax_refund", "1000")]
    )
    result = app.accounting_apply(request(company_id, received_annual, annual_actual, annual))
    assert result["completion"] == "complete", result
    assert (
        next(row for row in obligations(app, company_id, annual) if row["slot"] == "tax_refund")[
            "amount_fen"
        ]
        == "1000"
    )
    assert (
        remaining(app, company_id, annual, "tax_refund") == remaining(app, company_id, lower) == "0"
    )


def test_same_december_quarter_and_annual_with_mixed_money_have_no_false_dependency_cycle(
    assembled,
):
    app, company_id, _, _ = assembled
    first = confirmed_income("income-first", "2026-03", "10000")
    fourth = confirmed_income("income-fourth", "2026-12", "8000")
    annual = fact(
        "annual",
        "taxes.assessment",
        {**fourth["data"], "method": "annual_settlement", "cumulative_tax_fen": "11000"},
        "2026-12",
    )
    mixed = tax_cash(
        "mixed-tax-payment",
        "2026-12-31",
        "outflow",
        [(first, "tax", "5000"), (annual, "tax", "3000")],
    )
    result = app.accounting_apply(request(company_id, annual, mixed, fourth, first))
    assert result["completion"] == "complete", result
    assert remaining(app, company_id, first) == "3000"
    assert remaining(app, company_id, fourth) == remaining(app, company_id, annual) == "0"
    assert netting(app, company_id, fourth)["target"]["values"]["matched_fen"] == "2000"


def test_later_income_node_keeps_frozen_prior_netting_and_its_exact_formal_source(assembled):
    app, company_id, storage, _ = assembled
    first = confirmed_income("income-first", "2026-03", "10000")
    lower = confirmed_income("income-lower", "2026-06", "8000")
    result = app.accounting_apply(request(company_id, first, lower))
    assert result["completion"] == "complete", result
    # Freeze synthetic heads directly: this verifies immutable adoption,
    # not a delivered month-close domain or workflow.
    with storage.write_unit(company_id) as uow:
        uow.repository("ledger").freeze_heads("2026-06")
    previous = get(app, company_id, lower["business_id"])["results"]
    third = confirmed_income("income-third", "2026-09", "11000")
    result = app.accounting_apply(request(company_id, third))
    assert result["completion"] == "complete", result
    assert get(app, company_id, lower["business_id"])["results"] == previous
    assert remaining(app, company_id, first) == "8000"
    assert remaining(app, company_id, third) == "3000"


def test_repeated_actual_refund_continues_uncollected_right_instead_of_duplicating_it(assembled):
    app, company_id, _, _ = assembled
    first = confirmed_income("income-first", "2026-03", "10000")
    paid = tax_cash("first-paid", "2026-03-31", "outflow", [(first, "tax", "10000")])
    lower = confirmed_income("income-lower", "2026-06", "8000")
    declared = income_actual(lower, "8000", "10000", "-2000")
    result = app.accounting_apply(request(company_id, first, paid, lower, declared))
    assert result["completion"] == "complete", result
    assert remaining(app, company_id, lower, "tax_refund") == "2000"
    annual = fact(
        "annual",
        "taxes.assessment",
        {**lower["data"], "method": "annual_settlement", "tax_period_end": "2026-12"},
        "2027-05",
    )
    annual_actual = income_actual(annual, "8000", "10000", "-2000")
    result = app.accounting_apply(request(company_id, annual, annual_actual))
    assert result["completion"] == "complete", result
    assert main(app, company_id, annual)["target"]["values"]["change_fen"] == "0"
    assert remaining(app, company_id, lower, "tax_refund") == "0"
    assert remaining(app, company_id, annual, "tax_refund") == "2000"
    received = tax_cash("annual-received", "2027-05-20", "inflow", [(annual, "tax_refund", "2000")])
    command = request(company_id, received)
    result = app.accounting_apply(command)
    assert result["completion"] == "complete", result
    assert (
        next(row for row in obligations(app, company_id, annual) if row["slot"] == "tax_refund")[
            "amount_fen"
        ]
        == "2000"
    )
    assert remaining(app, company_id, annual, "tax_refund") == "0"
    assert app.accounting_apply(command)["completion"] == "complete"


@pytest.mark.parametrize("assembled", ["2026-04"], indirect=True)
def test_midyear_real_application_does_not_invent_prior_cumulative_income_tax(assembled):
    app, company_id, _, _ = assembled
    current = confirmed_income("income-lower", "2026-06", "8000")
    result = app.accounting_apply(request(company_id, current))
    assert result["completion"] == "not_saved" and result["error"] == "needs_information", result
    assert result["fact_issues"][0]["field"] == "/data/income_tax_carry"
    absent = app.accounting_get(
        {"company_id": company_id, "business_ids": [current["business_id"]], "include": ["facts"]}
    )
    assert absent["businesses"] == []


def test_later_real_overpayment_does_not_undo_frozen_earlier_tax_offset(assembled):
    app, company_id, storage, _ = assembled
    first = confirmed_income("income-first", "2026-03", "10000")
    lower = confirmed_income("income-lower", "2026-06", "8000")
    result = app.accounting_apply(request(company_id, lower, first))
    assert result["completion"] == "complete", result
    with storage.write_unit(company_id) as uow:
        uow.repository("ledger").freeze_heads("2026-06")
    before = netting(app, company_id, lower)
    paid = tax_cash("july-paid", "2026-07-20", "outflow", [(first, "tax", "10000")])
    result = app.accounting_apply(request(company_id, paid))
    assert result["completion"] == "not_saved", result
    excess = fact(
        "tax-overpayment",
        "operations.overpayment",
        {"source": {"business_id": first["business_id"], "slot": "tax"}, "amount_fen": "2000"},
        "2026-07",
        effective_on="2026-07-20",
    )
    result = app.accounting_apply(request(company_id, excess, paid))
    assert result["completion"] == "complete", result
    assert netting(app, company_id, lower) == before
    assert remaining(app, company_id, first) == remaining(app, company_id, lower) == "0"
    assert remaining(app, company_id, excess, "recovery") == "2000"
    assert "party_id" not in obligations(app, company_id, excess)[0]


def test_correcting_current_refund_right_uses_real_overreceipt_without_changing_cash(assembled):
    app, company_id, _, _ = assembled
    first = confirmed_income("income-first", "2026-03", "10000")
    paid = tax_cash("first-paid", "2026-03-31", "outflow", [(first, "tax", "10000")])
    lower = confirmed_income("income-lower", "2026-06", "8000")
    declared = income_actual(lower, "8000", "10000", "-2000")
    received = tax_cash("refund-received", "2026-06-30", "inflow", [(lower, "tax_refund", "2000")])
    result = app.accounting_apply(request(company_id, first, paid, lower, declared, received))
    assert result["completion"] == "complete", result
    corrected = deepcopy(declared)
    corrected["data"].update(prepaid_tax_fen="9000", tax_due_fen="-1000")
    excess = fact(
        "tax-overreceipt",
        "operations.overpayment",
        {
            "source": {"business_id": lower["business_id"], "slot": "tax_refund"},
            "amount_fen": "1000",
        },
        "2026-06",
        effective_on="2026-06-30",
    )
    result = app.accounting_apply(request(company_id, excess, mutations=[replace(corrected)]))
    assert result["completion"] == "complete", result
    assert remaining(app, company_id, lower) == "1000"
    assert remaining(app, company_id, lower, "tax_refund") == "0"
    refund = obligations(app, company_id, excess)[0]
    assert refund["direction"] == "payable" and refund["amount_fen"] == "1000"
    assert "party_id" not in refund
    assert get(app, company_id, received["business_id"])["fact"] == received
