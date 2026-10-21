"""The AI report request reads the same formal natural-quarter snapshot as the dashboard."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract

from ._working_case import case as case


def request(case, **values):
    return {"company_id": case.company, "year": 2026, "quarter": 1, **values}


def test_empty_books_return_explained_unknown_cells_without_writing(case):
    snapshot = case.app.accounting_report(request(case))
    validate_contract("reporting.schema.json", "FinancialSnapshot", snapshot)
    assert snapshot["status"] == "partial" and snapshot["issues"]
    cell = snapshot["balance_sheet"]["1"]["ending"]
    assert cell["state"] == "unestablished" and "amount_fen" not in cell
    assert snapshot["scope"]["period_from"] == "2026-01"
    assert snapshot["scope"]["period_to"] == snapshot["scope"]["as_of"] == "2026-03"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == 0


def test_ready_report_is_exact_shared_snapshot_and_only_relevant_changes_expire_head(case):
    case.seed(expense=False)
    fact = case.expense()
    fact["data"]["report_details"] = [{"code": "management_other", "amount_fen": "100000"}]
    assert case.app.accounting_apply(case.request(fact))["completion"] == "complete"
    first = case.app.accounting_report(request(case))
    assert first["status"] == "ready"
    assert first["profit_statement"]["32"]["current"]["amount_fen"] == "-100000"
    head = first["selection"]["selection_digest"]
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.kernel.ledger.financial_snapshot(first["scope"], read) == first
    case.app.accounting_sources(
        {
            "company_id": case.company,
            "action": "import",
            "sources": [{"kind": "owner_statement", "text": "与报表金额无关的原始说明"}],
        }
    )
    second = case.app.accounting_report(request(case, expected_head=head))
    assert second["head"] > first["head"]
    assert second["selection"]["selection_digest"] == head
    changed = deepcopy(fact)
    changed["data"]["amount_fen"] = "120000"
    changed["data"]["report_details"][0]["amount_fen"] = "120000"
    update = case.request(changed)
    update["mutations"][0].update(op="replace", expected_seq=1)
    assert case.app.accounting_apply(update)["completion"] == "complete"
    with pytest.raises(ContractError) as failed:
        case.app.accounting_report(request(case, expected_head=head))
    assert failed.value.code == "snapshot_changed"


def test_closed_quarter_keeps_its_formal_snapshot_after_later_correction(case):
    fact = case.seed()
    assert case.close(case.check("2026-03"))["state"] == "closed"
    frozen = case.app.accounting_report(request(case))
    changed = deepcopy(fact)
    changed["data"]["amount_fen"] = "120000"
    update = case.request(changed, correction_period="2026-04")
    update["mutations"][0].update(op="replace", expected_seq=1)
    assert case.app.accounting_apply(update)["completion"] == "complete"
    now = case.app.accounting_report(
        request(case, expected_head=frozen["selection"]["selection_digest"])
    )
    assert now["profit_statement"] == frozen["profit_statement"]
    assert now["profit_statement"]["32"]["current"]["amount_fen"] == "-100000"
    assert (
        case.app.accounting_report(request(case, quarter=2))["profit_statement"]["32"]["current"][
            "amount_fen"
        ]
        == "-20000"
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"quarter": 0},
        {"quarter": 5},
        {"quarter": True},
        {"year": 0},
        {"view": "current"},
        {"company_id": str(uuid4())},
    ],
)
def test_report_rejects_invalid_or_unknown_scope(case, extra):
    with pytest.raises(ContractError):
        case.app.accounting_report(request(case, **extra))
