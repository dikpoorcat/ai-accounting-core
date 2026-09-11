"""Explicit filed-tax credits compose with real Engine refunds and offsets."""

import json
from datetime import date

import pytest
from material_fixture import supporting_text
from pydantic import ValidationError
from test_business_domains import surtax_policy, vat_policy

from ai_accounting.kernel.contracts import KernelError, NeedsInformation, Read
from ai_accounting.kernel.domains import banking, taxes, transactions
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store


class Company:
    def __init__(self, path):
        self.engine = Engine(Store.create(path, default_registry(), "tax-test", "taxpayer", "db"))
        self.counter = 0
        self.revisions = {}
        self.filing = self.engine.register_evidence(
            b"Synthetic original filed VAT return",
            "text/plain",
            "filing",
            request_id=self.request(),
        )["digest"]
        self.confirmation = self.engine.register_evidence(
            b"Synthetic authority credit confirmation",
            "text/plain",
            "decision",
            request_id=self.request(),
        )["digest"]
        supporting_text(self.engine, self.confirmation)
        supporting_text(self.engine, self.filing)

    def request(self):
        self.counter += 1
        return f"request-{self.counter}"

    def save(self, model, subject, **fields):
        fact = model.model_validate_json(json.dumps(fields))
        result = self.engine.save_fact(
            fact.kind,
            subject,
            fact.model_dump(mode="json"),
            evidence=(self.filing, self.confirmation),
            expected_revision=self.revisions.get(subject, 0),
            request_id=self.request(),
        )
        self.revisions[subject] = result["revision"]
        return result

    def publish(self, *subjects):
        preview = self.engine.preview(list(subjects))
        self.engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=self.request(),
        )

    def current(self, kind, subject):
        with self.engine.store.connection(read_only=True) as connection:
            return self.engine.store.select(connection, Read("calculation", kind, f"@{subject}"))[0]

    def balance(self, key):
        with self.engine.store.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT amount FROM balance WHERE balance_key=?",
                (key,),
            ).fetchone()
            return row[0] if row else 0

    def sale(self, subject, period, gross=202_000, eligible=False):
        self.save(
            transactions.ServiceSale,
            subject,
            period=period,
            customer_id="customer",
            gross_fen=gross,
            fulfillment_date=f"{period}-10",
            tax_obligation_period=period,
            vat_policy_id="vat",
            exemption_eligible=eligible,
        )
        self.publish(subject)

    def assess(self, subject, period, credit_ids=()):
        self.save(
            taxes.TaxAssessment,
            subject,
            period=period,
            period_start=f"{period}-01",
            period_end=f"{period}-{'28' if period.endswith('02') else '31'}",
            vat_policy_id="vat",
            surtax_policy_id="surtax",
            tax_credit_ids=list(credit_ids),
        )

    def credit_fields(self, **changes):
        return (
            dict(
                period="2026-02",
                original_assessment_id="january",
                original_period_start="2026-01-01",
                original_period_end="2026-01-31",
                returns=[{"return_id": "returned", "original_sale_id": "original-sale"}],
                authority_confirmed=True,
                original_filing_reference="filed-vat-2026-01",
                confirmation_reference="authority-result-001",
                original_filing_evidence=self.filing,
                confirmation_evidence=self.confirmation,
                confirmation_date="2026-02-20",
                disposition="refund_or_offset",
                vat_credit_fen=1000,
                surtax_credit_fen=60,
                exemption_reversal_fen=0,
            )
            | changes
        )

    def close_january(self):
        self.save(
            banking.BankOpening,
            "bank-opening",
            period="2026-01",
            bank_account_id="bank",
            opening_fen=0,
            basis="new_account",
        )
        self.save(
            banking.BankStatement,
            "statement",
            period="2026-01",
            bank_account_id="bank",
            opening_fen=0,
            closing_fen=-2120,
            entries=[
                dict(
                    reference="tax-payment",
                    actual_date="2026-01-31",
                    signed_fen=-2120,
                )
            ],
        )
        self.save(
            banking.BankReconciliation,
            "reconciliation",
            period="2026-01",
            statement_id="statement",
            bank_account_id="bank",
            matches=[
                dict(
                    reference="tax-payment",
                    source_kind="payment",
                    source_id="original-payment",
                )
            ],
        )
        self.publish("bank-opening", "statement", "reconciliation")
        periods = Periods(self.engine)
        for category in MATERIAL_CATEGORIES:
            items = (
                [self.filing, self.confirmation]
                if category in {"bank", "tax", "transactions"}
                else []
            )
            periods.inventory(
                "2026-01",
                category,
                evidence=items,
                expected=len(items),
                no_business=not items,
                confirmation_evidence=self.confirmation,
                request_id=self.request(),
            )
        preview = periods.preview_close("2026-01", owner_confirmation=self.confirmation)
        periods.close(
            "2026-01",
            owner_confirmation=self.confirmation,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=self.request(),
        )
        return periods.closed_report("2026-01")


@pytest.fixture
def company(tmp_path):
    company = Company(tmp_path / "tax.sqlite")
    company.save(
        taxes.VatPolicyFact, "vat", period="2026-01", policy=vat_policy().model_dump(mode="json")
    )
    company.save(
        taxes.SurtaxPolicyFact,
        "surtax",
        period="2026-01",
        policy=surtax_policy().model_dump(mode="json"),
    )
    company.sale("original-sale", "2026-01")
    company.assess("january", "2026-01")
    company.publish("january")
    company.save(
        transactions.Payment,
        "original-payment",
        period="2026-01",
        actual_date="2026-01-31",
        direction="outflow",
        bank_account_id="bank",
        counterparty_id="authority",
        amount_fen=2120,
        allocations=[
            dict(
                source_kind="tax_assessment", source_id="january", obligation="vat", amount_fen=2000
            ),
            dict(
                source_kind="tax_assessment",
                source_id="january",
                obligation="surtax",
                amount_fen=120,
            ),
        ],
    )
    company.publish("original-payment")
    company.save(
        transactions.SaleReturn,
        "returned",
        period="2026-02",
        sale_id="original-sale",
        returned_gross_fen=101_000,
        credit_note_vat_fen=1000,
        customer_id="customer",
    )
    company.publish("returned")
    return company


def test_negative_period_requires_credit_then_refund_preserves_original_filing_and_payment(company):
    closed = company.close_january()
    original = company.current("tax_assessment", "january")
    paid = company.current("payment", "original-payment")
    company.assess("february", "2026-02")
    with pytest.raises(NeedsInformation) as missing:
        company.engine.preview(["february"])
    assert missing.value.issues[0]["field"] == "tax_credit_disposition"
    company.save(taxes.TaxCreditConfirmation, "credit", **company.credit_fields())
    company.assess("february", "2026-02", ["credit"])
    company.publish("credit", "february")
    result = company.current("tax_assessment", "february")
    assert result.values["payable_vat_fen"] == 0
    assert company.balance("tax_assessment:february:vat_credit_credit") == 1000
    assert company.balance("tax_assessment:february:surtax_credit_credit") == 60
    company.save(
        transactions.Payment,
        "refund",
        period="2026-02",
        actual_date="2026-02-25",
        direction="inflow",
        bank_account_id="bank",
        counterparty_id="authority",
        amount_fen=1060,
        allocations=[
            dict(
                source_kind="tax_assessment",
                source_id="february",
                obligation="vat_credit_credit",
                amount_fen=1000,
            ),
            dict(
                source_kind="tax_assessment",
                source_id="february",
                obligation="surtax_credit_credit",
                amount_fen=60,
            ),
        ],
    )
    company.publish("refund")
    assert company.balance("tax_assessment:february:vat_credit_credit") == 0
    assert company.balance("bank") == -1060
    assert company.current("tax_assessment", "january").id == original.id
    assert company.current("payment", "original-payment").id == paid.id
    assert paid.values["amount_fen"] == 2120
    assert Periods(company.engine).closed_report("2026-01") == closed


def test_credit_offsets_current_tax_without_new_money(company):
    company.sale("new-sale", "2026-02")
    company.save(
        taxes.TaxCreditConfirmation, "credit", **company.credit_fields(disposition="offset")
    )
    company.assess("february", "2026-02", ["credit"])
    company.publish("credit", "february")
    assert company.current("tax_assessment", "february").values["net_sales_fen"] == 200_000
    company.save(
        transactions.Settlement,
        "offset",
        period="2026-02",
        settlement_kind="debt_offset",
        offset_right_confirmed=True,
        first=dict(
            source_kind="tax_assessment", source_id="february", obligation="vat", amount_fen=1000
        ),
        second=dict(
            source_kind="tax_assessment",
            source_id="february",
            obligation="vat_credit_credit",
            amount_fen=1000,
        ),
    )
    company.publish("offset")
    assert company.balance("tax_assessment:february:vat") == 1000
    assert company.balance("tax_assessment:february:vat_credit_credit") == 0
    assert company.balance("bank") == -2120
    company.save(
        transactions.Payment,
        "forbidden-refund",
        period="2026-02",
        actual_date="2026-02-25",
        direction="inflow",
        bank_account_id="bank",
        counterparty_id="authority",
        amount_fen=60,
        allocations=[
            dict(
                source_kind="tax_assessment",
                source_id="february",
                obligation="surtax_credit_credit",
                amount_fen=60,
            ),
        ],
    )
    with pytest.raises(KernelError):
        company.engine.preview(["forbidden-refund"])


@pytest.mark.parametrize(
    "field",
    [
        "authority_confirmed",
        "vat_credit_fen",
        "surtax_credit_fen",
        "exemption_reversal_fen",
        "original_filing_evidence",
        "confirmation_evidence",
        "confirmation_date",
        "disposition",
    ],
)
def test_missing_confirmed_credit_fact_is_structured_needs_information(company, field):
    company.save(taxes.TaxCreditConfirmation, "credit", **company.credit_fields(**{field: None}))
    with pytest.raises(NeedsInformation) as missing:
        company.engine.preview(["credit"])
    assert missing.value.issues[0]["field"] == field


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"vat_credit_fen": 900}, "tax_credit_amount_conflict"),
        ({"surtax_credit_fen": 121}, "tax_credit_exceeds_filing"),
        ({"original_period_start": "2026-01-02"}, "tax_credit_original_period"),
    ],
)
def test_credit_must_reconcile_actual_return_and_original_filing(company, changes, code):
    company.save(taxes.TaxCreditConfirmation, "credit", **company.credit_fields(**changes))
    with pytest.raises(KernelError) as error:
        company.engine.preview(["credit"])
    assert error.value.code == code


@pytest.mark.parametrize("amount", [True, 1.0, "1000", -1])
def test_tax_credit_amounts_are_explicit_integer_fen(amount):
    with pytest.raises(ValidationError):
        taxes.TaxCreditConfirmation.model_validate_json(
            json.dumps(
                dict(
                    period="2026-02",
                    original_assessment_id="original",
                    original_period_start="2026-01-01",
                    original_period_end="2026-01-31",
                    returns=[dict(return_id="returned", original_sale_id="sale")],
                    vat_credit_fen=amount,
                )
            )
        )


def test_previously_exempt_return_reverses_relief_without_inventing_a_refund(tmp_path):
    company = Company(tmp_path / "exempt.sqlite")
    company.save(
        taxes.VatPolicyFact, "vat", period="2026-01", policy=vat_policy().model_dump(mode="json")
    )
    company.save(
        taxes.SurtaxPolicyFact,
        "surtax",
        period="2026-01",
        policy=surtax_policy().model_dump(mode="json"),
    )
    company.sale("original-sale", "2026-01", eligible=True)
    company.assess("january", "2026-01")
    company.publish("january")
    company.save(
        transactions.SaleReturn,
        "returned",
        period="2026-02",
        sale_id="original-sale",
        returned_gross_fen=101_000,
        credit_note_vat_fen=1000,
        customer_id="customer",
    )
    company.publish("returned")
    company.save(
        taxes.TaxCreditConfirmation,
        "credit",
        **company.credit_fields(
            vat_credit_fen=0,
            surtax_credit_fen=0,
            exemption_reversal_fen=1000,
        ),
    )
    company.assess("february", "2026-02", ["credit"])
    company.publish("credit", "february")
    assert company.current("tax_assessment", "february").values["obligations"] == ()
    with company.engine.store.connection(read_only=True) as connection:
        lines = connection.execute(
            "SELECT l.account,l.debit,l.credit FROM voucher_line l "
            "JOIN voucher_version v ON v.id=l.version_id WHERE v.calculation_id=?",
            (company.current("tax_assessment", "february").id,),
        ).fetchall()
    assert [tuple(row) for row in lines] == [("6301", 1000, 0), ("222101", 0, 1000)]


@pytest.mark.parametrize("eligible_positive", [False, True])
def test_nonnegative_period_with_incompatible_exemption_return_needs_disposition(eligible_positive):
    with pytest.raises(NeedsInformation) as missing:
        taxes.calculate_vat_period(
            sales=(
                taxes.VatSales(
                    net_sales_fen=300_000,
                    accrued_vat_fen=3000,
                    exemption_eligible=eligible_positive,
                ),
                taxes.VatSales(
                    net_sales_fen=-100_000,
                    accrued_vat_fen=-1000,
                    exemption_eligible=not eligible_positive,
                ),
            ),
            start=date(2026, 2, 1),
            end=date(2026, 2, 28),
            vat_policy=vat_policy(),
            surtax_policy=surtax_policy(),
        )
    assert missing.value.issues[0]["field"] == "tax_credit_disposition"


def test_same_period_return_preserves_eligibility_and_nets_normally():
    result = taxes.calculate_vat_period(
        sales=(
            taxes.VatSales(net_sales_fen=200_000, accrued_vat_fen=2000, exemption_eligible=True),
            taxes.VatSales(net_sales_fen=-100_000, accrued_vat_fen=-1000, exemption_eligible=True),
            taxes.VatSales(net_sales_fen=100_000, accrued_vat_fen=1000, exemption_eligible=False),
        ),
        start=date(2026, 2, 1),
        end=date(2026, 2, 28),
        vat_policy=vat_policy(),
        surtax_policy=surtax_policy(),
    )
    assert result.net_sales_fen == 200_000
    assert result.relief_fen == result.payable_vat_fen == 1000
