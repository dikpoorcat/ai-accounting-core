"""Owner net-pay aggregates retain full wage completeness without history payloads."""

import copy

import pytest
from stage9_book import MixedBook
from stage9_metrics import measure_work
from test_identity_corrections import identity_engine as identity_engine_fixture
from test_integrity_content import damage
from test_opening_continuation import _close_without_current_business
from test_opening_continuation import book as opening_book_fixture
from test_payroll import bonus, bonus_sources, payroll
from test_payroll_corrections import actual
from test_payroll_corrections import company as company_fixture
from test_settlement_late_reviews import prepared, review

from ai_accounting.kernel import dashboard as dashboard_module
from ai_accounting.kernel import settlement_freeze
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard

company = company_fixture
opening_book = opening_book_fixture
identity_engine = identity_engine_fixture


def response(engine, period, **kwargs):
    result = Dashboard(engine).employees(period, **kwargs, employee_filter="all")
    result.pop("generated_at", None)
    result["data"].pop("generated_at", None)
    return result


def full_response(engine, monkeypatch, period, **kwargs):
    with monkeypatch.context() as old:
        old.setattr(settlement_freeze, "frozen_employee_net_summary", lambda *_a, **_k: None)
        return response(engine, period, **kwargs)


@pytest.mark.parametrize("zero", [False, True])
def test_cohorts_preserve_closed_and_late_review_employee_response(tmp_path, monkeypatch, zero):
    company = prepared(tmp_path, zero=zero)
    review(company, monkeypatch, 1)
    review(company, monkeypatch, 2)
    company.save(
        payroll(
            period="2026-02",
            accounting_gross_salary_fen=0 if zero else 1_000_000,
            tax_reported_salary_fen=0 if zero else 1_000_000,
        ),
        "february",
    )
    company.confirm_payroll("february")
    company.publish("february")
    for period in ("2026-01", "2026-02"):
        for kwargs in ({}, {"employee_id": "employee", "section": "employees"}):
            assert response(company.engine, period, **kwargs) == full_response(
                company.engine, monkeypatch, period, **kwargs
            )


@pytest.mark.parametrize("closed_correction", [0, 1, 2])
def test_cohorts_preserve_actual_closed_correction_and_complete_public_totals(
    company, monkeypatch, closed_correction
):
    company.publish("january", "february")
    company.close("2026-01")
    if closed_correction:
        company.save(actual(), "actual")
        company.publish("actual", posting_period="2026-02")
    if closed_correction == 2:
        company.save(actual(employee=110000, employer=220000), "actual", revision=1)
        company.publish("actual", posting_period="2026-02")
    assert response(company.engine, "2026-02") == full_response(
        company.engine, monkeypatch, "2026-02"
    )


@pytest.mark.parametrize("closed", [False, True])
def test_actual_multi_period_identity_correction_keeps_complete_employee_response(
    identity_engine, monkeypatch, closed
):
    from test_identity_corrections import (
        test_payroll_conflict_resolution_recomputes_later_cumulative_state as scenario,
    )

    comparisons = []

    def differences(first, second, path=""):
        if isinstance(first, dict) and isinstance(second, dict):
            return [item for key in first.keys() | second.keys()
                    for item in differences(first.get(key), second.get(key), path + "." + str(key))]
        if isinstance(first, list) and isinstance(second, list) and len(first) == len(second):
            return [item for i, (a, b) in enumerate(zip(first, second, strict=True))
                    for item in differences(a, b, path + f"[{i}]")]
        return [] if first == second else [(path, first, second)]

    class ComparedDashboard:
        def __init__(self, engine):
            self.engine = engine

        def employees(self, period, **kwargs):
            new = Dashboard(self.engine).employees(period, **kwargs, employee_filter="all")
            expected = full_response(self.engine, monkeypatch, period, **kwargs)
            actual = copy.deepcopy(new)
            actual.pop("generated_at", None)
            actual["data"].pop("generated_at", None)
            assert actual == expected, differences(actual, expected)
            comparisons.append((period, kwargs))
            return new

    monkeypatch.setattr(dashboard_module, "Dashboard", ComparedDashboard)
    scenario(identity_engine, closed)
    engine = identity_engine[0]
    compared = ComparedDashboard(engine)
    focused = compared.employees(
        "2026-02", employee_id=identity_engine[3], section="employees", preparation="deferred"
    , employee_filter="all")
    assert len(focused["data"]["collections"]["employees"]["items"]) == 1
    assert len(comparisons) == 2


@pytest.mark.parametrize(
    "account,unknown",
    [
        ("221101", "source"),
        ("221101", "paid"),
        ("221101", "other"),
        ("222103", "source"),
        ("224102", "paid"),
    ],
)
def test_verified_incomplete_cohort_contract_preserves_unknown_and_full_wage_checking(
    tmp_path, monkeypatch, account, unknown
):
    company = prepared(tmp_path)
    original_scope = settlement_freeze._scope
    original_summary = dashboard_module._Snapshot.settlement_summary

    def incomplete_scope(*args, **kwargs):
        # First verify the real root and sources. Inject a legal incomplete
        # projection contract, not a claim that normal writes corrupt sources.
        scope = copy.deepcopy(original_scope(*args, **kwargs))
        group = next(
            group
            for key, group in scope.groups.items()
            if key[2] == account and key[4] == "payroll"
        )
        group[settlement_freeze._G_UNKNOWN_COUNT] = 1
        group[settlement_freeze._FIELDS.index(f"bad_{unknown}_count")] = 1
        return scope

    def incomplete_summary(self, **kwargs):
        summary = copy.deepcopy(original_summary(self, **kwargs))
        item = next(item for item in summary["obligations"] if item["account"] == account)
        item["remaining_fen"] = None
        if unknown in {"paid", "other"}:
            item[f"{unknown}_fen" if unknown == "paid" else "other_settled_fen"] = None
            item["period_paid_fen" if unknown == "paid" else "period_other_settled_fen"] = None
        summary["complete"] = False
        summary["issues"] = [{"field": "settlements", "message": "存在尚未确立的清偿关系"}]
        return summary

    with monkeypatch.context() as narrow:
        narrow.setattr(settlement_freeze, "_scope", incomplete_scope)
        projected = response(company.engine, "2026-01")
    with monkeypatch.context() as legacy:
        legacy.setattr(dashboard_module._Snapshot, "settlement_summary", incomplete_summary)
        expected = full_response(company.engine, monkeypatch, "2026-01")
    assert projected == expected
    assert projected["data"]["employees"]["checking"] is True
    assert projected["data"]["employees"]["outstanding_net_fen"] == (
        None if account == "221101" else 907400
    )


def test_cohort_warm_membership_does_not_enumerate_unrelated_state_cache(tmp_path, monkeypatch):
    company = prepared(tmp_path)

    class ExactOnly(dict):
        def __iter__(self):
            raise AssertionError("unrelated frozen cache must not be enumerated")

        def keys(self):
            raise AssertionError("unrelated frozen cache keys must not be enumerated")

    original = settlement_freeze._scope

    def warm(*args, **kwargs):
        reads = kwargs["reads"]
        if not isinstance(reads._frozen_settlement_states, ExactOnly):
            reads._frozen_settlement_states = ExactOnly(
                {("unrelated", index, "other-digest"): {} for index in range(12000)}
            )
        return original(*args, **kwargs)

    expected = response(company.engine, "2026-01")
    monkeypatch.setattr(settlement_freeze, "_scope", warm)
    assert response(company.engine, "2026-01") == expected


def test_cohort_projection_keeps_fixed_v1_on_the_complete_reader(tmp_path, monkeypatch):
    company = prepared(tmp_path)
    calls = []
    original = dashboard_module._Snapshot.settlement_summary

    def observed(self, **kwargs):
        calls.append(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(dashboard_module._Snapshot, "settlement_summary", observed)
    with historical_content(1):
        assert (
            response(company.engine, "2026-01")["data"]["employees"]["outstanding_net_fen"]
            == 907400
        )
    assert calls and calls[0]["subject_ids"] == {"january"}


def test_cohort_public_read_reduces_history_payload_with_identical_amounts(tmp_path, monkeypatch):
    company = prepared(tmp_path)
    new_work, new = measure_work(company.engine, lambda: response(company.engine, "2026-01"))
    old_work, old = measure_work(
        company.engine, lambda: full_response(company.engine, monkeypatch, "2026-01")
    )
    assert new == old
    assert new_work["counters"]["stdlib_json_loads"] < old_work["counters"]["stdlib_json_loads"]
    assert (
        new_work["counters"]["returned_value_bytes"] < old_work["counters"]["returned_value_bytes"]
    )
    assert (
        new_work["counters"]["adoption_accounting_rows"]
        == old_work["counters"]["adoption_accounting_rows"]
    )


def test_closed_opening_net_and_other_components_keep_exact_employee_scope(
    opening_book, monkeypatch
):
    engine, _save, _publish, package, proof = opening_book
    package(
        [
            ("opening_cash", "cash", {"cash_account_id": "cash", "balance_fen": 200000}),
            (
                "opening_payroll_payable",
                "net",
                {
                    "employee_id": "employee",
                    "recipient_id": "employee",
                    "component": "net",
                    "payroll_period": "2025-12",
                    "outstanding_fen": 15000,
                },
            ),
            (
                "opening_payroll_payable",
                "tax",
                {
                    "employee_id": "employee",
                    "recipient_id": "authority",
                    "component": "withheld_tax",
                    "payroll_period": "2025-12",
                    "outstanding_fen": 800,
                },
            ),
            (
                "opening_equity",
                "equity",
                {
                    "equity_kind": "paid_in_capital",
                    "holder_or_basis_id": "owner",
                    "balance_fen": 184200,
                },
            ),
        ]
    )
    _close_without_current_business(engine, "2026-01", proof)
    new = response(engine, "2026-01")
    assert new == full_response(engine, monkeypatch, "2026-01")
    assert new["data"]["employees"]["outstanding_net_fen"] == 15000
    assert new["data"]["employees"]["checking"] is False


def test_closed_bonus_net_cohorts_match_complete_obligations(tmp_path, monkeypatch):
    from test_payroll_corrections import Company

    company = Company(tmp_path / "bonus.sqlite")
    for source in bonus_sources():
        company.save(source.fact, source.subject_id)
    company.save(payroll(), "january")
    company.confirm_payroll("january")
    company.save(bonus(), "bonus")
    company.publish("january", "bonus")
    company.close("2026-01")
    assert response(company.engine, "2026-01") == full_response(
        company.engine, monkeypatch, "2026-01"
    )


@pytest.mark.parametrize("unknown", [False, True])
def test_frozen_net_cohorts_include_employee_beyond_default_page(
    opening_book, monkeypatch, unknown
):
    engine, _save, _publish, package, proof = opening_book
    package(
        [
            ("opening_cash", "cash", {"cash_account_id": "cash", "balance_fen": 1100000}),
            *[
                (
                    "opening_payroll_payable",
                    f"prior-{i:02}",
                    {
                        "employee_id": f"employee-{i:02}",
                        "recipient_id": f"employee-{i:02}",
                        "component": "net",
                        "payroll_period": "2025-12",
                        "outstanding_fen": 50000,
                    },
                )
                for i in range(22)
            ],
        ]
    )
    _close_without_current_business(engine, "2026-01", proof)
    original = settlement_freeze._scope
    if unknown:

        def incomplete(*args, **kwargs):
            # A legal incomplete projection after actual root verification;
            # normal business writes are not represented as damaged sources.
            scope = copy.deepcopy(original(*args, **kwargs))
            group = next(
                group
                for key, group in scope.groups.items()
                if key[2] == "221101" and key[3] == "employee-21"
            )
            group[settlement_freeze._G_UNKNOWN_COUNT] = group[
                settlement_freeze._G_BAD_SOURCE_COUNT
            ] = 1
            return scope

        monkeypatch.setattr(settlement_freeze, "_scope", incomplete)
    result = response(engine, "2026-01")
    assert result["data"]["collections"]["employees"]["page"]["returned_count"] == 20
    assert result["data"]["collections"]["employees"]["page"]["total_count"] == 22
    assert "employee-21" not in {
        item["employee_id"] for item in result["data"]["collections"]["employees"]["items"]
    }
    assert result["data"]["employees"]["checking"] is unknown
    assert result["data"]["employees"]["outstanding_net_fen"] == (None if unknown else 1100000)
    if not unknown:
        assert result == full_response(engine, monkeypatch, "2026-01")


@pytest.mark.parametrize("missing", [False, True])
def test_mixed_closed_month_locates_only_wage_payment_states_and_rejects_missing_key(
    tmp_path, monkeypatch, missing
):
    book = MixedBook(tmp_path / "mixed", employees=2, businesses=28)
    book.add_month(0)
    states = []
    original = settlement_freeze._decode_state

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        states.append(result)
        return result

    monkeypatch.setattr(settlement_freeze, "_decode_state", observed)
    new = response(book.engine, "2016-01")
    assert states and all(state["source_kind"] == "payroll" for state in states)
    paid_key = states[0]["obligation_key"]
    monkeypatch.setattr(settlement_freeze, "_decode_state", original)
    assert new == full_response(book.engine, monkeypatch, "2016-01")
    if missing:
        damage(
            book.engine,
            "settlement_state_revision",
            "DELETE FROM settlement_state_revision WHERE obligation_key=?",
            (paid_key,),
            foreign_keys=False,
        )
        with pytest.raises(KernelError):
            response(book.engine, "2016-01")
