"""Cooperation supplements remain typed derived positions, never ordinary debts."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.financing.public import create_investment

from ._cooperation_support import (
    PROJECT,
    agency_payment,
    contract,
    expense,
    funding,
    operator,
    submitted,
)
from ._support import OWNER, assemble, change


def scope(company, month="2026-01", view="current", **extra):
    return {"company_id": company, "period_from": month, "period_to": month, "view": view, **extra}


def scenario(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    charged = expense(agreement, project="300", company="150")
    paid = agency_payment(agreement, charged, project="240", company="120")
    into = operator(agreement, 140, into=True, day="2026-01-04")
    saved = app.accounting_apply(
        submitted(company, agreement, funding(agreement), into, charged, paid)
    )
    assert saved["completion"] == "complete", saved
    return app, company, agreement, create_investment(ledger=app.ledger)


def test_exact_adopted_position_is_read_without_vouchers_or_ordinary_resources(
    tmp_path, monkeypatch
):
    app, company, agreement, owner = scenario(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("位置读取不得读取完整凭证或猜普通义务")

    monkeypatch.setattr(app.ledger, "get_vouchers", forbidden)
    monkeypatch.setattr(app.ledger, "query_obligation_positions", forbidden)
    with app.storage.read_unit(company) as read:
        actual = owner.cooperation_positions(scope(company), read)
        assert len(actual.positions) == 1
        item = actual.positions[0]
        assert (item.contract_id, item.operator_id, item.project_id) == (
            agreement["business_id"],
            OWNER,
            PROJECT,
        )
        assert (item.held_fen, item.company_due_fen, item.operator_due_fen) == (0, 20, 0)
        assert item.as_of_period == "2026-01" and item.status == "active"
        assert item.reference["result_slot"] == "position:2026-01"
        assert item.source["result_id"] == item.reference["result_id"]
        assert (
            owner.cooperation_positions(scope(company, expected_head=actual.version), read)
            == actual
        )
        assert (
            owner.cooperation_positions(
                scope(company), read, contract_ids=(agreement["business_id"],)
            ).positions
            == actual.positions
        )
        quiet = owner.cooperation_positions(scope(company, "2026-02", "as_posted"), read)
        assert quiet.positions == actual.positions
        assert quiet.positions[0].as_of_period == "2026-01"


def test_open_replacement_changes_position_version_and_preserves_historical_cutoff(tmp_path):
    app, company, agreement, owner = scenario(tmp_path)
    with app.storage.read_unit(company) as read:
        previous = owner.cooperation_positions(scope(company), read)
    top_up = funding(agreement, "20", "2026-01-10")
    top_up["data"]["purpose"] = "replenishment"
    saved = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": top_up["business_id"],
            "mutations": [{"op": "create", "expected_seq": 0, "fact": top_up}],
        }
    )
    assert saved["completion"] == "complete", saved
    with app.storage.read_unit(company) as read:
        current = owner.cooperation_positions(scope(company), read)
        assert current.positions[0].company_due_fen == 0
        assert current.version != previous.version
        with pytest.raises(ContractError) as changed:
            owner.cooperation_positions(scope(company, expected_head=previous.version), read)
        assert changed.value.code == "snapshot_changed"


def test_frozen_position_keeps_its_month_and_exact_adopted_contract(tmp_path, monkeypatch):
    app, company, agreement, owner = scenario(tmp_path)
    # This check freezes adopted owner results, independently of period-close readiness.
    with app.storage.write_unit(company) as unit:
        unit.repository("ledger").freeze_heads("2026-01")
    changed = deepcopy(agreement)
    changed["data"]["company_initial_commitment_fen"] = "200"
    saved = app.accounting_apply(change(company, changed, correction_period="2026-02"))
    assert saved["completion"] == "complete", saved
    used = []
    get_sources = app.ledger.get_adopted_sources

    def record_sources(sources, read):
        used.extend(source["result_id"] for source in sources)
        return get_sources(sources, read)

    monkeypatch.setattr(app.ledger, "get_adopted_sources", record_sources)
    with app.storage.read_unit(company) as read:
        january = owner.cooperation_positions(scope(company, view="as_posted"), read)
        february = owner.cooperation_positions(scope(company, "2026-02", "as_posted"), read)
    assert january.positions[0].company_due_fen == 20
    assert february.positions[0].company_due_fen == 20
    assert january.positions[0].source != february.positions[0].source
    assert len(used) == 2 and used[0] != used[1]


@pytest.mark.parametrize(
    "extra",
    [
        {"period_from": "2025-12"},
        {"view": "as_posted", "as_of": "2026-02"},
        {"object_ids": [OWNER]},
    ],
)
def test_position_scope_is_one_explicit_company_month(tmp_path, extra):
    app, company, _, owner = scenario(tmp_path)
    with app.storage.read_unit(company) as read, pytest.raises(ContractError) as error:
        owner.cooperation_positions(scope(company, **extra), read)
    assert error.value.code == "invalid_input"
