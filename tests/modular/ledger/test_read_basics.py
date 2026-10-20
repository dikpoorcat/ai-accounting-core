"""Public selected reads retain formal amounts and load only requested details."""

from copy import deepcopy
from uuid import uuid4

import pytest
from test_adoption import BANK, BUSINESS, COMPANY, adopt_declared, expense, result
from test_adoption import store as store
from test_aggregations import journal
from test_financial_core import origin, save

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.ledger.public import AccountBalanceKey, VoucherLocator, create_ledger


def scope(month="2026-01", *, view="as_posted"):
    return {"company_id": COMPANY, "period_from": month, "period_to": month, "view": view}


def test_summary_uses_all_profit_main_rows_and_keeps_bank_despite_unknown_loan(store):
    ledger = create_ledger()
    earned = journal(str(uuid4()), [("1002", 170), ("5001", -100), ("5111", -30), ("630101", -40)])
    spent = journal(
        str(uuid4()), [("5401", 10), ("5602", 20), ("571104", 5), ("5801", 7), ("1002", -42)]
    )
    loan = journal(str(uuid4()), [("1002", 50), ("2501", -50)])
    save(store, ledger, [origin("0"), earned, spent, loan])
    with store.read_unit(COMPANY) as read:
        actual = ledger.financial_summary(scope(view="current"), read)
    assert actual.revenue == {"state": "known", "amount_fen": "170"}
    assert actual.expense == {"state": "known", "amount_fen": "42"}
    assert actual.net_profit == {"state": "known", "amount_fen": "128"}
    assert actual.bank == {"state": "known", "amount_fen": "178"}
    assert actual.liabilities["state"] == "unestablished"


def test_account_cube_retains_bank_and_fund_objects_without_inventing_initial_zero(store):
    ledger = create_ledger()
    key = AccountBalanceKey("1002", BANK)
    with store.read_unit(COMPANY) as read:
        absent = ledger.account_balances([key], "2026-01", read)[0]
    assert absent.opening["state"] == absent.ending["state"] == "unestablished"
    save(store, ledger, [origin("100")])
    with store.read_unit(COMPANY) as read:
        actual = ledger.account_balances([key, AccountBalanceKey("1002")], "2026-01", read)
    assert [row.ending["amount_fen"] for row in actual] == ["100", "100"]
    assert actual[0].opening["amount_fen"] == "100"
    assert actual[0].first_recorded_period == "2026-01"


def test_directory_and_exact_contents_never_load_vouchers_or_resource_children(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        for method in ("vouchers", "children", "current_heads", "heads_through"):
            monkeypatch.setattr(
                type(repo), method, lambda *a, **kw: pytest.fail("目录加载了无关明细")
            )
        page = ledger.query_adopted_directory(scope(), ("operations.expense",), PageQuery(1), read)
        assert page.total_count == 1 and page.items[0].reference["business_id"] == BUSINESS
        contents = ledger.get_adopted_contents([page.items[0].reference], read)
        assert contents[0].target["values"]["amount_fen"] == "100000"


def test_formal_flow_exposes_closed_correction_baseline_and_current_full_target(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        historical = ledger.query_adopted_values(
            scope("2026-02"), ("operations.expense",), PageQuery(), read
        )
        current = ledger.query_adopted_values(
            scope(view="current"), ("operations.expense",), PageQuery(), read
        )
        baseline = ledger.get_adopted_contents([historical.items[0].baseline_reference], read)[0]
    item = historical.items[0]
    assert item.target["values"]["amount_fen"] == "80000"
    assert item.baseline_target["values"]["amount_fen"] == "100000"
    assert baseline.basis["input"]["amount_fen"] == "100000"
    assert baseline.reference["state"] == "frozen"
    assert current.items[0].target["values"]["amount_fen"] == "80000"
    assert current.items[0].baseline_target is None
    assert current.items[0].baseline_reference is None


def test_voucher_page_reads_only_page_lines_and_keeps_frozen_original(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        original = type(repo).read_voucher_lines
        seen = []

        def counted(owner, ids):
            seen.extend(ids)
            return original(owner, ids)

        monkeypatch.setattr(type(repo), "read_voucher_lines", counted)
        monkeypatch.setattr(type(repo), "vouchers", lambda *a, **kw: pytest.fail("凭证页遍历全账"))
        page = ledger.query_voucher_directory(
            scope("2026-02", view="current"), (), PageQuery(1), read
        )
        assert page.total_count == 2 and len(page.items) == 1 and not seen
        header = page.items[0]
        value = ledger.get_vouchers(
            [
                VoucherLocator(
                    header.voucher_id, header.result["result_id"], header.result["generation"]
                )
            ],
            read,
        )[0]
        assert value["role"] == "reversal" and len(seen) == 1
        jan = ledger.query_voucher_directory(scope(), (), PageQuery(), read)
        assert len(jan.items) == 1 and jan.items[0].role == "original"


def test_exact_content_rejects_replaced_generation(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    old = result(store)["reference"]
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit)
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            ledger.get_adopted_contents([deepcopy(old)], read)
        assert error.value.code == "snapshot_changed"
