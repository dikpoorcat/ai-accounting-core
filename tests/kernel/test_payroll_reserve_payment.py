"""Whole gross batches settle net wages without inventing return dates or debts."""

import pytest
from pydantic import ValidationError
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company

from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains.banking import (
    BankEntry,
    BankOpening,
    BankReconciliation,
    BankStatement,
    Match,
)
from ai_accounting.kernel.domains.managed_reserve import ManagedReserveScope, ReserveCost
from ai_accounting.kernel.domains.payroll_reserve_payment import (
    PayrollNetAllocation,
    PayrollReservePayment,
)
from ai_accounting.kernel.domains.transactions import Allocation, Payment


@pytest.fixture
def company(tmp_path):
    company = Company(tmp_path / "whole-wage-batch.sqlite")
    company.save(contribution_policy(), "contributions")
    company.save(income_tax_policy(), "income-tax")
    for person in ("one", "two"):
        company.save(profile(employee_id=person, effective_to="2026-01"), "profile-" + person)
        company.save(opening(employee_id=person), "opening-" + person)
        company.save(payroll(employee_id=person, profile_id="profile-" + person), "wage-" + person)
    company.publish("wage-one", "wage-two")
    return company


def batch(company, **changes):
    rows = [company.current("wage-" + person) for person in ("one", "two")]
    gross = sum(row.values["gross_fen"] for row in rows)
    net = sum(row.values["net_fen"] for row in rows)
    return PayrollReservePayment(
        **(
            dict(
                period="2026-02",
                actual_date="2026-02-10",
                bank_account_id="bank",
                amount_fen=gross,
                scope_id="scope",
                platform_account_id="pocket",
                reserve_return_fen=gross - net,
                return_period="2026-02",
                actual_return_date=None,
                return_confirmed=True,
                complete_group_confirmed=True,
                allocations=tuple(
                    PayrollNetAllocation(
                        source_kind="payroll",
                        source_id="wage-" + person,
                        recipient_id=person,
                        amount_fen=row.values["net_fen"],
                    )
                    for person, row in zip(("one", "two"), rows, strict=True)
                ),
            )
            | changes
        )
    )


def prepare(company, fact=None, *, sid="gross-batch", scope_changes=None):
    fact = fact or batch(company)
    company.save(fact, sid)
    data = dict(
        period="2026-02",
        platform_account_ids=("pocket",),
        effective_from="2026-02",
        effective_through="2026-02",
        treatment_confirmed=True,
        cost_sources=(ReserveCost(source_kind=fact.kind, source_id=sid),),
    )
    company.save(ManagedReserveScope(**(data | (scope_changes or {}))), "scope")
    return fact


def balances(company):
    with company.engine.store.connection(read_only=True) as connection:
        return {
            row[0]: row[1] for row in connection.execute("SELECT balance_key,amount FROM balance")
        }


def test_real_batch_single_bank_projection_no_new_debt_or_return_date_and_rebuild(company):
    fact = prepare(company)
    before = balances(company)
    preview = company.engine.preview(["scope", "gross-batch"])
    options = dict(
        preview_digest=preview["digest"], epochs=preview["epochs"], request_id=company.request()
    )
    result = company.engine.confirm(["scope", "gross-batch"], **options)
    assert company.engine.confirm(["scope", "gross-batch"], **options) == result
    current = company.current("gross-batch", fact.kind)
    assert current.values["actual_return_date"] is None
    assert current.values["return_period"] == "2026-02"
    assert current.values["managed_reserve_cost_fen"] == fact.reserve_return_fen
    assert current.values["net_settled_fen"] + fact.reserve_return_fen == fact.amount_fen
    assert len(current.values["settlements"]) == 2
    after = balances(company)
    assert after["bank"] == -fact.amount_fen
    assert all(after.get("payroll:wage-" + p + ":net", 0) == 0 for p in ("one", "two"))
    assert all(after[k] == amount for k, amount in before.items() if not k.endswith(":net"))
    assert "pocket" not in after
    with company.engine.store.connection(read_only=True) as connection:
        lines = list(
            connection.execute(
                "SELECT l.account,l.debit,l.credit,l.cashflow FROM voucher_line l "
                "JOIN voucher_version v ON v.id=l.version_id WHERE v.calculation_id=?",
                (current.id,),
            )
        )
    assert sum(row[2] for row in lines if row[0] == "1002") == fact.amount_fen
    assert sum(row[1] for row in lines if row[0] == "5602") == fact.reserve_return_fen
    assert not any(row[0] in {"1221", "224101", "2241"} for row in lines)
    assert sum(row[2] for row in lines if row[3] == "payroll") == current.values["net_settled_fen"]
    company.engine.rebuild_projections(request_id=company.request())
    assert balances(company) == after


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"return_period": None}, "return_period"),
        ({"return_period": "2026-03"}, "return_period"),
        ({"return_confirmed": None}, "return_confirmed"),
        ({"return_confirmed": False}, "return_confirmed"),
        ({"complete_group_confirmed": False}, "complete_group_confirmed"),
    ],
)
def test_missing_or_unsupported_boundaries_fail_atomically(company, changes, field):
    prepare(company, batch(company, **changes))
    before = company.count("calculation"), company.count("voucher_version"), balances(company)
    with pytest.raises(NeedsInformation) as caught:
        company.engine.preview(["scope", "gross-batch"])
    assert caught.value.issues[0]["field"] == field
    assert (
        company.count("calculation"),
        company.count("voucher_version"),
        balances(company),
    ) == before


@pytest.mark.parametrize("day", ["2026-02-09", "2026-03-01"])
def test_known_return_day_cannot_precede_bank_or_cross_month(company, day):
    prepare(company, batch(company, actual_return_date=day))
    with pytest.raises(KernelError, match="返款实际日"):
        company.engine.preview(["scope", "gross-batch"])


def ordinary_payment(company, amount, *, publish=True):
    company.save(
        Payment(
            period="2026-02",
            actual_date="2026-02-05",
            direction="outflow",
            bank_account_id="bank",
            counterparty_id="one",
            amount_fen=amount,
            allocations=(
                Allocation(
                    source_kind="payroll", source_id="wage-one", obligation="net", amount_fen=amount
                ),
            ),
        ),
        "other-payment",
    )
    if publish:
        company.publish("other-payment")


@pytest.mark.parametrize("published", [True, False])
def test_prior_payment_or_confirmed_unpublished_payment_cannot_be_hidden(company, published):
    ordinary_payment(company, 100, publish=published)
    prepare(company)
    with pytest.raises(NeedsInformation):
        company.engine.preview(["scope", "gross-batch"])


def test_later_ordinary_payment_cannot_reuse_settled_net(company):
    prepare(company)
    company.publish("scope", "gross-batch")
    ordinary_payment(company, 100, publish=False)
    with pytest.raises(KernelError):
        company.engine.preview(["other-payment"])
    assert balances(company).get("payroll:wage-one:net", 0) == 0


def test_wage_amendment_expiry_and_current_net_must_still_match(company):
    prepare(company)
    preview = company.engine.preview(["scope", "gross-batch"])
    company.save(
        payroll(employee_id="one", profile_id="profile-one", accounting_gross_salary_fen=1100000),
        "wage-one",
        revision=1,
    )
    with pytest.raises(KernelError):
        company.engine.confirm(
            ["scope", "gross-batch"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=company.request(),
        )
    with pytest.raises(NeedsInformation):
        company.engine.preview(["scope", "gross-batch", "wage-one"])


def test_public_schema_rejects_arbitrary_obligation_duplicate_and_inexact_total(company):
    fact = batch(company)
    wire = command_models(company.engine.store.registry)
    valid = dict(
        company_id="payroll-company",
        kind=fact.kind,
        subject_id="batch",
        data=fact.model_dump(mode="json"),
        evidence=[company.owner_confirmation],
        expected_revision=0,
        request_id="public",
    )
    validate_command(wire, "save_fact", valid)
    for edits in (
        {"account": "1002"},
        {"amount_fen": fact.amount_fen + 1},
        {"allocations": [fact.allocations[0].model_dump(mode="json")] * 2},
    ):
        with pytest.raises(KernelError):
            validate_command(wire, "save_fact", valid | {"data": valid["data"] | edits})
    with pytest.raises(ValidationError):
        PayrollNetAllocation(
            source_kind="expense",
            source_id="cost",
            obligation="primary",
            recipient_id="one",
            amount_fen=100,
        )


def test_two_real_bank_rows_match_one_whole_source_and_invalid_group_fails(company):
    fact = prepare(company)
    company.publish("scope", "gross-batch")
    company.save(
        BankOpening(period="2026-02", bank_account_id="bank", opening_fen=0, basis="new_account"),
        "bank-opening",
    )
    company.save(
        BankStatement(
            period="2026-02",
            bank_account_id="bank",
            opening_fen=0,
            closing_fen=-fact.amount_fen,
            entries=(
                BankEntry(
                    reference="empty-counterparty", actual_date="2026-02-10", signed_fen=-123456
                ),
                BankEntry(
                    reference="aggregate-row",
                    actual_date="2026-02-10",
                    signed_fen=-(fact.amount_fen - 123456),
                ),
            ),
        ),
        "bank-statement",
    )
    company.publish("bank-opening", "bank-statement")
    reconciliation = BankReconciliation(
        period="2026-02",
        statement_id="bank-statement",
        bank_account_id="bank",
        matches=tuple(
            Match(reference=row, source_kind=fact.kind, source_id="gross-batch")
            for row in ("empty-counterparty", "aggregate-row")
        ),
    )
    company.save(reconciliation, "bank-reconciliation")
    company.publish("bank-reconciliation")
    assert company.current("bank-reconciliation", "bank_reconciliation").values["balanced"] is True
    assert (
        company.current("bank-reconciliation", "bank_reconciliation").values["matched_count"] == 1
    )
    partial = reconciliation.model_copy(update={"matches": reconciliation.matches[:1]})
    company.save(partial, "bank-reconciliation", revision=1)
    with pytest.raises(NeedsInformation):
        company.engine.preview(["bank-reconciliation"])
    company.save(reconciliation, "bank-reconciliation", revision=2)
    company.publish("bank-reconciliation")
    company.close("2026-01")
    closed = company.close("2026-02")
    assert company.current("gross-batch", fact.kind).id in closed["calculations"]


def test_bank_material_uses_full_amount_and_preserves_unsigned_outflow(company):
    fact = batch(company)
    fact.validate_material_amount(
        "result.amount_fen",
        fact.amount_fen,
        source_amounts=(123456, fact.amount_fen - 123456),
        source_directions=("outflow", "outflow"),
    )
    with pytest.raises(KernelError):
        fact.validate_material_amount(
            "result.amount_fen",
            fact.amount_fen,
            source_amounts=(fact.amount_fen,),
            source_directions=("inflow",),
        )
    with pytest.raises(KernelError):
        fact.validate_material_amount(
            "result.managed_reserve_cost_fen",
            fact.reserve_return_fen,
            source_amounts=(fact.reserve_return_fen,),
            source_directions=("outflow",),
        )


def test_wrong_platform_or_unadopted_cost_cannot_use_scope(company):
    prepare(company, batch(company, platform_account_id="other-pocket"))
    with pytest.raises(KernelError) as caught:
        company.engine.preview(["scope", "gross-batch"])
    assert caught.value.code == "reserve_cost_scope"


def test_commit_failure_rolls_back_scope_wages_and_expense_together(company):
    prepare(company)
    preview = company.engine.preview(["scope", "gross-batch"])
    before = balances(company), company.count("calculation"), company.count("voucher_version")

    def fail(point, connection):
        if point == "published":
            raise RuntimeError("synthetic fail before commit")

    company.engine.fault = fail
    options = dict(
        preview_digest=preview["digest"], epochs=preview["epochs"], request_id=company.request()
    )
    with pytest.raises(RuntimeError, match="synthetic fail"):
        company.engine.confirm(["scope", "gross-batch"], **options)
    assert (
        balances(company),
        company.count("calculation"),
        company.count("voucher_version"),
    ) == before
    company.engine.fault = lambda point, connection: None
    company.engine.confirm(["scope", "gross-batch"], **options)
    assert balances(company)["bank"] == -batch(company).amount_fen


def test_group_total_and_recipient_are_checked_against_current_wage_sources(company):
    fact = batch(company)
    altered = fact.model_copy(
        update={
            "amount_fen": fact.amount_fen + 1,
            "reserve_return_fen": fact.reserve_return_fen + 1,
        }
    )
    prepare(company, altered)
    with pytest.raises(NeedsInformation, match="毛额合计"):
        company.engine.preview(["scope", "gross-batch"])


def test_actual_return_date_is_preserved_if_known(company):
    fact = prepare(company, batch(company, actual_return_date="2026-02-18"))
    company.publish("scope", "gross-batch")
    assert company.current("gross-batch", fact.kind).values["actual_return_date"] == "2026-02-18"
