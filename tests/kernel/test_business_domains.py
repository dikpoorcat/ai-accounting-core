"""Executable accounting examples for the new pure business modules."""

import json
from datetime import date
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from ai_accounting.kernel.contracts import (
    Calculation,
    Context,
    FactVersion,
    KernelError,
    NeedsInformation,
    Registry,
)
from ai_accounting.kernel.domains import adjustments, assets, taxes, transactions
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import MAX_FEN, YearMonth


def version(model, subject, **fields):
    return FactVersion(subject + "-v1", subject, 1, model.model_validate_json(json.dumps(fields)))


def calculation(source, result):
    return Calculation(
        source.subject_id + "-calculated",
        source.subject_id,
        source.fact.kind,
        source.fact.period,
        result.values,
        source.id,
    )


def context(source, *, facts=(), calculations=()):
    selections = {}
    for read in source.fact.reads():
        candidates = facts if read.source == "fact" else calculations
        chosen = []
        for item in candidates:
            fact = item.fact if read.source == "fact" else item[0].fact
            subject = item.subject_id if read.source == "fact" else item[0].subject_id
            if (read.kind != "*" and fact.kind != read.kind) or read.key not in (
                *fact.scopes(),
                "@" + subject,
                *(claim.key for claim in fact.claims()),
            ):
                continue
            if read.before_period is not None and fact.period >= read.before_period:
                continue
            chosen.append(item if read.source == "fact" else item[1])
        selections[read] = tuple(chosen)
    return Context(selections)


def vat_policy(**changes):
    return taxes.VatPolicy(
        version="test-v1",
        source_url="https://www.chinatax.gov.cn/test-policy",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
        rate_percent="1",
        threshold_fen=1000000,
        threshold_operator="at_or_below",
        **changes,
    )


def surtax_policy():
    return taxes.SurtaxPolicy(
        version="test-surtax-v1",
        source_url="https://www.chinatax.gov.cn/test-surtax",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
        urban_rate_percent="7",
        education_rate_percent="3",
        local_education_rate_percent="2",
        payable_fraction="0.5",
    )


@pytest.mark.parametrize("amount", [True, 100.0, "100", MAX_FEN + 1])
def test_money_does_not_coerce(amount):
    with pytest.raises(ValidationError):
        version(
            transactions.Expense,
            "expense",
            period="2026-09",
            counterparty_id="supplier",
            amount_fen=amount,
            expense_class="administration",
            creditor_kind="supplier",
        )


def test_tax_is_independent_of_global_decimal_context():
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        assert taxes.split_tax_inclusive(101_000, Decimal("1")) == (100_000, 1_000)
        result = taxes.calculate_vat_period(
            sales=(
                taxes.VatSales(
                    net_sales_fen=100000, accrued_vat_fen=1000, exemption_eligible=False
                ),
            ),
            start=date(2026, 9, 1),
            end=date(2026, 9, 30),
            vat_policy=vat_policy(),
            surtax_policy=surtax_policy(),
        )
    assert result.payable_vat_fen == 1000
    assert (result.urban_tax_fen, result.education_tax_fen, result.local_education_tax_fen) == (
        35,
        15,
        10,
    )


def test_tax_exemption_does_not_exempt_ineligible_sales():
    result = taxes.calculate_vat_period(
        sales=(
            taxes.VatSales(net_sales_fen=100000, accrued_vat_fen=1000, exemption_eligible=True),
            taxes.VatSales(net_sales_fen=100000, accrued_vat_fen=1000, exemption_eligible=False),
        ),
        start=date(2026, 9, 1),
        end=date(2026, 9, 30),
        vat_policy=vat_policy(),
        surtax_policy=surtax_policy(),
    )
    assert result.relief_fen == result.payable_vat_fen == 1000


def test_policy_requires_primary_source_and_effective_interval():
    with pytest.raises(ValidationError):
        taxes.VatPolicy.model_validate(
            vat_policy().model_dump() | {"source_url": "https://blog.example/policy"}
        )
    with pytest.raises(ValueError, match="NOT_EFFECTIVE"):
        vat_policy().require_effective(date(2026, 12, 1), date(2027, 1, 1))
    with pytest.raises(ValidationError):
        taxes.VatPolicy.model_validate(vat_policy().model_dump() | {"rate_percent": 1.0})


def test_missing_business_tax_fact_has_structured_diagnostic():
    sale = version(
        transactions.ServiceSale,
        "sale",
        period="2026-09",
        customer_id="customer",
        gross_fen=10100,
        fulfillment_date="2026-09-15",
    )
    with pytest.raises(NeedsInformation) as error:
        transactions.calculate_sale(sale, context(sale))
    assert error.value.response()["fact_issues"][0]["field"] == "vat_policy_id"


def expense(subject="expense", amount=1000):
    fact = version(
        transactions.Expense,
        subject,
        period="2026-09",
        counterparty_id="supplier",
        amount_fen=amount,
        expense_class="administration",
        creditor_kind="supplier",
    )
    result = transactions.calculate_expense(fact, context(fact))
    return fact, calculation(fact, result)


def payment(subject="payment", amount=1000):
    return version(
        transactions.Payment,
        subject,
        period="2026-09",
        actual_date="2026-09-20",
        direction="outflow",
        bank_account_id="bank-a",
        counterparty_id="supplier",
        amount_fen=amount,
        allocations=[
            {
                "source_kind": "expense",
                "source_id": "expense",
                "obligation": "primary",
                "amount_fen": amount,
            }
        ],
    )


def test_actual_payment_after_accrual_change_requires_explicit_recovery():
    original = expense(amount=1000)
    actual = payment(amount=1000)
    first = transactions.calculate_payment(actual, context(actual, calculations=(original,)))
    assert first.values["amount_fen"] == 1000
    corrected = expense(amount=800)
    with pytest.raises(NeedsInformation):
        transactions.calculate_payment(actual, context(actual, calculations=(corrected,)))
    recovery = version(
        transactions.Overpayment,
        "recovery",
        period="2026-09",
        source_kind="expense",
        source_id="expense",
        obligation_name="primary",
        counterparty_id="supplier",
        amount_fen=200,
        recovery_right_confirmed=True,
    )
    republished = transactions.calculate_payment(
        actual, context(actual, calculations=(corrected,), facts=(recovery,))
    )
    recovered = transactions.calculate_overpayment(
        recovery, context(recovery, calculations=(corrected,), facts=(actual,))
    )
    assert republished.lines == first.lines
    assert republished.values["amount_fen"] == 1000
    assert recovered.values["obligations"][0]["amount_fen"] == 200
    assert [(row.account, row.debit, row.credit) for row in recovered.lines] == [
        ("1221", 200, 0),
        ("2202", 0, 200),
    ]


def test_two_partial_actual_payments_cannot_overallocate():
    source = expense(amount=1000)
    first, second = payment("p1", 600), payment("p2", 500)
    with pytest.raises(NeedsInformation):
        transactions.calculate_payment(
            second, context(second, calculations=(source,), facts=(first,))
        )


def asset_sources(asset_type="fixed", cost=1001, life=3):
    asset = version(
        assets.AssetAcquisition,
        "asset",
        period="2026-06",
        asset_type=asset_type,
        supplier_id="supplier",
        acquisition_date="2026-06-10",
        cost_fen=cost,
        acquisition_basis="direct_purchase",
    )
    acquired = calculation(asset, assets.calculate_acquisition(asset, context(asset)))
    active = version(
        assets.AssetActivation,
        "activation",
        period="2026-06",
        asset_id="asset",
        in_use_date="2026-06-10",
        useful_life_months=life,
        residual_fen=0,
        benefit_area="administration",
        rounding_policy="floor_final_remainder",
    )
    activated = calculation(
        active,
        assets.calculate_activation(
            active, context(active, facts=(asset,), calculations=((asset, acquired),))
        ),
    )
    return asset, acquired, active, activated


@pytest.mark.parametrize(
    "asset_type,first_month", [("fixed", "2026-07"), ("intangible", "2026-06")]
)
def test_asset_life_has_correct_start_and_exact_final_remainder(asset_type, first_month):
    asset, acquired, active, activated = asset_sources(asset_type)
    computations, amounts = [(active, activated)], []
    for offset in range(3):
        month = str(YearMonth.from_ordinal(YearMonth(first_month).ordinal + offset))
        monthly = version(assets.AssetConsumption, "month-" + month, period=month, asset_id="asset")
        result = assets.calculate_consumption(
            monthly, context(monthly, facts=(asset, active), calculations=computations)
        )
        amounts.append(result.values["consumption_fen"])
        computations.append((monthly, calculation(monthly, result)))
    assert amounts == [333, 333, 335]
    assert computations[-1][1].values["carrying_fen"] == 0


def test_asset_consumption_cannot_skip_a_month():
    asset, _, active, activated = asset_sources()
    monthly = version(assets.AssetConsumption, "august", period="2026-08", asset_id="asset")
    with pytest.raises(NeedsInformation):
        assets.calculate_consumption(
            monthly, context(monthly, facts=(asset, active), calculations=((active, activated),))
        )


def test_accounting_domains_publish_to_generated_strict_tables(tmp_path):
    registry = Registry()
    for module in (transactions, assets, taxes):
        module.register(registry)
    store = Store.create(tmp_path / "company.sqlite3", registry, "company", "taxpayer", "instance")
    engine = Engine(store)
    evidence = engine.register_evidence(
        b"test invoice", "text/plain", "invoice", request_id="evidence"
    )["digest"]
    saved = engine.save_fact(
        "expense",
        "expense",
        {
            "period": "2026-09",
            "counterparty_id": "supplier",
            "amount_fen": 1000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        evidence=(evidence,),
        expected_revision=0,
        request_id="expense-fact",
    )
    assert saved["status"] == "confirmed"
    preview = engine.preview(["expense"])
    result = engine.confirm(
        ["expense"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="publish",
    )
    assert result["status"] == "published"
    with store.connection(read_only=True) as connection:
        assert connection.execute("SELECT sum(debit),sum(credit) FROM voucher_line").fetchone()[
            :
        ] == (1000, 1000)
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE balance_key='expense:expense:primary'"
            ).fetchone()[0]
            == 1000
        )


def test_annual_income_tax_result_uses_prior_year_frozen_assessment():
    previous = version(
        taxes.IncomeTaxAssessment,
        "quarter",
        period="2026-12",
        year=2026,
        cumulative_assessed_fen=1000,
        assessment_basis="confirmed_provision",
    )
    posted = calculation(
        previous, taxes.calculate_income_tax_assessment(previous, context(previous))
    )
    annual = version(
        taxes.IncomeTaxAssessment,
        "annual",
        period="2027-05",
        year=2026,
        cumulative_assessed_fen=800,
        assessment_basis="annual_settlement",
    )
    result = taxes.calculate_income_tax_assessment(
        annual, context(annual, calculations=((previous, posted),))
    )
    assert result.values["change_fen"] == -200
    assert result.values["obligations"][0]["amount_fen"] == 200
    assert result.values["obligations"][0]["normal"] == "debit"
    assert [(row.account, row.debit, row.credit) for row in result.lines] == [
        ("222106", 200, 0),
        ("5801", 0, 200),
    ]


def test_project_cost_cannot_be_both_expensed_and_capitalized():
    cost = version(
        transactions.ProjectCost,
        "cost",
        period="2026-06",
        project_id="software",
        supplier_id="supplier",
        amount_fen=1000,
        project_nature="purchased_intangible",
        capitalization_conditions_confirmed=True,
    )
    posted = calculation(cost, transactions.calculate_project_cost(cost, context(cost)))
    prior = version(
        transactions.ProjectRelease,
        "first",
        period="2026-07",
        project_id="software",
        project_sources=[{"source_id": "cost", "amount_fen": 700}],
        expense_class="administration",
    )
    transfer = version(
        assets.AssetAcquisition,
        "asset",
        period="2026-08",
        acquisition_date="2026-08-01",
        cost_fen=400,
        asset_type="intangible",
        acquisition_basis="project_completion",
        project_sources=[{"source_id": "cost", "amount_fen": 400}],
    )
    with pytest.raises(KernelError, match="不得超过"):
        assets.calculate_acquisition(
            transfer, context(transfer, facts=(prior,), calculations=((cost, posted),))
        )
    first = transactions.calculate_project_release(
        prior, context(prior, calculations=((cost, posted),))
    )
    assert first.values["released_fen"] == 700
    assert first.balances[0].amount == -700


def test_prepaid_supplier_cost_offsets_expense_without_new_money():
    advance = version(
        transactions.Advance,
        "prepaid",
        period="2026-09",
        counterparty_id="supplier",
        amount_fen=1000,
        side="supplier",
        contractual_obligation_established=True,
    )
    advance_posted = calculation(advance, transactions.calculate_advance(advance, context(advance)))
    incurred = expense()
    offset = version(
        transactions.Settlement,
        "application",
        period="2026-09",
        settlement_kind="advance_application",
        offset_right_confirmed=True,
        first={
            "source_kind": "advance",
            "source_id": "prepaid",
            "obligation": "advance",
            "amount_fen": 1000,
        },
        second={
            "source_kind": "expense",
            "source_id": "expense",
            "obligation": "primary",
            "amount_fen": 1000,
        },
    )
    result = transactions.calculate_settlement(
        offset, context(offset, calculations=((advance, advance_posted), incurred))
    )
    assert [(row.account, row.debit, row.credit) for row in result.lines] == [
        ("2202", 1000, 0),
        ("1123", 0, 1000),
    ]
    assert [effect.amount for effect in result.balances] == [-1000, -1000]
    actual = payment(amount=1)
    with pytest.raises(KernelError, match="超过"):
        transactions.calculate_payment(
            actual, context(actual, facts=(offset,), calculations=(incurred,))
        )


def test_asset_disposal_after_full_life_uses_frozen_final_consumption():
    asset, _, active, activated = asset_sources(life=1)
    monthly = version(assets.AssetConsumption, "final", period="2026-07", asset_id="asset")
    consumed = assets.calculate_consumption(
        monthly, context(monthly, facts=(asset, active), calculations=((active, activated),))
    )
    disposal = version(
        assets.AssetDisposal,
        "scrap",
        period="2026-09",
        asset_id="asset",
        disposal_date="2026-09-10",
        disposal_kind="scrap",
        gross_proceeds_fen=0,
    )
    result = assets.calculate_disposal(
        disposal,
        context(
            disposal,
            facts=(asset, active),
            calculations=((active, activated), (monthly, calculation(monthly, consumed))),
        ),
    )
    assert result.values["carrying_fen"] == 0
    assert result.values["gain_loss_fen"] == 0
    assert [(row.account, row.debit, row.credit) for row in result.lines] == [
        ("1601", 0, 1001),
        ("1602", 1001, 0),
    ]


def test_card_rounding_does_not_inherit_global_decimal_precision():
    asset, _, active, activated = asset_sources(cost=100001, life=12)
    active = FactVersion(
        active.id,
        active.subject_id,
        active.revision,
        active.fact.model_copy(update={"rounding_policy": "round_half_up_card"}),
    )
    monthly = version(assets.AssetConsumption, "first", period="2026-07", asset_id="asset")
    with localcontext() as global_context:
        global_context.prec = 2
        result = assets.calculate_consumption(
            monthly, context(monthly, facts=(asset, active), calculations=((active, activated),))
        )
    assert result.values["consumption_fen"] == 8333


def test_disposal_cannot_credit_an_asset_that_was_never_formally_activated():
    asset, _, active, _ = asset_sources()
    disposal = version(
        assets.AssetDisposal,
        "disposal",
        period="2026-06",
        asset_id="asset",
        disposal_date="2026-06-30",
        disposal_kind="scrap",
        gross_proceeds_fen=0,
    )
    with pytest.raises(NeedsInformation) as failure:
        assets.calculate_disposal(disposal, context(disposal, facts=(asset, active)))
    assert failure.value.issues[0]["field"] == "asset_activation"


def test_loan_interest_stays_at_actual_principal_until_real_repayment():
    agreement = version(
        assets.LoanAgreement,
        "agreement",
        period="2026-01",
        lender_id="bank",
        lender_is_licensed=True,
        currency="CNY",
        annual_rate_percent="3.65",
        day_count_basis="actual_365",
        maturity_date="2027-01-01",
        loan_term="short_term",
    )
    drawdown = version(
        assets.LoanDrawdown,
        "drawdown",
        period="2026-01",
        agreement_id="agreement",
        principal_fen=1000000,
        actual_date="2026-01-01",
        bank_account_id="company-bank",
    )
    posted = calculation(
        drawdown, assets.calculate_drawdown(drawdown, context(drawdown, facts=(agreement,)))
    )
    interest = version(
        assets.LoanInterest,
        "interest",
        period="2026-01",
        drawdown_id="drawdown",
        agreement_id="agreement",
        period_start="2026-01-01",
        period_end_exclusive="2026-02-01",
    )
    result = assets.calculate_interest(
        interest, context(interest, facts=(agreement, drawdown), calculations=((drawdown, posted),))
    )
    assert result.values["interest_fen"] == 3100
    assert result.values["principal_fen"] == 1000000
    assert result.values["actual_days"] == 31


def test_card_rounding_cannot_activate_a_schedule_with_negative_final_depreciation():
    asset, acquired, active, _ = asset_sources(cost=18, life=12)
    active = FactVersion(
        active.id,
        active.subject_id,
        active.revision,
        active.fact.model_copy(update={"rounding_policy": "round_half_up_card"}),
    )
    with pytest.raises(NeedsInformation) as failure:
        assets.calculate_activation(
            active, context(active, facts=(asset,), calculations=((asset, acquired),))
        )
    assert failure.value.issues[0]["field"] == "rounding_policy"


def loan_sources(principal=1000000):
    agreement = version(
        assets.LoanAgreement,
        "agreement",
        period="2026-01",
        lender_id="bank",
        lender_is_licensed=True,
        currency="CNY",
        annual_rate_percent="3.65",
        day_count_basis="actual_365",
        maturity_date="2027-01-01",
        loan_term="short_term",
    )
    drawdown = version(
        assets.LoanDrawdown,
        "drawdown",
        period="2026-01",
        agreement_id="agreement",
        principal_fen=principal,
        actual_date="2026-01-01",
        bank_account_id="company-bank",
    )
    posted = calculation(
        drawdown, assets.calculate_drawdown(drawdown, context(drawdown, facts=(agreement,)))
    )
    return agreement, drawdown, posted


def principal_repayment(amount=400000, day="2026-01-16", **changes):
    fields = {
        "period": day[:7],
        "actual_date": day,
        "direction": "outflow",
        "bank_account_id": "company-bank",
        "counterparty_id": "bank",
        "amount_fen": amount,
        "allocations": [
            {
                "source_kind": "loan_drawdown",
                "source_id": "drawdown",
                "obligation": "principal",
                "amount_fen": amount,
            }
        ],
    }
    return version(transactions.Payment, "repayment", **(fields | changes))


def test_partial_actual_repayment_changes_only_the_following_interest_interval():
    agreement, drawdown, posted = loan_sources()
    actual = principal_repayment()
    results = []
    for start, end in (("2026-01-01", "2026-01-16"), ("2026-01-16", "2026-02-01")):
        interest = version(
            assets.LoanInterest,
            start,
            period="2026-01",
            drawdown_id="drawdown",
            agreement_id="agreement",
            period_start=start,
            period_end_exclusive=end,
        )
        results.append(
            assets.calculate_interest(
                interest,
                context(
                    interest,
                    facts=(agreement, drawdown, actual),
                    calculations=((drawdown, posted),),
                ),
            )
        )
    assert [result.values["principal_fen"] for result in results] == [1000000, 600000]
    assert [result.values["interest_fen"] for result in results] == [1500, 960]


def test_owner_repayment_to_lender_reduces_interest_without_inventing_company_cash():
    agreement, drawdown, posted = loan_sources()
    paid = version(
        adjustments.EmployeeAdvance,
        "owner-paid",
        period="2026-01",
        payer_id="owner",
        payer_kind="owner",
        payment_on_behalf_confirmed=True,
        actual_creditor_payment_date="2026-01-16",
        sources=[
            {
                "source_kind": "loan_drawdown",
                "source_id": "drawdown",
                "obligation": "principal",
                "amount_fen": 400000,
            }
        ],
    )
    transferred = adjustments.calculate_employee_advance(
        paid, context(paid, calculations=((drawdown, posted),))
    )
    assert {line.account for line in transferred.lines} == {"2001", "2241"}
    interest = version(
        assets.LoanInterest,
        "interest",
        period="2026-01",
        drawdown_id="drawdown",
        agreement_id="agreement",
        period_start="2026-01-16",
        period_end_exclusive="2026-02-01",
    )
    result = assets.calculate_interest(
        interest,
        context(interest, facts=(agreement, drawdown, paid), calculations=((drawdown, posted),)),
    )
    assert result.values["principal_fen"] == 600000
    assert result.values["interest_fen"] == 960


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"direction": "inflow"}, "invalid_principal_repayment"),
        ({"counterparty_id": "other-lender"}, "loan_repayment_party_conflict"),
    ],
)
def test_unvalidated_actual_repayment_cannot_reduce_loan_principal(changes, code):
    agreement, drawdown, posted = loan_sources()
    actual = principal_repayment(**changes)
    interest = version(
        assets.LoanInterest,
        "interest",
        period="2026-01",
        drawdown_id="drawdown",
        agreement_id="agreement",
        period_start="2026-01-16",
        period_end_exclusive="2026-02-01",
    )
    with pytest.raises(KernelError) as failure:
        assets.calculate_interest(
            interest,
            context(
                interest, facts=(agreement, drawdown, actual), calculations=((drawdown, posted),)
            ),
        )
    assert failure.value.code == code


@pytest.mark.parametrize("principal,paid", [(1000000, 1000000), (1, 0)])
def test_paid_off_or_sub_fen_interest_keeps_an_explainable_zero_result(principal, paid):
    agreement, drawdown, posted = loan_sources(principal)
    facts = (agreement, drawdown)
    if paid:
        facts += (principal_repayment(paid, "2026-01-16"),)
    interest = version(
        assets.LoanInterest,
        "interest",
        period="2026-01",
        drawdown_id="drawdown",
        agreement_id="agreement",
        period_start="2026-01-16",
        period_end_exclusive="2026-02-01",
    )
    result = assets.calculate_interest(
        interest, context(interest, facts=facts, calculations=((drawdown, posted),))
    )
    assert result.values["interest_fen"] == 0
    assert result.values["principal_fen"] == principal - paid
    assert result.lines == ()
    assert result.values["obligations"] == []
