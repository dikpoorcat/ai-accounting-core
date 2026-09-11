"""Real publication chains prove zero withholding without manufacturing deduction facts."""

import pytest
from test_payroll import (
    actual,
    bonus,
    bonus_sources,
    context_for,
    contribution_policy,
    income_tax_policy,
    opening,
    payroll,
    payroll_sources,
    profile,
    version,
)
from test_payroll_corrections import Company

from ai_accounting.kernel.contracts import KernelError, NeedsInformation, Read
from ai_accounting.kernel.domains.payroll import (
    UNKNOWN_DEDUCTIONS,
    PayrollBounded,
    TaxBracketFact,
    calculate_payroll,
)
from ai_accounting.kernel.domains.transactions import Allocation, Payment
from ai_accounting.kernel.payroll_preparation import (
    BoundedPayrollPlan,
    PayrollChangeNotice,
    PayrollNoChange,
    PayrollPreparation,
)
from ai_accounting.kernel.tax_import import TaxImport
from ai_accounting.kernel.types import YearMonth, canonical


def bounded(**changes):
    data = payroll(accounting_gross_salary_fen=500_000, tax_reported_salary_fen=500_000)
    return PayrollBounded(
        **(data.model_dump(mode="json") | dict.fromkeys(UNKNOWN_DEDUCTIONS) | changes)
    )


def company(tmp_path):
    instance = Company(tmp_path / "bounded.sqlite")
    for fact, subject in (
        (profile(effective_to="2026-02"), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
    ):
        instance.save(fact, subject)
    return instance


def test_month_precision_preserves_nulls_and_known_payables(tmp_path):
    instance = company(tmp_path)
    saved = instance.save(bounded(), "january")
    instance.publish("january")
    calc = instance.current("january", "payroll_bounded")
    assert calc.values["tax_fen"] == 0
    assert calc.values["net_fen"] == 420_000
    assert calc.values["tax_state"] is None
    assert calc.values["tax_input"]["income_date"] is None
    assert all(calc.values["tax_input"][field] is None for field in UNKNOWN_DEDUCTIONS)
    bounds = calc.values["tax_state_bounds"]
    assert bounds["lower_bound"]["cumulative_income_fen"] == 500_000
    assert bounds["lower_bound"]["cumulative_withheld_tax_fen"] == 0
    assert {item["fact_id"] for item in bounds["unknown_fields"]} == {saved["fact_id"]}
    assert {item["field"] for item in bounds["unknown_fields"]} == set(UNKNOWN_DEDUCTIONS)
    assert calc.values["obligations"][0]["key"] == "payroll_bounded:january:net"
    exported = TaxImport(instance.engine).preview("2026-01")
    assert exported["status"] == "needs_information"
    assert not exported["rows_fen"]
    assert {item["field"] for item in exported["fact_issues"]} >= set(UNKNOWN_DEDUCTIONS)
    with instance.engine.store.connection(read_only=True) as connection:
        stored = instance.engine.store.current_fact(connection, "january")
    assert stored.fact.tax_income_date is None
    assert stored.fact.special_additional_deduction_fen is None


@pytest.mark.parametrize("next_kind", ["payroll", "payroll_bounded"])
def test_positive_future_tax_cannot_skip_unknown_history_and_failure_is_atomic(tmp_path, next_kind):
    instance = company(tmp_path)
    saved = instance.save(bounded(), "january")
    instance.publish("january")
    february = payroll(period="2026-02", tax_reported_salary_fen=2_000_000)
    if next_kind == "payroll_bounded":
        february = PayrollBounded(**february.model_dump(mode="json"))
    instance.save(february, "february")
    before = instance.count("calculation"), instance.count("voucher_version")
    with pytest.raises(NeedsInformation) as error:
        instance.publish("february")
    assert all(item["fact_id"] == saved["fact_id"] for item in error.value.issues)
    assert before == (instance.count("calculation"), instance.count("voucher_version"))
    instance.save(bounded(**dict.fromkeys(UNKNOWN_DEDUCTIONS, 0)), "january", revision=1)
    instance.publish("january", "february")
    assert instance.current("january", "payroll_bounded").values["tax_state"] is not None
    assert instance.current("february", next_kind).values["tax_fen"] == 40_200


def test_source_correction_rebuilds_only_employee_chain_and_keeps_actual_payment(tmp_path):
    instance = company(tmp_path)
    for fact, subject in (
        (bounded(), "january"),
        (bounded(period="2026-02"), "february"),
        (profile(employee_id="other", effective_to="2026-02"), "other-profile"),
        (opening(employee_id="other"), "other-opening"),
        (bounded(employee_id="other", profile_id="other-profile"), "other-january"),
        (
            Payment(
                period="2026-02",
                amount_fen=420_000,
                actual_date="2026-02-03",
                direction="outflow",
                bank_account_id="bank",
                counterparty_id="employee",
                allocations=(
                    Allocation(
                        source_kind="payroll_bounded",
                        source_id="january",
                        obligation="net",
                        amount_fen=420_000,
                    ),
                ),
            ),
            "payment",
        ),
    ):
        instance.save(fact, subject)
    _, initial = instance.publish("january", "february", "other-january", "payment")
    other = instance.current("other-january", "payroll_bounded")
    with instance.engine.store.connection(read_only=True) as connection:
        actual_payment = instance.engine.store.current_fact(connection, "payment")
    january = instance.save(bounded(**dict.fromkeys(UNKNOWN_DEDUCTIONS, 0)), "january", revision=1)
    assert {"january", "february", "payment"} <= set(january["pending"])
    _, corrected = instance.publish("january")
    february = instance.current("february", "payroll_bounded")
    assert {item["period"] for item in february.values["tax_state_bounds"]["unknown_fields"]} == {
        "2026-02"
    }
    assert other == instance.current("other-january", "payroll_bounded")
    for subject in ("january", "february"):
        assert corrected[subject]["voucher_number"] == initial[subject]["voucher_number"]
    with instance.engine.store.connection(read_only=True) as connection:
        assert actual_payment == instance.engine.store.current_fact(connection, "payment")
    assert instance.current("payment", "payment").values["amount_fen"] == 420_000


def test_closed_month_keeps_frozen_proof_when_deductions_are_later_confirmed(tmp_path):
    instance = company(tmp_path)
    instance.save(bounded(), "january")
    instance.publish("january")
    frozen = instance.close("2026-01")
    ledger = instance.engine.ledger("2026-01")
    old = instance.current("january", "payroll_bounded")
    instance.save(bounded(**dict.fromkeys(UNKNOWN_DEDUCTIONS, 0)), "january", revision=1)
    with pytest.raises(KernelError) as error:
        instance.publish("january")
    assert error.value.code == "closed_correction_required"
    instance.publish("january", correction_period="2026-02")
    from ai_accounting.kernel.periods import Periods

    assert Periods(instance.engine).closed_report("2026-01") == frozen
    assert instance.engine.ledger("2026-01") == ledger
    assert instance.current("january", "payroll_bounded").values["tax_state"] is not None
    assert instance.engine.trace(old.id)["calculation"]["outcome"]["values"]["tax_state"] is None


def test_cross_kind_same_employee_month_is_not_two_wages(tmp_path):
    instance = company(tmp_path)
    instance.save(bounded(), "january")
    instance.publish("january")
    instance.save(payroll(), "duplicate")
    with pytest.raises(KernelError) as error:
        instance.publish("duplicate")
    assert error.value.code == "duplicate_remuneration"


def test_complete_bounded_wage_still_supports_nonzero_tax(tmp_path):
    instance = company(tmp_path)
    instance.save(
        bounded(tax_reported_salary_fen=1_000_000, **dict.fromkeys(UNKNOWN_DEDUCTIONS, 0)),
        "january",
    )
    instance.publish("january")
    values = instance.current("january", "payroll_bounded").values
    assert values["tax_fen"] == 12_600
    assert values["tax_state"]["cumulative_income_fen"] == 1_000_000
    assert "tax_state_bounds" not in values
    assert values["tax_input"]["income_date"] is None


def test_whole_month_policy_is_required_without_an_actual_income_day():
    current = version(bounded())
    sources = payroll_sources()
    policy = income_tax_policy().model_copy(update={"effective_from": "2026-01-15"})
    sources = [item for item in sources if item.subject_id != "income-tax"] + [
        version(policy, "income-tax")
    ]
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(current, context_for(current, sources))
    assert error.value.issues[0]["field"] == "tax_income_date"
    explicit = version(bounded(tax_income_date="2026-01-20"))
    assert calculate_payroll(explicit, context_for(explicit, sources)).values["tax_fen"] == 0


def test_zero_proof_checks_reachable_brackets_not_only_the_highest_income_endpoint():
    current = version(bounded(tax_reported_salary_fen=1_000_000))
    policy = income_tax_policy().model_copy(
        update={
            "brackets": (
                TaxBracketFact(upper_bound_fen=10_000, rate="0.5", quick_deduction_fen=0),
                TaxBracketFact(upper_bound_fen=None, rate="0", quick_deduction_fen=0),
            ),
        }
    )
    sources = [item for item in payroll_sources() if item.subject_id != "income-tax"] + [
        version(policy, "income-tax")
    ]
    with pytest.raises(NeedsInformation):
        calculate_payroll(current, context_for(current, sources))


@pytest.mark.parametrize("kind", ["payroll", "payroll_bounded"])
def test_profile_first_withholding_month_is_not_fabricated_as_an_actual_day(tmp_path, kind):
    instance = company(tmp_path)
    instance.save(profile(withholding_start_date="2026-01"), "profile", revision=1)
    fact = payroll() if kind == "payroll" else bounded()
    instance.save(fact, "january")
    instance.publish("january")
    calc = instance.current("january", kind)
    assert calc.values["tax_input"]["withholding_start_date"] == "2026-01"
    raw = instance.engine.trace(calc.id)["calculation"]["outcome"]
    assert all(
        entry.get("values", {}).get("withholding_start_date", "2026-01") == "2026-01"
        for entry in raw["explanation"]
    )
    with instance.engine.store.connection(read_only=True) as connection:
        saved = instance.engine.store.current_fact(connection, "profile")
    assert saved.fact.withholding_start_date == "2026-01"


def test_annual_bonus_requires_exact_state_from_bounded_wage(tmp_path):
    instance = company(tmp_path)
    instance.save(bounded(), "january")
    for source in bonus_sources():
        if source.subject_id in {"bonus-policy", "bonus-usage"}:
            instance.save(source.fact, source.subject_id)
    instance.save(bonus(regular_payroll_id="january", tax_method="combined"), "bonus")
    with pytest.raises(NeedsInformation) as error:
        instance.publish("january", "bonus")
    assert error.value.issues[0]["field"] in UNKNOWN_DEDUCTIONS
    assert instance.count("voucher") == 0


def test_combined_bonus_reuses_complete_month_precision_payroll_without_inventing_a_day(tmp_path):
    instance = company(tmp_path)
    instance.save(profile(withholding_start_date="2026-01"), "profile", revision=1)
    instance.save(bounded(**dict.fromkeys(UNKNOWN_DEDUCTIONS, 0)), "january")
    for source in bonus_sources():
        if source.subject_id in {"bonus-policy", "bonus-usage"}:
            instance.save(source.fact, source.subject_id)
    instance.save(bonus(regular_payroll_id="january", tax_method="combined"), "bonus")
    instance.publish("january", "bonus")
    assert instance.current("bonus", "annual_bonus").values["tax_fen"] == 87_600
    wage = instance.current("january", "payroll_bounded")
    assert wage.values["tax_input"]["income_date"] is None
    assert wage.values["tax_input"]["withholding_start_date"] == "2026-01"


def test_preparation_preserves_unknown_deductions_but_never_copies_actual_day(tmp_path):
    instance = company(tmp_path)
    instance.save(bounded(tax_income_date="2026-01-20"), "january")
    instance.publish("january")
    service = PayrollPreparation(instance.engine)
    basis = service.reuse_basis("2026-02")
    instance.save(
        PayrollNoChange(
            period="2026-02",
            prior_period="2026-01",
            basis_digest=basis["basis_digest"],
            employee_roster_unchanged=True,
            salary_and_deductions_unchanged=True,
        ),
        "no-change",
    )
    plan = service.prepare("2026-02")
    assert plan["status"] == "ready"
    candidate = plan["candidates"][0]
    assert candidate["kind"] == "payroll_bounded"
    assert candidate["data"]["tax_income_date"] is None
    assert candidate["data"]["other_legal_deduction_fen"] is None
    instance.save(
        PayrollChangeNotice(
            period="2026-02",
            employee_id="employee",
            changed_fields=("tax_relief",),
        ),
        "change",
    )
    assert service.prepare("2026-02")["status"] == "needs_information"
    instance.save(
        BoundedPayrollPlan(
            period="2026-02",
            employee_id="employee",
            payroll=bounded(period="2026-02", tax_relief_fen=0),
        ),
        "plan",
    )
    proposal = service.prepare("2026-02")
    assert proposal["status"] == "ready"
    saved = service.confirm(
        "2026-02", preview_digest=proposal["digest"], request_id=instance.request()
    )
    subject = saved["results"][0]["subject_id"]
    instance.publish(subject)
    assert instance.current(subject, "payroll_bounded").values["tax_fen"] == 0


def test_48_month_real_engine_chain_keeps_uncertainty_with_exact_yearly_resets(tmp_path):
    instance = Company(tmp_path / "48-months.sqlite")
    first = YearMonth("2022-08")
    records = [
        (
            profile(
                period=first,
                effective_from=first,
                effective_to="2026-07",
                withholding_start_date="2022-08-31",
            ),
            "profile",
        ),
        (
            contribution_policy().model_copy(
                update={
                    "period": first,
                    "effective_from": "2022-08-01",
                    "effective_to": "2026-07-31",
                }
            ),
            "contributions",
        ),
        (income_tax_policy().model_copy(update={"period": first}), "income-tax"),
    ]
    for year in range(2022, 2027):
        # These are separately asserted synthetic known-zero opening facts, not
        # an inference that last year's unknown deduction details became known.
        month = first if year == 2022 else YearMonth(f"{year}-01")
        records.append((opening(period=month), f"opening-{year}"))
    for index in range(48):
        month = YearMonth.from_ordinal(first.ordinal + index)
        records.append(
            (
                bounded(
                    period=month,
                    accounting_gross_salary_fen=80_000,
                    tax_reported_salary_fen=0 if index == 0 else 500_000,
                    contribution_basis="actual_required",
                ),
                f"wage-{month}",
            )
        )
        records.append((actual(period=month, employee=80_000, employer=160_000), f"social-{month}"))
    evidence = instance.engine.register_evidence(
        canonical([fact.model_dump(mode="json") for fact, _ in records]).encode(),
        "application/json",
        "synthetic confirmed source records",
        request_id=instance.request(),
    )["digest"]
    instance.engine.save_facts(
        [
            {
                "kind": fact.kind,
                "subject_id": subject,
                "data": fact.model_dump(mode="json"),
                "evidence": [evidence],
                "expected_revision": 0,
            }
            for fact, subject in records
        ],
        request_id=instance.request(),
    )
    subjects = [subject for fact, subject in records if fact.kind == "payroll_bounded"]
    instance.publish(*reversed(subjects))
    for subject in subjects:
        result = instance.current(subject, "payroll_bounded")
        assert result.values["net_fen"] == result.values["tax_fen"] == 0
        assert result.values["tax_state"] is None
        assert result.values["tax_input"]["income_date"] is None
        assert all(
            item["period"][:4] == result.period[:4]
            for item in result.values["tax_state_bounds"]["unknown_fields"]
        )
    final = instance.current(subjects[-1], "payroll_bounded")
    assert final.values["tax_state_bounds"]["lower_bound"]["cumulative_income_fen"] == 3_500_000
    assert len(final.values["tax_state_bounds"]["unknown_fields"]) == 28
    with instance.engine.store.connection(read_only=True) as connection:
        assert (
            len(
                instance.engine.store.select(
                    connection, Read("calculation", "payroll_bounded", "*")
                )
            )
            == 48
        )
