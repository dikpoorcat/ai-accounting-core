"""Read boundaries retain exact identities, unknowns, retirement and closed authority."""

import base64

import pytest
from test_adoption import (
    BUSINESS,
    COMPANY,
    _payment_at,
    adopt_declared,
    expense,
    seed_payment,
    withdrawal,
)
from test_adoption import store as store
from test_closing import close
from test_financial_core import origin, save
from test_read_basics import scope

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    QuerySelection,
    canonical_json,
)
from ai_accounting_modular.ledger._financial_projection_selection import ProjectionCalculation
from ai_accounting_modular.ledger.public import (
    AccountBalanceKey,
    LedgerQuery,
    ObligationPositionQuery,
    create_ledger,
)


def test_no_output_opening_and_retired_slot_remain_in_adopted_directory(store):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), expense()])
    with store.write_unit(COMPANY) as unit:
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        baseline = ledger.get_result_source(BUSINESS, "main", unit)
        adopt_declared(ledger, [withdrawal(baseline)], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        empty = ledger.query_adopted_directory(scope(), ("opening.package",), PageQuery(), read)
        assert len(empty.items) == 1
        contents = ledger.get_adopted_contents([empty.items[0].reference], read)
        assert contents[0].target["values"]["debit_fen"] == "0"
        retired = ledger.query_adopted_values(
            scope("2026-02"), ("operations.expense",), PageQuery(), read
        )
        assert retired.filtered_count == 1
        assert retired.items[0].target["kind"] == "system.withdrawal"
        assert retired.items[0].baseline_target["values"]["amount_fen"] == "100000"


def test_closed_summary_uses_saved_profit_even_if_current_calculator_changes(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), expense()])
    close(store, ledger, "2026-01")

    def forbidden(*args, **kwargs):
        pytest.fail("闭期摘要重新计算了已保存的损益或资产负债")

    monkeypatch.setattr(ProjectionCalculation, "profit", forbidden)
    monkeypatch.setattr(ProjectionCalculation, "positions", forbidden)
    with store.read_unit(COMPANY) as read:
        actual = ledger.financial_summary(scope(), read)
    assert actual.expense == {"state": "known", "amount_fen": "100000"}
    assert actual.net_profit == {"state": "known", "amount_fen": "-100000"}
    assert actual.current_liabilities == {"state": "known", "amount_fen": "100000"}
    assert actual.non_current_liabilities == {"state": "known", "amount_fen": "0"}


def test_large_exact_resource_amount_never_passes_through_float(store):
    ledger = create_ledger()
    seed_payment(store)
    amount = 2**53 + 1
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense(str(amount)), _payment_at("1")], unit)
    with store.read_unit(COMPANY) as read:
        page = ledger.query_obligation_positions(
            ObligationPositionQuery("2026-01"), PageQuery(), read
        )
    assert page.page.items[0].amounts.amount_fen == amount
    assert page.page.items[0].amounts.remaining_fen == 2**53
    assert type(page.full_totals[0].amounts.remaining_fen) is int


def test_account_as_posted_cutoff_mismatch_is_rejected_not_ignored(store):
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            create_ledger().account_balances(
                [AccountBalanceKey("1002")],
                "2026-01",
                read,
                selection=QuerySelection("as_posted", "2026-02"),
            )
    assert error.value.code == "invalid_input"


def test_same_context_explicit_selection_preserves_frozen_and_current_obligations(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit, correction_period="2026-02")
    with store.read_unit(COMPANY, view="as_posted", as_of="2026-01") as read:
        prior = ledger.query_obligations(LedgerQuery(), read).items[0]
        latest = ledger.query_obligations(
            LedgerQuery(), read, selection=QuerySelection("current")
        ).items[0]
        ref = {"business_id": BUSINESS, "slot": "primary"}
        resolved = ledger.resolve([ref], "obligation", read, selection=QuerySelection("current"))[0]
    assert prior["balance"]["remaining_fen"] == "100000"
    assert latest["balance"]["remaining_fen"] == resolved["balance"]["remaining_fen"] == "80000"


def test_invalid_key_types_and_stale_query_cursor_are_structured_rejections(store):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), expense()])
    with store.read_unit(COMPANY) as read:
        first = ledger.query_adopted_directory(scope(), (), PageQuery(1), read)
        cursor_body = {"version": first.version, "key": [1, "main", BUSINESS]}
        malformed = base64.urlsafe_b64encode(canonical_json(cursor_body).encode()).decode()
        with pytest.raises(ContractError) as error:
            ledger.query_adopted_directory(scope(), (), PageQuery(1, malformed), read)
        assert error.value.code == "invalid_input"
        invalid_scope = {**scope(view="current"), "as_of": "2026-01"}
        with pytest.raises(ContractError) as error:
            ledger.query_adopted_directory(invalid_scope, (), PageQuery(), read)
        assert error.value.code == "invalid_input"


@pytest.mark.parametrize("method", ["query_adopted_directory", "query_voucher_directory"])
def test_scope_and_page_versions_share_the_actual_directory_version(store, method):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), expense()])
    query = getattr(ledger, method)
    with store.read_unit(COMPANY) as read:
        old = query(scope(), (), PageQuery(), read)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit)
    with store.read_unit(COMPANY) as read:
        current = query(scope(), (), PageQuery(), read)
        assert current.version != old.version
        accepted = query(
            {**scope(), "expected_head": current.version},
            (),
            PageQuery(expected_version=current.version),
            read,
        )
        assert accepted == current
        for requested_scope, page in (
            ({**scope(), "expected_head": old.version}, PageQuery()),
            (scope(), PageQuery(expected_version=old.version)),
        ):
            with pytest.raises(ContractError) as error:
                query(requested_scope, (), page, read)
            assert error.value.code == "snapshot_changed"
