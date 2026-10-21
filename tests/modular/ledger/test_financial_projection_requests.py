"""Persisted consumers use the same explicit financial range as public reads."""

from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.public import FINANCIAL_PROJECTION_FORMAT, Ledger

COMPANY = "00000000-0000-0000-0000-000000000001"
DATABASE = "00000000-0000-0000-0000-000000000002"


def context(**changes):
    return SimpleNamespace(
        **{
            "company_id": COMPANY,
            "database_id": DATABASE,
            "start_period": "2026-01",
            "head": "current-company-head",
        }
        | changes
    )


def scope(**changes):
    return {
        "company_id": COMPANY,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": "as_posted",
    } | changes


def test_factory_normalizes_once_and_ignores_transient_expected_and_company_head():
    ledger, read = Ledger(), context()
    raw = scope(expected_head="a" * 64)
    created = ledger.financial_projection_request("summary_values", raw, read)
    other = ledger.financial_projection_request(
        "summary_values", scope(as_of="2026-01"), context(head="later-unrelated-head")
    )
    assert FINANCIAL_PROJECTION_FORMAT == 2
    assert created == other
    assert created.descriptor_key == ("requests", created.key)
    assert created.scope == scope(as_of="2026-01")
    raw["period_to"] = "2026-03"
    assert created.scope["period_to"] == "2026-01"


def test_real_request_identity_separates_output_kind_database_and_range():
    ledger = Ledger()
    original = ledger.financial_projection_request("summary_values", scope(), context())
    other = (
        ledger.financial_projection_request("snapshot", scope(), context()),
        ledger.financial_projection_request(
            "summary_values", scope(), context(database_id="00000000-0000-0000-0000-000000000003")
        ),
        ledger.financial_projection_request(
            "summary_values", scope(period_from="2026-02", period_to="2026-02"), context()
        ),
        ledger.financial_projection_request("summary_values", scope(view="current"), context()),
    )
    assert len({original.key, *(request.key for request in other)}) == 5


@pytest.mark.parametrize(
    "kind,selected",
    [
        ("other", scope()),
        ("snapshot", scope(as_of="2026-02")),
        ("snapshot", scope(period_to="2026-02")),
        ("snapshot", scope(company_id="00000000-0000-0000-0000-000000000004")),
    ],
)
def test_factory_keeps_original_financial_scope_rejections(kind, selected):
    with pytest.raises(ContractError):
        Ledger().financial_projection_request(kind, selected, context())
