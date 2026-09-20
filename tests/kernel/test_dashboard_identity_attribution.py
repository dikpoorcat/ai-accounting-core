import pytest
from test_identity_corrections import confirm, opening_package
from test_identity_corrections import identity_engine as _identity_engine_fixture
from test_opening_continuation import _close_without_current_business

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.materials import Materials


@pytest.fixture
def identity_engine_fixture(tmp_path):
    return _identity_engine_fixture.__wrapped__(tmp_path)


def add_open_period(engine, evidence, period):
    Materials(engine).receive(
        "dashboard-period:" + period,
        {
            "period": period,
            "evidence_digest": evidence,
            "category": "transactions",
            "purpose": "supporting",
            "supporting_purpose": "建立合成测试的明确开放月份",
            "specification": {
                "format": "text",
                "all_pages_reviewed": True,
                "passages": [
                    {
                        "location": "confirmation",
                        "page": 1,
                        "excerpt": "synthetic identity confirmation",
                    }
                ],
            },
        },
        evidence=(evidence,),
        expected_revision=0,
        request_id="dashboard-period:" + period,
    )


def test_fund_identity_reassignment_is_an_explicit_non_cash_adjustment(
    identity_engine_fixture,
):
    engine, evidence, owner, _ = identity_engine_fixture
    entities = Entities(engine)
    old_account = entities.register_entity(
        "fund_account",
        {"display_name": "原现金账户"},
        account_type="cash",
        source="synthetic",
        request_id="old-cash",
    )["entity_id"]
    current_account = entities.register_entity(
        "fund_account",
        {"display_name": "纠正后现金账户"},
        account_type="cash",
        source="synthetic",
        request_id="current-cash",
    )["entity_id"]
    opening = {"cash_account_id": old_account, "balance_fen": 10000}
    opening_package(
        engine,
        evidence,
        [
            ("opening_cash", "opening-cash", opening),
            (
                "opening_equity",
                "capital",
                {
                    "equity_kind": "paid_in_capital",
                    "balance_fen": 10000,
                    "holder_or_basis_id": owner,
                },
            ),
        ],
    )
    _close_without_current_business(engine, "2026-01", evidence)
    confirm(
        engine,
        {
            "changes": [
                {
                    "subject_id": "opening-cash",
                    "expected_revision": 1,
                    "action": "reassign",
                    "data": {
                        "period": "2026-01",
                        "package_id": "opening",
                        "cash_account_id": current_account,
                        "balance_fen": 10000,
                    },
                }
            ],
            "evidence": [evidence],
            "reason": "confirmed same account",
            "posting_period": "2026-03",
        },
    )
    add_open_period(engine, evidence, "2026-03")
    add_open_period(engine, evidence, "2026-04")
    funds = Dashboard(engine).funds("2026-03")["data"]
    accounts = {item["account_id"]: item for item in funds["accounts"]}
    assert set(accounts) == {current_account}
    assert accounts[current_account]["opening_fen"] == 0
    assert accounts[current_account]["net_change_fen"] == 0
    assert accounts[current_account]["attribution_adjustment_fen"] == 10000
    assert accounts[current_account]["closing_fen"] == 10000
    assert funds["opening_fen"] == 10000
    assert funds["net_change_fen"] == 0
    assert funds["total_fen"] == 10000
    assert funds["cash_fen"] == 10000

    next_month = Dashboard(engine).funds("2026-04")["data"]
    next_accounts = {item["account_id"]: item for item in next_month["accounts"]}
    assert set(next_accounts) == {current_account}
    assert next_accounts[current_account]["opening_fen"] == 10000
    assert next_accounts[current_account]["attribution_adjustment_fen"] == 0
    assert next_accounts[current_account]["closing_fen"] == 10000
