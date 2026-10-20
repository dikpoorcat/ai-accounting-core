"""Close-through ranges preserve precise financial history without empty-month rows."""

import pytest
from _financial_snapshot_review_support import BANK as APP_BANK
from _financial_snapshot_review_support import SOURCE as APP_SOURCE
from _financial_snapshot_review_support import assemble, fact, identity, loan_contract
from _financial_snapshot_review_support import expense as app_expense
from _financial_snapshot_review_support import opening as app_opening
from test_adoption import BUSINESS, COMPANY, PAYMENT, _payment_at, expense
from test_adoption import store as store
from test_aggregations import journal
from test_financial_core import FEE, SALE, funds, origin, report, save, value

from ai_accounting_modular.contracts.public import ContractError, content_digest, loads_json
from ai_accounting_modular.ledger import _financial
from ai_accounting_modular.ledger._balances import validate_capacities
from ai_accounting_modular.ledger._repository import LedgerRepository
from ai_accounting_modular.ledger.public import create_ledger


def close(storage, ledger, period):
    with storage.write_unit(COMPANY) as unit:
        checked = ledger.check_period(period, unit)
        assert checked.ready, checked.blocking_issues
        return ledger.close_period(period, checked.head, unit)


def scope(period="2026-03", first=None):
    return {
        "company_id": COMPANY,
        "period_from": first or period,
        "period_to": period,
        "view": "as_posted",
    }


def test_first_close_covers_prior_business_and_empty_months_in_one_record(store):
    ledger = create_ledger()
    with store.read_unit(COMPANY) as read:
        assert ledger.closed_through(read) is None
    save(
        store,
        ledger,
        [origin("0"), expense(), journal(FEE, [("5602", 2000), ("2202", -2000)], period="2026-03")],
    )
    closed = close(store, ledger, "2026-03")
    assert closed.state == "closed" and closed.closed_through == "2026-03"
    assert closed.period_from == "2026-01"
    assert closed.report["selection"]["closed_periods"] == ["2026-01", "2026-02", "2026-03"]
    with store.read_unit(COMPANY) as read:
        assert ledger.closed_through(read) == "2026-03"
        repo = read.repository("ledger")
        assert [r[0] for r in repo._session.fetchall("SELECT period FROM period_close")] == [
            "2026-03"
        ]
        assert all(head["state"] == "frozen" for head in repo.current_heads())
        january = ledger.get_closed_report(scope("2026-01"), read)
        assert value(january, "profit_statement", 14) == "100000"
        assert ledger.check_period("2026-02", read).state == "closed"
        row = repo.close_record("2026-03")
        assert row["report_digest"] == content_digest(loads_json(row["report_json"]))
    with store.write_unit(COMPANY) as unit:
        repeated = ledger.close_period("2026-02", "0" * 64, unit)
        assert repeated.state == "closed"


def test_skipped_month_is_closed_for_later_new_business_and_current_correction(store):
    ledger = create_ledger()
    save(store, ledger, [origin("0")])
    close(store, ledger, "2026-03")
    late = journal(SALE, [("5602", 100), ("2202", -100)], period="2026-02")
    with pytest.raises(ContractError) as error:
        save(store, ledger, [late])
    assert error.value.code == "closed_period_required"
    save(store, ledger, [late], correction="2026-04")
    assert value(report(store, ledger, "2026-02", view="as_posted"), "profit_statement", 14) == "0"
    assert value(report(store, ledger, "2026-02"), "profit_statement", 14) == "100"
    assert (
        value(report(store, ledger, "2026-04", view="as_posted"), "profit_statement", 14) == "100"
    )


def test_zero_opening_is_frozen_and_cannot_later_change_old_cutoff(store):
    ledger = create_ledger()
    initial = origin("0")
    save(store, ledger, [initial])
    close(store, ledger, "2026-03")
    changed = origin("200")
    changed["businesses"][0].update(op="replace", expected_seq=1)
    save(store, ledger, [changed], correction="2026-04")
    assert (
        value(report(store, ledger, "2026-02", view="as_posted"), "balance_sheet", 1, "ending")
        == "0"
    )
    assert (
        value(report(store, ledger, "2026-04", view="as_posted"), "balance_sheet", 1, "ending")
        == "200"
    )
    assert (
        value(report(store, ledger, "2026-04", view="as_posted"), "cash_flow_statement", 20) == "0"
    )


def test_exact_saved_month_is_verified_and_does_not_recalculate_amounts(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense()])
    expected = close(store, ledger, "2026-01").report

    def unexpected(*args, **kwargs):
        raise AssertionError("closed exact month must read its saved amounts")

    monkeypatch.setattr(_financial, "snapshot", unexpected)
    monkeypatch.setattr(LedgerRepository, "adoptions_between", unexpected)
    with store.read_unit(COMPANY) as read:
        actual = ledger.financial_snapshot(scope("2026-01"), read)
    assert actual["balance_sheet"] == expected["balance_sheet"]
    assert actual["selection"]["closed_periods"] == ["2026-01"]


def test_covered_month_and_quarter_use_first_cover_mapper_not_current_default(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense()])
    close(store, ledger, "2026-03")
    monkeypatch.setattr(_financial, "CURRENT_MAPPING_VERSION", "not-the-adopted-mapper")
    with store.read_unit(COMPANY) as read:
        month = ledger.financial_snapshot(scope("2026-01"), read)
        quarter = ledger.get_closed_report(scope("2026-03", "2026-01"), read)
    assert value(month, "profit_statement", 14) == "100000"
    assert value(quarter, "profit_statement", 14) == "100000"


def test_unknown_cashflow_is_archived_without_rejecting_known_bookkeeping(store):
    ledger = create_ledger()
    save(
        store, ledger, [origin(), funds(journal(SALE, [("1002", 100), ("5001", -100)]), 100, None)]
    )
    closed = close(store, ledger, "2026-01")
    assert closed.ready and closed.report["status"] == "partial"
    assert value(closed.report, "balance_sheet", 1, "ending") == "130100"
    assert closed.report["cash_flow_statement"]["20"]["current"]["state"] == "unestablished"


def test_close_rejects_changed_same_scope_but_ignores_unrelated_future_business(store):
    ledger = create_ledger()
    save(store, ledger, [origin()])
    with store.read_unit(COMPANY) as read:
        before = ledger.check_period("2026-01", read)
    save(store, ledger, [journal(FEE, [("5602", 1), ("2202", -1)], period="2026-02")])
    with store.write_unit(COMPANY) as unit:
        assert ledger.close_period("2026-01", before.head, unit).state == "closed"
    with store.read_unit(COMPANY) as read:
        before = ledger.check_period("2026-02", read)
    save(store, ledger, [journal(SALE, [("5602", 2), ("2202", -2)], period="2026-02")])
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            ledger.close_period("2026-02", before.head, unit)
    assert error.value.code == "conflict"


def test_missing_formal_opening_is_a_real_blocker_and_open_report_is_not_closed(store):
    ledger = create_ledger()
    with store.read_unit(COMPANY) as read:
        checked = ledger.check_period("2026-01", read)
        assert not checked.ready
        assert checked.blocking_issues[0]["code"] == "opening_basis_missing"
        with pytest.raises(ContractError) as error:
            ledger.get_closed_report(scope("2026-01"), read)
        assert error.value.code == "not_found"


def test_historical_resources_use_the_selected_payment_and_allocations_not_future_correction(store):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense(), _payment_at()])
    close(store, ledger, "2026-01")
    save(store, ledger, [_payment_at("80000", seq=1)], correction="2026-02")
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        old, current = repo.resource_view(as_of="2026-01"), repo.resource_view()
        validate_capacities(old, {BUSINESS, PAYMENT})
        validate_capacities(current, {BUSINESS, PAYMENT})
        old_head = old.current_heads([PAYMENT])[0]
        old_movement = old.children("result_fund_movement", [old_head["result_id"]])[0]
        movement_id = old_movement["movement_id"]
        assert old.movements([movement_id])[0]["amount_fen"] == 100000
        assert current.movements([movement_id])[0]["amount_fen"] == 80000
        assert old.settlements_by_movements([movement_id])[0]["amount_fen"] == 100000
        assert current.settlements_by_movements([movement_id])[0]["amount_fen"] == 80000


def test_earlier_cutoff_does_not_consume_a_later_real_payment(store):
    ledger = create_ledger()
    later = _payment_at("50000", period="2026-02")
    save(store, ledger, [origin(), expense(), later])
    movement_id = later["plans"][0]["fund_movements"][0]["movement_id"]
    with store.read_unit(COMPANY) as read:
        selected = read.repository("ledger").resource_view(as_of="2026-01")
        assert selected.incoming("settlement", {BUSINESS}) == []
        assert selected.movements([movement_id]) == []
        assert selected.settlements_by_movements([movement_id]) == []
        assert ledger.check_period("2026-01", read).ready
    closed = close(store, ledger, "2026-01")
    assert value(closed.report, "cash_flow_statement", 20) == "0"
    assert value(closed.report, "balance_sheet", 33, "ending") == "100000"


def test_first_cover_uses_requested_cutoff_for_opening_not_the_later_cover_month(store):
    ledger = create_ledger()
    save(store, ledger, [origin("100")])
    close(store, ledger, "2026-01")
    changed = origin("200")
    changed["businesses"][0].update(op="replace", expected_seq=1)
    save(store, ledger, [changed], correction="2026-08")
    close(store, ledger, "2026-09")
    july = report(store, ledger, "2026-07", view="as_posted")
    september = report(store, ledger, "2026-09", view="as_posted")
    assert value(july, "balance_sheet", 1, "beginning") == "100"
    assert value(july, "balance_sheet", 1, "ending") == "100"
    assert value(september, "balance_sheet", 1, "beginning") == "200"
    assert value(september, "balance_sheet", 1, "ending") == "200"
    assert value(september, "cash_flow_statement", 20) == "0"


def test_failed_archive_write_cannot_commit_a_partial_freeze_even_if_caught(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense()])
    with store.read_unit(COMPANY) as read:
        checked = ledger.check_period("2026-03", read)

    def fail_archive(*args, **kwargs):
        raise RuntimeError("synthetic archive failure")

    monkeypatch.setattr(LedgerRepository, "save_close", fail_archive)
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            with pytest.raises(RuntimeError, match="archive failure"):
                ledger.close_period("2026-03", checked.head, unit)
    assert error.value.code == "invalid_operation"
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        assert repo.closed_through() is None
        assert all(head["state"] == "open" for head in repo.current_heads())


def test_prebook_contracts_remain_nonaccounting_and_can_be_corrected_after_close(tmp_path):
    book = assemble(tmp_path)
    contract = loan_contract("prebook-before-close", "2028-01-01", month="2025-01")
    book.submit(app_opening(), contract)
    with book.app.storage.write_unit(book.company) as unit:
        checked = book.app.ledger.check_period("2026-03", unit)
        book.app.ledger.close_period("2026-03", checked.head, unit)
    contract["data"]["matures_on"] = "2029-01-01"
    book.replace(contract)
    book.submit(loan_contract("prebook-after-close", "2029-01-01", month="2024-01"))
    with book.app.storage.read_unit(book.company) as read:
        repo = read.repository("ledger")
        heads = repo.current_heads([contract["business_id"]])
        assert heads[0]["state"] == "open" and heads[0]["posting_period"] == "2025-01"
        assert repo.closed_through() == "2026-03"


def test_capacity_read_omits_large_original_statement_and_pure_policy_but_freezes_both(
    tmp_path, monkeypatch
):
    book = assemble(tmp_path)
    statement = fact(
        "large-original-statement",
        "money.bank_statement",
        {
            "funds_account_id": APP_BANK,
            "opening_fen": "0",
            "closing_fen": "100100",
            "rows": [
                {
                    "row_id": identity(f"original-row-{i}"),
                    "source": {"source_id": APP_SOURCE, "location": f"原件第{i + 1}行"},
                    "actual_on": "2026-01-20",
                    "direction": "inflow",
                    "amount_fen": "100",
                }
                for i in range(1001)
            ],
        },
    )
    policy = fact(
        "pure-closing-policy",
        "taxes.surtax_policy",
        {
            "policy": {
                "version": "synthetic-1",
                "official_url": "https://www.chinatax.gov.cn/",
                "effective_from": "2026-01-01",
            },
            "urban_rate": "0.07",
            "education_rate": "0.03",
            "local_education_rate": "0.02",
            "payable_fraction": "0.5",
        },
    )
    expense_fact = app_expense("closing-resource-owner", "10000")
    book.submit(app_opening(), statement, policy, expense_fact)
    loaded = set()
    original = LedgerRepository.heads_through

    def track_capacity_read(repo, as_of, business_ids=()):
        loaded.update(business_ids)
        return original(repo, as_of, business_ids)

    monkeypatch.setattr(LedgerRepository, "heads_through", track_capacity_read)
    with book.app.storage.write_unit(book.company) as unit:
        checked = book.app.ledger.check_period("2026-01", unit)
        assert checked.ready
        closed = book.app.ledger.close_period("2026-01", checked.head, unit)
        assert closed.state == "closed"
        heads = unit.repository("ledger").current_heads(
            [statement["business_id"], policy["business_id"]]
        )
        assert len(heads) == 2 and all(head["state"] == "frozen" for head in heads)
    assert expense_fact["business_id"] in loaded
    assert loaded.isdisjoint({statement["business_id"], policy["business_id"]})


def test_capacity_read_includes_fund_movement_without_obligation(store, monkeypatch):
    ledger = create_ledger()
    save(
        store,
        ledger,
        [
            origin(),
            funds(journal(SALE, [("1002", 100), ("5001", -100)]), 100, "customer_receipts"),
        ],
    )
    loaded = set()
    original = LedgerRepository.heads_through

    def track_capacity_read(repo, as_of, business_ids=()):
        loaded.update(business_ids)
        return original(repo, as_of, business_ids)

    monkeypatch.setattr(LedgerRepository, "heads_through", track_capacity_read)
    with store.read_unit(COMPANY) as read:
        assert ledger.check_period("2026-01", read).ready
    assert SALE in loaded


def test_resource_capacity_corruption_still_blocks_close_after_read_is_narrowed(store):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense(), _payment_at()])
    # Deliberate open-row damage: public adoption cannot create this over-allocation.
    with store.write_unit(COMPANY) as unit:
        unit.repository("ledger")._session.execute(
            "UPDATE settlement SET amount_fen=100001 WHERE source_business_id=?", (BUSINESS,)
        )
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            ledger.check_period("2026-01", read)
        assert error.value.code == "dependency_conflict"
        assert read.repository("ledger").closed_through() is None


def test_known_cash_to_posting_difference_is_a_blocker_not_an_allowed_partial(store):
    ledger = create_ledger()
    save(store, ledger, [origin(), journal(SALE, [("1002", 100), ("5001", -100)])])
    with store.read_unit(COMPANY) as read:
        checked = ledger.check_period("2026-01", read)
        assert not checked.ready
        assert any(
            issue["code"] == "financial_reconciliation_failed" for issue in checked.blocking_issues
        )
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            ledger.close_period("2026-01", checked.head, unit)
    assert error.value.code == "needs_information"
    with store.read_unit(COMPANY) as read:
        assert read.repository("ledger").closed_through() is None


@pytest.mark.parametrize("field", ["report_digest", "result_head", "report_schema"])
def test_saved_report_requires_valid_record_contract_and_digests(store, monkeypatch, field):
    ledger = create_ledger()
    save(store, ledger, [origin()])
    close(store, ledger, "2026-01")
    original = LedgerRepository.close_record

    def damaged(repo, period, *, covering=False):
        row = original(repo, period, covering=covering)
        row[field] = "wrong-schema" if field == "report_schema" else "0" * 64
        return row

    monkeypatch.setattr(LedgerRepository, "close_record", damaged)
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            ledger.get_closed_report(scope("2026-01"), read)
        assert error.value.code == "storage_corrupt"
