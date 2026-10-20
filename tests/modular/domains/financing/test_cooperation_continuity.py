"""Actual project cash survives open corrections and closed economic periods."""

from uuid import uuid4

import pytest

from ._cooperation_support import (
    agency_payment,
    contract,
    expense,
    funding,
    position,
    submitted,
)
from ._support import COUNTERPARTY, assemble, change, fact, get


def _main(app, company, item):
    return next(
        result
        for result in get(app, company, item["business_id"])["businesses"][0]["results"]
        if result["target"]["result_slot"] == "main"
    )


def test_later_operator_payment_uses_its_own_month_and_keeps_prior_unpaid_amount(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    charged = expense(agreement)
    paid = agency_payment(agreement, charged, day="2026-02-06")
    paid["period"] = "2026-02"
    actual = app.accounting_apply(submitted(company, paid, charged, agreement, funding(agreement)))
    assert actual["completion"] == "complete", actual
    january = position(app, company, agreement)
    february = position(app, company, agreement, "2026-02")
    assert january["held_fen"] == "100"
    assert january["company_trade_payable_fen"] == "10"
    assert february["held_fen"] == "80" and february["operator_due_fen"] == "10"
    assert january["company_capital_fen"] == february["company_capital_fen"] == "90"


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("corrected_project,corrected_company", [(16, 8), (0, 0)])
def test_corrected_cost_preserves_actual_agency_and_recovery_refund_does_not_reverse_cost_twice(
    tmp_path, closed, corrected_project, corrected_company
):
    app, company = assemble(tmp_path)
    agreement = contract()
    charged = expense(agreement)
    paid = agency_payment(agreement, charged)
    actual = app.accounting_apply(submitted(company, agreement, funding(agreement), charged, paid))
    assert actual["completion"] == "complete", actual
    old_agent = _main(app, company, paid)
    if closed:
        # This scenario verifies adoption, not the separate close prerequisites.
        with app.storage.write_unit(company) as unit:
            unit.repository("ledger").freeze_heads("2026-01")
    charged["data"].update(
        project_total_fen=str(corrected_project), amount_fen=str(corrected_company)
    )
    project_recovery, company_recovery = 20 - corrected_project, 10 - corrected_company
    month = "2026-02" if closed else "2026-01"
    claim = fact(
        "operations.overpayment",
        {
            "source": {"business_id": charged["business_id"], "slot": "primary"},
            "amount_fen": str(company_recovery),
            "project_total_fen": str(project_recovery),
        },
        period=month,
        actual=month + "-07",
    )
    altered = change(company, charged, correction_period=month if closed else None)
    altered["mutations"].append({"op": "create", "expected_seq": 0, "fact": claim})
    actual = app.accounting_apply(altered)
    assert actual["completion"] == "complete", actual
    current_agent = _main(app, company, paid)
    assert current_agent["target"]["values"]["project_amount_fen"] == "20"
    assert current_agent["target"]["values"]["company_amount_fen"] == "10"
    assert old_agent["target"] == current_agent["target"]
    january = position(app, company, agreement)
    assert january["company_expense_fen"] == str(corrected_company)
    assert january["company_trade_receivable_net_fen"] == str(company_recovery)
    received = fact(
        "investment.cooperation_collection",
        {
            "contract_id": agreement["business_id"],
            "source": {"business_id": claim["business_id"], "slot": "recovery"},
            "project_amount_fen": str(project_recovery),
        },
        period=month,
        actual=month + "-08",
    )
    actual = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [{"op": "create", "expected_seq": 0, "fact": received}],
            **({"correction_period": month} if closed else {}),
        }
    )
    assert actual["completion"] == "complete", actual
    final = position(app, company, agreement, month)
    assert final["company_expense_fen"] == str(corrected_company)
    assert final["project_paid_fen"] == str(corrected_project)
    assert final["company_paid_fen"] == str(corrected_company)
    assert final["held_fen"] == str(100 - corrected_project)
    assert final["operator_due_fen"] == str(corrected_project - corrected_company)
    assert final["company_capital_fen"] == str(100 - corrected_company)
    assert final["company_trade_receivable_net_fen"] == "0"
    assert _main(app, company, received)["target"]["values"]["company_amount_fen"] == str(
        company_recovery
    )


@pytest.mark.parametrize("closed", [False, True])
def test_withdrawing_misrecorded_agency_reopens_trade_without_inventing_company_cash(
    tmp_path, closed
):
    app, company = assemble(tmp_path)
    agreement = contract()
    charged = expense(agreement)
    paid = agency_payment(agreement, charged)
    actual = app.accounting_apply(submitted(company, agreement, funding(agreement), charged, paid))
    assert actual["completion"] == "complete", actual
    if closed:
        with app.storage.write_unit(company) as unit:
            unit.repository("ledger").freeze_heads("2026-01")
    actual = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [
                {"op": "withdraw", "business_id": paid["business_id"], "expected_seq": 1}
            ],
            **(
                {
                    "correction_period": "2026-02",
                    "explanation": "合成资料核对后确认经营者并未实际付款。",
                }
                if closed
                else {}
            ),
        }
    )
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement, "2026-02" if closed else "2026-01")
    assert current["held_fen"] == "100" and current["operator_due_fen"] == "0"
    assert current["company_trade_payable_fen"] == "10"
    assert current["company_capital_fen"] == "90"


def test_reassigning_the_same_real_trade_and_agency_updates_both_contract_positions(tmp_path):
    app, company = assemble(tmp_path)
    first, second = contract(), contract()
    second["data"]["project_id"] = str(uuid4())
    charged = expense(first)
    paid = agency_payment(first, charged)
    initial = submitted(company, first, second, funding(first), funding(second), charged, paid)
    initial["objects"].append(
        {
            "object_id": second["data"]["project_id"],
            "kind": "investment_product",
            "name": "合成另一合作项目",
            "expected_seq": 0,
            "data": {"category": "cooperation"},
        }
    )
    actual = app.accounting_apply(initial)
    assert actual["completion"] == "complete", actual
    charged["data"]["cooperation_id"] = second["business_id"]
    paid["data"]["contract_id"] = second["business_id"]
    actual = app.accounting_apply(change(company, paid, charged))
    assert actual["completion"] == "complete", actual
    old, new = position(app, company, first), position(app, company, second)
    assert old["held_fen"] == old["company_capital_fen"] == "100"
    assert old["operator_due_fen"] == "0"
    assert new["held_fen"] == "80" and new["operator_due_fen"] == "10"
    assert new["company_capital_fen"] == "90"


@pytest.mark.parametrize("closed", [False, True])
def test_zero_company_share_recovery_restores_real_project_cash_without_company_posting(
    tmp_path, closed
):
    app, company = assemble(tmp_path)
    agreement = contract()
    agreement["data"]["company_revenue_expense_share"] = "0.1"
    charged = expense(agreement, project="1", company="0")
    paid = agency_payment(agreement, charged, project="1")
    actual = app.accounting_apply(submitted(company, agreement, funding(agreement), charged, paid))
    assert actual["completion"] == "complete", actual
    assert position(app, company, agreement)["held_fen"] == "99"
    if closed:
        with app.storage.write_unit(company) as unit:
            unit.repository("ledger").freeze_heads("2026-01")
    month = "2026-02" if closed else "2026-01"
    charged["data"].update(project_total_fen="0", amount_fen="0")
    claim = fact(
        "operations.overpayment",
        {
            "source": {"business_id": charged["business_id"], "slot": "primary"},
            "amount_fen": "0",
            "project_total_fen": "1",
        },
        period=month,
        actual=month + "-07",
    )
    returned = fact(
        "investment.cooperation_collection",
        {
            "contract_id": agreement["business_id"],
            "source": {"business_id": claim["business_id"], "slot": "recovery"},
            "project_amount_fen": "1",
        },
        period=month,
        actual=month + "-08",
    )
    altered = change(company, charged, correction_period=month if closed else None)
    altered["mutations"].extend(
        {"op": "create", "expected_seq": 0, "fact": item} for item in (claim, returned)
    )
    actual = app.accounting_apply(altered)
    assert actual["completion"] == "complete", actual
    final = position(app, company, agreement, month)
    assert final["project_paid_fen"] == final["company_paid_fen"] == "0"
    assert final["held_fen"] == final["company_capital_fen"] == "100"
    assert _main(app, company, paid)["target"]["values"]["project_amount_fen"] == "1"
    for item in (claim, returned):
        assert (
            _main(app, company, item)["target"]["values"][
                "amount_fen" if item == claim else "company_amount_fen"
            ]
            == "0"
        )


def test_zero_company_share_cannot_invent_project_recovery_without_actual_overpayment(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    agreement["data"]["company_revenue_expense_share"] = "0.1"
    charged = expense(agreement, project="0", company="0")
    claim = fact(
        "operations.overpayment",
        {
            "source": {"business_id": charged["business_id"], "slot": "primary"},
            "amount_fen": "0",
            "project_total_fen": "1",
        },
        actual="2026-01-07",
    )
    returned = fact(
        "investment.cooperation_collection",
        {
            "contract_id": agreement["business_id"],
            "source": {"business_id": claim["business_id"], "slot": "recovery"},
            "project_amount_fen": "1",
        },
        actual="2026-01-08",
    )
    actual = app.accounting_apply(
        submitted(company, agreement, funding(agreement), charged, claim, returned)
    )
    assert actual["completion"] == "not_saved", actual
    assert actual["error"] == "dependency_conflict", actual
    assert get(app, company, agreement["business_id"])["businesses"] == []


def test_zero_company_source_correction_needs_real_project_excess_reclassification(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    agreement["data"]["company_revenue_expense_share"] = "0.1"
    charged = expense(agreement, project="1", company="0")
    paid = agency_payment(agreement, charged, project="1")
    actual = app.accounting_apply(submitted(company, agreement, funding(agreement), charged, paid))
    assert actual["completion"] == "complete", actual
    charged["data"].update(project_total_fen="0", amount_fen="0")
    actual = app.accounting_apply(change(company, charged))
    assert actual["completion"] == "not_saved", actual
    assert actual["error"] == "dependency_conflict", actual
    assert position(app, company, agreement)["held_fen"] == "99"


@pytest.mark.parametrize("explicit_company", [False, True])
@pytest.mark.parametrize("closed", [False, True])
def test_corrected_refund_range_changes_only_derived_company_allocation_not_actual_project_cash(
    tmp_path, explicit_company, closed
):
    app, company = assemble(tmp_path)
    agreement = contract()
    charged = expense(agreement, project="2", company="1")
    paid = agency_payment(agreement, charged, project="2")
    first, second = (
        fact(
            "operations.expense_recovery",
            {
                "expense_id": charged["business_id"],
                "counterparty_id": COUNTERPARTY,
                "project_total_fen": "1",
                "amount_fen": value,
            },
            actual=day,
        )
        for value, day in (("1", "2026-01-07"), ("0", "2026-01-08"))
    )
    first["business_id"] = "79000000-0000-4000-8000-000000000001"
    second["business_id"] = "79000000-0000-4000-8000-000000000002"
    received = fact(
        "investment.cooperation_collection",
        {
            "contract_id": agreement["business_id"],
            "source": {"business_id": second["business_id"], "slot": "primary"},
            "project_amount_fen": "1",
            **({"company_amount_fen": "0"} if explicit_company else {}),
        },
        actual="2026-01-09",
    )
    actual = app.accounting_apply(
        submitted(company, agreement, funding(agreement), charged, paid, first, second, received)
    )
    assert actual["completion"] == "complete", str(actual)
    assert _main(app, company, received)["target"]["values"]["company_amount_fen"] == "0"
    if closed:
        with app.storage.write_unit(company) as unit:
            unit.repository("ledger").freeze_heads("2026-01")
    first["data"].update(project_total_fen="0", amount_fen="0")
    second["data"]["amount_fen"] = "1"
    actual = app.accounting_apply(
        change(company, first, second, correction_period="2026-02" if closed else None)
    )
    assert actual["completion"] == "complete", actual
    adopted = _main(app, company, received)["target"]["values"]
    assert adopted["project_amount_fen"] == "1"
    assert adopted["company_amount_fen"] == ("0" if explicit_company else "1")
    current = position(app, company, agreement, "2026-02" if closed else "2026-01")
    assert current["project_paid_fen"] == "1"
    assert current["held_fen"] == "99"
    assert current["company_trade_receivable_net_fen"] == ("1" if explicit_company else "0")
    assert current["operator_due_fen"] == ("0" if explicit_company else "1")
