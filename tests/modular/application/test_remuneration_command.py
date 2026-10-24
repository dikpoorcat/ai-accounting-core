"""AI reads payroll payment capacity from the public command, never subtracting tax itself."""

import pytest
from jsonschema import Draft202012Validator

from ai_accounting_modular.agent.public import create_agent
from ai_accounting_modular.contracts.public import ContractError
from tests.modular.domains.people.test_people import PERSON, declaration, no_contributions
from tests.modular.domains.people.test_read_remuneration import (
    add,
    bank_object,
    payment,
    wage_with_actuals,
)
from tests.modular.domains.people.test_read_remuneration_tax import tax_target, unpaid_wage

from ._working_case import case as case


def query(agent, book, wage, month="2026-02", **extra):
    return agent.dispatch(
        "accounting_remuneration",
        {
            "company_id": book.company,
            "period": month,
            "business_ids": [wage["business_id"]],
            **extra,
        },
    )


def test_public_command_exposes_current_capacity_without_changing_exact_payroll(case, monkeypatch):
    case.seed(expense=False)
    wage, bank = unpaid_wage(), bank_object()
    add(case, no_contributions(), wage, objects=[bank])
    agent = create_agent(case.app)
    initial = query(agent, case, wage, "2026-01")
    item = initial["items"][0]
    assert initial["company_id"] == case.company
    assert initial["database_id"]
    assert initial["view"] == "current"
    assert (item["person_id"], item["remuneration_type"], item["business_period"]) == (
        PERSON,
        "wage",
        "2026-01",
    )
    assert item["tax_state"] == "not_withheld_not_filed"
    assert item["amounts"]["net_fen"] == item["payable_fen"] == "100000"
    exact = case.get(wage["business_id"])
    tax = tax_target(wage, "10000")
    add(case, tax)
    after_tax = query(agent, case, wage)["items"][0]
    assert after_tax["tax_targets"][0]["business_id"] == tax["business_id"]
    assert after_tax["amounts"]["tax_fen"] == "10000"
    assert after_tax["amounts"]["net_fen"] == after_tax["payable_fen"] == "90000"
    paid = payment(bank["object_id"], "2026-02", (wage["business_id"], "30000"))
    add(case, paid)

    def no_monthly_activity(*_args, **_kwargs):
        pytest.fail(
            "selected payroll command must not build monthly activity or scan wide positions"
        )

    owner = case.app.registry.owner("people.remuneration")
    with monkeypatch.context() as scoped:
        scoped.setattr(owner, "remuneration_activity", no_monthly_activity)
        scoped.setattr(case.kernel.ledger, "capture_obligation_directory", no_monthly_activity)
        scoped.setattr(case.kernel.ledger, "query_obligation_positions", no_monthly_activity)
        remaining = query(agent, case, wage)["items"][0]
        assert remaining["amounts"]["net_fen"] == "90000"
        assert remaining["remaining_fen"] == remaining["payable_fen"] == "60000"
    add(
        case, payment(bank["object_id"], "2026-02", (wage["business_id"], remaining["payable_fen"]))
    )
    settled = query(agent, case, wage)["items"][0]
    assert settled["remaining_fen"] == settled["payable_fen"] == "0"
    assert settled["amounts"]["net_fen"] == "90000"
    assert query(agent, case, wage, "2026-01") == initial
    assert query(agent, case, wage, "2026-01", view="as_posted")["items"] == initial["items"]
    assert case.get(wage["business_id"])["businesses"] == exact["businesses"]


def test_public_payable_keeps_only_unabsorbed_hold_and_excludes_already_paid(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    declared, reference = declaration(wage, "40000")
    wage["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    bank = bank_object()
    add(case, wage, employment, actual, declared, objects=[bank])
    agent = create_agent(case.app)
    original = query(agent, case, wage, "2026-01")["items"][0]
    assert original["amounts"]["held_fen"] == "10000"
    assert original["payable_fen"] == "490000"
    add(case, tax_target(wage, "35000", tax_period="2026-01"))
    add(case, payment(bank["object_id"], "2026-02", (wage["business_id"], "100000")))
    current = query(agent, case, wage)["items"][0]
    assert current["amounts"]["net_fen"] == "495000"
    assert current["amounts"]["disbursement_target_fen"] == "490000"
    assert current["amounts"]["held_fen"] == "5000"
    assert current["remaining_fen"] == "395000"
    assert current["payable_fen"] == "390000"


def test_agent_publishes_exact_narrow_request_and_rejects_ambiguous_inputs(case):
    agent = create_agent(case.app)
    schema = next(
        command["request_schema"]
        for command in agent.contracts()["commands"]
        if command["command"] == "accounting_remuneration"
    )
    valid = {
        "company_id": case.company,
        "period": "2026-02",
        "business_ids": [case.source_id],
    }
    validator = Draft202012Validator(schema)
    assert validator.is_valid(valid)
    for invalid in (
        {**valid, "business_ids": []},
        {**valid, "business_ids": [case.source_id, case.source_id]},
        {**valid, "amount_fen": "100000"},
        {**valid, "view": "latest"},
    ):
        assert not validator.is_valid(invalid)
        with pytest.raises(ContractError, match="合同"):
            agent.dispatch("accounting_remuneration", invalid)
