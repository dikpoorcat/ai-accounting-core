"""Pure checks of T1's deliberately limited domain projection paths."""

from copy import deepcopy

import pytest

from ai_accounting.kernel.accounting import AccountingBook
from ai_accounting.kernel.contracts import Calculation, FactVersion, KernelError, Read, Registry
from ai_accounting.kernel.domains.accounting import (
    payment_references,
    payroll_references,
    project_payment,
    project_payroll,
    register,
)
from ai_accounting.kernel.domains.assets import AssetActivation
from ai_accounting.kernel.domains.cash import CashPayment
from ai_accounting.kernel.domains.payroll import Payroll, PayrollBounded, PayrollWithholdingActual
from ai_accounting.kernel.domains.payroll_reserve_payment import PayrollReservePayment
from ai_accounting.kernel.domains.platforms import PlatformPayment
from ai_accounting.kernel.domains.transactions import Allocation, Payment
from ai_accounting.kernel.types import YearMonth, digest


class References:
    def __init__(self, *, facts=(), calculations=(), signatures=None):
        self.facts = {item.id: item for item in facts}
        self.calculations = {item.id: item for item in calculations}
        self.signatures = signatures or {}

    def fact(self, pointer, kind):
        result = self.facts[pointer]
        assert result.fact.kind == kind
        return result

    def calculation(self, pointer, kind):
        result = self.calculations[pointer]
        assert result.kind == kind
        return result

    def signature(self, pointer):
        return self.signatures[pointer]


def wage():
    return FactVersion(
        "wage-fact",
        "wage",
        1,
        Payroll(
            period="2026-08",
            employee_id="employee",
            profile_id="profile",
            contribution_policy_id="contribution",
            income_tax_policy_id="income-tax",
            accounting_gross_salary_fen=1000,
            tax_reported_salary_fen=1000,
            tax_exempt_income_fen=0,
            special_additional_deduction_fen=0,
            other_legal_deduction_fen=0,
            tax_relief_fen=0,
            expense_class="management",
            contribution_basis="policy_until_actual",
        ),
    )


def withholding(pointer, evidence, **changes):
    payload = {
        "period": "2026-08",
        "employee_id": "employee",
        "withheld_tax_fen": 100,
        "withholding_confirmed": True,
        "reported_cumulative_standard_deduction_fen": None,
    } | changes
    return FactVersion(pointer, "actual-tax", 1, PayrollWithholdingActual(**payload), evidence)


def wage_outcome(source):
    reported_deduction = source.fact.reported_cumulative_standard_deduction_fen
    actual = {
        "fact_id": source.id,
        "withheld_tax_fen": source.fact.withheld_tax_fen,
        "reported_cumulative_standard_deduction_fen": reported_deduction,
        "difference_from_calculation_fen": 0,
        "evidence": list(source.evidence),
        "cash_payment_recorded": False,
    }
    return {
        "lines": [],
        "balances": [],
        "opening": False,
        "opening_lines": [],
        "values": {
            "employee_id": "employee",
            "tax_fen": 100,
            "actual_withholding": actual,
            "source_versions": ["profile-v1", source.id],
            "rule_versions": ["rule-v1"],
            "tax_state": {"cumulative_withheld_tax_fen": 100},
        },
        "explanation": [
            {"step": "contribution_burden_allocation", "values": {"code": "pension"}},
            {
                "step": "actual_withholding_adopted",
                "values": {
                    "actual_withholding": deepcopy(actual),
                    "calculated_tax_fen": 100,
                },
            },
        ],
    }


def test_actual_evidence_revision_has_equal_projection_with_exact_frozen_references():
    old = withholding("actual-v1", ("a" * 64,))
    new = withholding("actual-v2", ("a" * 64, "b" * 64))
    results = []
    for source in (old, new):
        outcome = wage_outcome(source)
        assert payroll_references(wage(), outcome) == (
            Read("fact", "payroll_withholding_actual", "#" + source.id),
        )
        results.append(project_payroll(wage(), outcome, References(facts=(source,))))
    assert results[0] == results[1]
    assert results[0]["values"]["source_versions"][0] == "profile-v1"
    assert results[0]["values"]["rule_versions"] == ["rule-v1"]


@pytest.mark.parametrize(
    "change",
    [
        lambda o: o["values"].update(new_state="different"),
        lambda o: o["values"]["actual_withholding"].update(new_field="different"),
        lambda o: o["values"]["source_versions"].__setitem__(0, "profile-v2"),
        lambda o: o["values"]["rule_versions"].__setitem__(0, "rule-v2"),
        lambda o: o["values"]["tax_state"].update(cumulative_withheld_tax_fen=200),
        lambda o: o["explanation"][0]["values"].update(code="medical"),
        lambda o: o["explanation"][1]["values"].update(new_field="different"),
    ],
)
def test_unapproved_payroll_paths_remain_significant(change):
    source = withholding("actual-v1", ("a" * 64,))
    original = wage_outcome(source)
    changed = deepcopy(original)
    change(changed)
    refs = References(facts=(source,))
    assert digest(project_payroll(wage(), original, refs)) != digest(
        project_payroll(wage(), changed, refs)
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda o: o["values"]["actual_withholding"].update(withheld_tax_fen=999),
        lambda o: o["values"]["actual_withholding"].update(evidence=["b" * 64]),
        lambda o: o["values"]["source_versions"].append("actual-v1"),
        lambda o: o["explanation"].pop(),
        lambda o: o["explanation"][1]["values"]["actual_withholding"].update(fact_id="other"),
    ],
)
def test_inconsistent_frozen_withholding_requires_compatibility(change):
    source = withholding("actual-v1", ("a" * 64,))
    outcome = wage_outcome(source)
    change(outcome)
    with pytest.raises(KernelError) as error:
        project_payroll(wage(), outcome, References(facts=(source,)))
    assert error.value.code == "accounting_compatibility_required"


@pytest.mark.parametrize(
    "changes",
    [
        {"employee_id": "other"},
        {"period": "2026-07"},
    ],
)
def test_frozen_withholding_must_match_wage_identity(changes):
    source = withholding("actual-v1", ("a" * 64,), **changes)
    with pytest.raises(KernelError) as error:
        project_payroll(wage(), wage_outcome(source), References(facts=(source,)))
    assert error.value.details["reason"] == "actual_withholding_identity_mismatch"


def payment(kind="payment"):
    payload = {
        "period": "2026-08",
        "actual_date": "2026-08-31",
        "direction": "outflow",
        "counterparty_id": "employee",
        "amount_fen": 900,
        "allocations": (
            Allocation(
                source_kind="payroll",
                source_id="wage",
                obligation="net",
                amount_fen=900,
                recipient_id="employee",
            ),
        ),
    }
    if kind == "payment":
        fact = Payment(**payload, bank_account_id="bank")
    elif kind == "cash_payment":
        fact = CashPayment(**payload, cash_account_id="cash")
    elif kind == "platform_payment":
        fact = PlatformPayment(**payload, platform_account_id="platform", movement_ids=("row",))
    else:
        payload.update(counterparty_id="payroll-group", amount_fen=1000)
        payload["allocations"] = tuple(a.model_dump() for a in payload["allocations"])
        fact = PayrollReservePayment(
            **payload,
            bank_account_id="bank",
            scope_id="reserve",
            platform_account_id="platform",
            reserve_return_fen=100,
            return_period="2026-08",
            return_confirmed=True,
            complete_group_confirmed=True,
        )
    return FactVersion("payment-fact", "payment", 1, fact)


def wage_calculation(pointer, **changes):
    payload = {
        "id": pointer,
        "subject_id": "wage",
        "kind": "payroll",
        "period": YearMonth("2026-08"),
        "fact_id": "wage-fact",
        "values": {
            "employee_id": "employee",
            "obligations": [
                {
                    "name": "net",
                    "key": "payroll:wage:net",
                    "counterparty_id": "employee",
                    "amount_fen": 900,
                    "account": "221101",
                    "normal": "credit",
                    "category": "payable",
                }
            ],
        },
    } | changes
    return Calculation(**payload)


def payment_outcome(pointer):
    return {
        "values": {
            "settlements": [
                {
                    "source_calculation": pointer,
                    "obligation": "payroll:wage:net",
                    "amount_fen": 900,
                    "unknown_field": "retained",
                }
            ],
            "tax_transfers": [{"source_calculation": "unrelated-tax-pointer"}],
        }
    }


@pytest.mark.parametrize(
    "kind",
    [
        "payment",
        "cash_payment",
        "platform_payment",
        "payroll_reserve_payment",
    ],
)
def test_payment_channels_normalize_only_verified_wage_pointer(kind):
    version = payment(kind)
    results = []
    for pointer in ("wage-result-v1", "wage-result-v2"):
        outcome = payment_outcome(pointer)
        assert payment_references(version, outcome) == (
            Read("calculation", "payroll", "#" + pointer),
        )
        refs = References(
            calculations=(wage_calculation(pointer),),
            signatures={
                pointer: {"contract": "v1", "digest": "same-accounting-result"},
            },
        )
        results.append(project_payment(version, outcome, refs))
    assert results[0] == results[1]
    assert results[0]["values"]["tax_transfers"][0]["source_calculation"] == "unrelated-tax-pointer"
    assert results[0]["values"]["settlements"][0]["unknown_field"] == "retained"


def test_payment_cannot_hide_changed_source_accounting_signature():
    results = []
    for signature in ("original", "different"):
        refs = References(
            calculations=(wage_calculation("result"),),
            signatures={
                "result": {"contract": "v1", "digest": signature},
            },
        )
        results.append(project_payment(payment(), payment_outcome("result"), refs))
    assert results[0] != results[1]


@pytest.mark.parametrize(
    "changes",
    [
        {"subject_id": "other-wage"},
        {"period": YearMonth("2026-09")},
    ],
)
def test_payment_rejects_unrelated_or_future_frozen_wage(changes):
    refs = References(calculations=(wage_calculation("result", **changes),))
    with pytest.raises(KernelError) as error:
        project_payment(payment(), payment_outcome("result"), refs)
    assert error.value.details["reason"] == "payroll_settlement_source_identity_mismatch"


def test_payment_rejects_allocation_recipient_mismatch():
    version = payment()
    changed = FactVersion(
        version.id,
        version.subject_id,
        version.revision,
        version.fact.model_copy(update={"counterparty_id": "other"}),
    )
    refs = References(calculations=(wage_calculation("result"),))
    with pytest.raises(KernelError) as error:
        project_payment(changed, payment_outcome("result"), refs)
    assert error.value.details["reason"] == "payroll_settlement_recipient_mismatch"


def accounting_book():
    registry = Registry()
    register(registry)

    def forbidden_evaluator(*args):
        pytest.fail("frozen comparison must not reevaluate a business calculator")

    registry.evaluators.update(
        {kind: forbidden_evaluator for kind in registry.accounting_projectors}
    )
    return AccountingBook(registry)


def add_wage(book, pointer, source, *, kind="payroll", record_dependency=True):
    version = wage()
    if kind == "payroll_bounded":
        version = FactVersion(
            version.id,
            version.subject_id,
            version.revision,
            PayrollBounded(**version.fact.model_dump()),
        )
    outcome = wage_outcome(source)
    calculation = Calculation(
        pointer,
        version.subject_id,
        kind,
        version.fact.period,
        outcome["values"],
        version.id,
        digest(outcome).hex(),
    )
    book.facts[source.id] = source
    book.add(calculation, version, outcome, fact_ids=(source.id,) if record_dependency else ())
    return outcome


@pytest.mark.parametrize("kind", ["payroll", "payroll_bounded"])
def test_book_compares_frozen_legacy_results_without_evaluation_or_rewriting(kind):
    book = accounting_book()
    old = withholding("old", ("a" * 64,))
    new = withholding("new", ("a" * 64, "b" * 64))
    before = []
    for pointer, source in (("old-result", old), ("new-result", new)):
        outcome = add_wage(book, pointer, source, kind=kind)
        before.append(deepcopy(outcome))
    assert book.signature("old-result") == book.signature("new-result")
    for pointer, outcome in zip(("old-result", "new-result"), before, strict=True):
        assert digest(book.records[pointer].outcome) == digest(outcome)
        assert book.records[pointer].calculation.result_digest == digest(outcome).hex()
        assert "accounting" not in book.records[pointer].outcome


def test_book_requires_actual_source_to_be_a_recorded_dependency():
    book = accounting_book()
    source = withholding("actual", ("a" * 64,))
    add_wage(book, "result", source, record_dependency=False)
    with pytest.raises(KernelError) as error:
        book.signature("result")
    assert error.value.details["reason"] == "unrecorded_fact_dependency"


def test_book_rejects_unsupported_comparison_contract():
    book = accounting_book()
    with pytest.raises(KernelError) as error:
        book.signature("result", contract="unsupported-v999")
    assert error.value.details["reason"] == "unsupported_comparison_contract"


def test_program_build_change_does_not_change_frozen_result_or_signature(monkeypatch):
    from ai_accounting.kernel import engine

    source = withholding("actual", ("a" * 64,))
    signatures, result_digests = [], []
    for program in ("old-program", "new-program"):
        monkeypatch.setattr(engine, "PROGRAM_VERSION", program)
        book = accounting_book()
        outcome = add_wage(book, "result", source)
        signatures.append(book.signature("result"))
        result_digests.append(digest(outcome))
    assert signatures[0] == signatures[1]
    assert result_digests[0] == result_digests[1]


def test_future_asset_state_is_significant_even_when_outcome_has_not_changed():
    book = accounting_book()
    outcomes = []
    for months in (12, 24):
        fact = AssetActivation(
            period="2026-08",
            asset_id="asset",
            in_use_date="2026-08-01",
            useful_life_months=months,
            residual_fen=0,
            benefit_area="administration",
            rounding_policy="floor_final_remainder",
        )
        version = FactVersion(f"activation-fact-{months}", "activation", 1, fact)
        outcome = {"lines": [], "balances": [], "values": {"asset_id": "asset"}}
        calculation = Calculation(
            f"activation-result-{months}",
            "activation",
            fact.kind,
            fact.period,
            outcome["values"],
            version.id,
        )
        book.add(calculation, version, outcome)
        outcomes.append(outcome)
    assert outcomes[0] == outcomes[1]
    assert book.signature("activation-result-12") != book.signature("activation-result-24")
