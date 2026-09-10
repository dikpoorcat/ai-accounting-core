"""Revenue, actual receipt, VAT point and advance fulfillment remain distinct facts."""

import pytest
from test_business_domains import surtax_policy, vat_policy
from test_tax_credits import Company

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains import taxes, transactions
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods


@pytest.fixture
def company(tmp_path):
    result = Company(tmp_path / "service.sqlite")
    result.save(
        taxes.VatPolicyFact, "vat", period="2026-01", policy=vat_policy().model_dump(mode="json")
    )
    result.save(
        taxes.SurtaxPolicyFact,
        "surtax",
        period="2026-01",
        policy=surtax_policy().model_dump(mode="json"),
    )
    return result


def sale(company, *, period="2026-03", tax_month="2026-04", **changes):
    company.save(
        transactions.ServiceSale,
        "sale",
        **(
            dict(
                period=period,
                customer_id="customer",
                gross_fen=149400,
                vat_policy_id="vat",
                exemption_eligible=False,
                tax_obligation_period=tax_month,
            )
            | changes
        ),
    )
    company.publish("sale")
    return company.current("service_sale", "sale")


def receipt(company, subject, amount, day, *, source_kind="service_sale", source="sale"):
    company.save(
        transactions.Payment,
        subject,
        period=day[:7],
        actual_date=day,
        direction="inflow",
        bank_account_id="bank",
        counterparty_id="customer",
        amount_fen=amount,
        allocations=[
            dict(source_kind=source_kind, source_id=source, obligation="primary", amount_fen=amount)
        ],
    )


def lines(company, kind, subject):
    calculation = company.current(kind, subject)
    with company.engine.store.connection(read_only=True) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT l.account,l.debit,l.credit FROM voucher_line l "
                "JOIN voucher_version v ON v.id=l.version_id WHERE v.calculation_id=?",
                (calculation.id,),
            )
        ]


def test_revenue_month_needs_no_invented_fulfillment_day(company):
    result = sale(company)
    assert result.values["recognized_revenue_fen"] == 147921
    assert result.values["net_sales_fen"] == result.values["accrued_vat_fen"] == 0
    assert result.values["tax_obligation_date"] is None
    assert lines(company, "service_sale", "sale") == [
        ("1122", 149400, 0),
        ("5001", 0, 147921),
        ("222104", 0, 1479),
    ]


def test_first_partial_receipt_transfers_whole_source_vat_once_in_same_voucher(company):
    sale(company, tax_obligation_date="2026-04-02")
    receipt(company, "first", 40000, "2026-04-02")
    company.publish("first")
    assert lines(company, "payment", "first") == [
        ("1002", 40000, 0),
        ("1122", 0, 40000),
        ("222104", 1479, 0),
        ("222101", 0, 1479),
    ]
    receipt(company, "second", 109400, "2026-05-10")
    company.publish("second")
    assert lines(company, "payment", "second") == [("1002", 109400, 0), ("1122", 0, 109400)]
    company.save(
        transactions.ServiceTaxPoint,
        "receipt-proof",
        period="2026-04",
        sale_id="sale",
        trigger="receipt",
        payment_id="first",
    )
    company.publish("receipt-proof")
    assert lines(company, "service_tax_point", "receipt-proof") == []
    company.assess("march", "2026-03")
    company.save(
        taxes.TaxAssessment,
        "april",
        period="2026-04",
        period_start="2026-04-01",
        period_end="2026-04-30",
        vat_policy_id="vat",
        surtax_policy_id="surtax",
    )
    company.publish("march", "april")
    assert company.current("tax_assessment", "march").values["payable_vat_fen"] == 0
    assert company.current("tax_assessment", "april").values["payable_vat_fen"] == 1479
    assert company.current("tax_assessment", "april").values["net_sales_fen"] == 147921


@pytest.mark.parametrize("day", ["2026-03-31", "2026-04-01", "2026-05-02"])
def test_actual_receipt_cannot_infer_a_different_declared_tax_point(company, day):
    sale(company, tax_obligation_date="2026-04-02")
    receipt(company, "wrong", 40000, day)
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["wrong"])
    assert error.value.issues[0]["field"] == "tax_obligation_period"
    assert company.balance("bank") == 0


def test_declared_tax_point_then_receipt_does_not_repeat_vat(company):
    sale(company)
    company.save(
        transactions.ServiceTaxPoint,
        "tax-point",
        period="2026-04",
        sale_id="sale",
        trigger="declared",
        declaration_confirmed=True,
    )
    company.publish("tax-point")
    assert lines(company, "service_tax_point", "tax-point") == [
        ("222104", 1479, 0),
        ("222101", 0, 1479),
    ]
    receipt(company, "paid", 149400, "2026-05-10")
    company.publish("paid")
    assert len(lines(company, "payment", "paid")) == 2
    company.save(
        transactions.ServiceTaxPoint,
        "duplicate",
        period="2026-04",
        sale_id="sale",
        trigger="declared",
        declaration_confirmed=True,
    )
    with pytest.raises(KernelError, match="重复|一次"):
        company.engine.preview(["duplicate"])


def test_same_month_tax_point_posts_directly_and_payment_does_not_repeat(company):
    sale(company, tax_month="2026-03")
    assert lines(company, "service_sale", "sale")[-1] == ("222101", 0, 1479)
    receipt(company, "paid", 149400, "2026-03-12")
    company.publish("paid")
    assert len(lines(company, "payment", "paid")) == 2


def test_missing_tax_month_and_partial_month_policy_need_actual_facts(company):
    company.save(
        transactions.ServiceSale,
        "missing",
        period="2026-03",
        customer_id="customer",
        gross_fen=101000,
        vat_policy_id="vat",
        exemption_eligible=False,
    )
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["missing"])
    assert error.value.issues[0]["field"] == "tax_obligation_period"
    company.save(
        taxes.VatPolicyFact,
        "partial-policy",
        period="2026-03",
        policy=vat_policy().model_dump(mode="json") | {"effective_from": "2026-03-15"},
    )
    company.save(
        transactions.ServiceSale,
        "partial",
        period="2026-03",
        customer_id="customer",
        gross_fen=101000,
        vat_policy_id="partial-policy",
        exemption_eligible=False,
        tax_obligation_period="2026-03",
    )
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["partial"])
    assert error.value.issues[0]["field"] == "tax_obligation_date"


def test_taxed_advance_partial_fulfillment_uses_cumulative_net_without_vat_twice(company):
    company.save(
        taxes.VatPolicyFact,
        "vat-three",
        period="2026-01",
        policy=vat_policy().model_dump(mode="json") | {"rate_percent": "3"},
    )
    company.save(
        transactions.Advance,
        "advance",
        period="2026-01",
        counterparty_id="customer",
        amount_fen=100,
        side="customer",
        contractual_obligation_established=True,
        vat_due_on_advance=True,
        vat_policy_id="vat-three",
        exemption_eligible=False,
        tax_obligation_period="2026-01",
    )
    company.publish("advance")
    receipt(company, "prepaid", 100, "2026-01-05", source_kind="advance", source="advance")
    company.publish("prepaid")
    assert lines(company, "advance", "advance") == [
        ("1122", 100, 0),
        ("2203", 0, 97),
        ("222101", 0, 3),
    ]
    for subject, period in (("first", "2026-02"), ("second", "2026-03")):
        company.save(
            transactions.AdvanceFulfillment,
            subject,
            period=period,
            advance_id="advance",
            fulfilled_gross_fen=50,
        )
        company.publish(subject)
    assert lines(company, "advance_fulfillment", "first") == [("2203", 49, 0), ("5001", 0, 49)]
    assert lines(company, "advance_fulfillment", "second") == [("2203", 48, 0), ("5001", 0, 48)]
    assert company.balance("advance:advance:advance") == 0
    company.save(
        transactions.AdvanceFulfillment,
        "excess",
        period="2026-05",
        advance_id="advance",
        fulfilled_gross_fen=1,
    )
    with pytest.raises(KernelError, match="不能超过"):
        company.engine.preview(["excess"])


def test_untaxed_advance_requires_explicit_fulfillment_tax_point(company):
    company.save(
        transactions.Advance,
        "advance",
        period="2026-01",
        counterparty_id="customer",
        amount_fen=101000,
        side="customer",
        contractual_obligation_established=True,
        vat_due_on_advance=False,
    )
    company.publish("advance")
    company.save(
        transactions.AdvanceFulfillment,
        "fulfilled",
        period="2026-03",
        advance_id="advance",
        fulfilled_gross_fen=101000,
    )
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["fulfilled"])
    assert error.value.issues[0]["field"] == "vat_policy_id"
    company.save(
        transactions.AdvanceFulfillment,
        "fulfilled",
        period="2026-03",
        advance_id="advance",
        fulfilled_gross_fen=101000,
        vat_policy_id="vat",
        exemption_eligible=False,
        tax_obligation_period="2026-03",
    )
    company.publish("fulfilled")
    assert lines(company, "advance_fulfillment", "fulfilled") == [
        ("2203", 101000, 0),
        ("5001", 0, 100000),
        ("222101", 0, 1000),
    ]


def test_return_before_tax_point_reduces_deferred_source_then_receipt_transfers_remainder(company):
    sale(company, period="2026-01", tax_month="2026-03", gross_fen=202000)
    company.save(
        transactions.SaleReturn,
        "returned",
        period="2026-02",
        sale_id="sale",
        returned_gross_fen=101000,
        credit_note_vat_fen=1000,
        customer_id="customer",
    )
    company.publish("returned")
    assert lines(company, "sale_return", "returned")[-1] == ("222104", 1000, 0)
    receipt(company, "paid", 101000, "2026-03-10")
    company.publish("paid")
    assert lines(company, "payment", "paid")[-2:] == [("222104", 1000, 0), ("222101", 0, 1000)]


def test_later_return_does_not_reopen_earlier_closed_return(company):
    sale(company, tax_month="2026-03", gross_fen=202000)
    company.save(
        transactions.SaleReturn,
        "first-return",
        period="2026-03",
        sale_id="sale",
        returned_gross_fen=50500,
        credit_note_vat_fen=500,
        customer_id="customer",
    )
    company.publish("first-return")
    company.assess("march", "2026-03")
    company.publish("march")
    periods = Periods(company.engine)
    for category in MATERIAL_CATEGORIES:
        evidence = (
            [company.filing, company.confirmation] if category in {"transactions", "tax"} else []
        )
        periods.inventory(
            "2026-03",
            category,
            evidence=evidence,
            expected=len(evidence),
            no_business=not evidence,
            confirmation_evidence=company.confirmation,
            request_id=company.request(),
        )
    preview = periods.preview_close("2026-03", owner_confirmation=company.confirmation)
    periods.close(
        "2026-03",
        owner_confirmation=company.confirmation,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )
    closed = periods.closed_report("2026-03")
    first = company.current("sale_return", "first-return")
    company.save(
        transactions.SaleReturn,
        "later-return",
        period="2026-04",
        sale_id="sale",
        returned_gross_fen=50500,
        credit_note_vat_fen=500,
        customer_id="customer",
    )
    company.publish("later-return")
    assert company.current("sale_return", "first-return").id == first.id
    assert periods.closed_report("2026-03") == closed


def test_taxed_advance_fulfillment_then_refund_releases_exact_net_and_tax(company):
    company.save(
        taxes.VatPolicyFact,
        "vat-three",
        period="2026-01",
        policy=vat_policy().model_dump(mode="json") | {"rate_percent": "3"},
    )
    company.save(
        transactions.Advance,
        "advance",
        period="2026-01",
        counterparty_id="customer",
        amount_fen=100,
        side="customer",
        contractual_obligation_established=True,
        vat_due_on_advance=True,
        vat_policy_id="vat-three",
        exemption_eligible=False,
        tax_obligation_period="2026-01",
    )
    company.publish("advance")
    receipt(company, "prepaid", 100, "2026-01-05", source_kind="advance", source="advance")
    company.publish("prepaid")
    company.save(
        transactions.AdvanceFulfillment,
        "fulfilled",
        period="2026-02",
        advance_id="advance",
        fulfilled_gross_fen=50,
    )
    company.publish("fulfilled")
    company.save(
        transactions.AdvanceRefund,
        "refund",
        period="2026-03",
        advance_id="advance",
        refunded_gross_fen=50,
        refund_right_confirmed=True,
    )
    company.publish("refund")
    assert lines(company, "advance_refund", "refund") == [
        ("2203", 0, 50),
        ("2203", 48, 0),
        ("222101", 2, 0),
    ]
    assert company.balance("advance:advance:advance") == 0
    company.save(
        transactions.Payment,
        "paid-back",
        period="2026-03",
        actual_date="2026-03-10",
        direction="outflow",
        bank_account_id="bank",
        counterparty_id="customer",
        amount_fen=50,
        allocations=[
            dict(
                source_kind="advance_refund",
                source_id="refund",
                obligation="primary",
                amount_fen=50,
            )
        ],
    )
    company.publish("paid-back")
    assert company.balance("advance_refund:refund:primary") == 0
    assert company.balance("bank") == 50
    company.save(
        transactions.AdvanceRefund,
        "duplicate",
        period="2026-05",
        advance_id="advance",
        refunded_gross_fen=1,
        refund_right_confirmed=True,
    )
    with pytest.raises(KernelError, match="不能超过"):
        company.engine.preview(["duplicate"])


def test_same_period_full_taxed_advance_refund_nets_tax_source_to_zero(company):
    company.save(
        transactions.Advance,
        "advance",
        period="2026-01",
        counterparty_id="customer",
        amount_fen=101000,
        side="customer",
        contractual_obligation_established=True,
        vat_due_on_advance=True,
        vat_policy_id="vat",
        exemption_eligible=False,
        tax_obligation_period="2026-01",
    )
    company.save(
        transactions.AdvanceRefund,
        "refund",
        period="2026-01",
        advance_id="advance",
        refunded_gross_fen=101000,
        refund_right_confirmed=True,
    )
    company.publish("advance", "refund")
    company.assess("january", "2026-01")
    company.publish("january")
    result = company.current("tax_assessment", "january")
    assert result.values["net_sales_fen"] == result.values["payable_vat_fen"] == 0


def test_cross_period_advance_refund_uses_explicit_filed_tax_credit(company):
    company.save(
        transactions.Advance,
        "advance",
        period="2026-01",
        counterparty_id="customer",
        amount_fen=202000,
        side="customer",
        contractual_obligation_established=True,
        vat_due_on_advance=True,
        vat_policy_id="vat",
        exemption_eligible=False,
        tax_obligation_period="2026-01",
    )
    company.publish("advance")
    company.assess("january", "2026-01")
    company.publish("january")
    company.save(
        transactions.AdvanceRefund,
        "returned",
        period="2026-02",
        advance_id="advance",
        refunded_gross_fen=101000,
        refund_right_confirmed=True,
    )
    company.publish("returned")
    company.save(
        taxes.TaxCreditConfirmation,
        "credit",
        **company.credit_fields(
            returns=[
                dict(
                    return_id="returned",
                    original_advance_id="advance",
                )
            ]
        ),
    )
    company.assess("february", "2026-02", ["credit"])
    company.publish("credit", "february")
    assert company.balance("tax_assessment:february:vat_credit_credit") == 1000
    assert company.balance("tax_assessment:february:surtax_credit_credit") == 60
