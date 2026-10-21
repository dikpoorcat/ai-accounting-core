"""Indexed balance candidates retain the complete formal-selection arithmetic."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_adoption import BANK, BUSINESS, COMPANY, adopt_declared, expense, withdrawal
from test_adoption import store as store
from test_aggregations import journal
from test_financial_core import ORIGIN, origin, save
from test_read_basics import scope

from ai_accounting_modular.contracts.public import ContractError, QuerySelection
from ai_accounting_modular.ledger import _financial_reads
from ai_accounting_modular.ledger._financial_selection import Selection
from ai_accounting_modular.ledger.public import AccountBalanceKey, create_ledger


def _wide(repo, view, end, start, issues, keys):
    return Selection(repo, view, end, start, issues)


def _compare(ledger, read, keys, period, monkeypatch, selection=None):
    narrow = ledger.account_balances(keys, period, read, selection=selection)
    with monkeypatch.context() as patch:
        patch.setattr(_financial_reads, "BalanceSelection", _wide)
        wide = ledger.account_balances(keys, period, read, selection=selection)
    for actual, expected in zip(narrow, wide, strict=True):
        assert replace(actual, sources=()) == replace(expected, sources=())
        assert all(ref in expected.sources for ref in actual.sources)
    return narrow


def _bank_journal(business_id, amount, *, period="2026-01", object_id=BANK, seq=0):
    change = journal(business_id, [("1002", amount), ("3001", -amount)], period=period)
    change["plans"][0]["lines"][0]["object_id"] = object_id
    responsibility = change["businesses"][0]
    responsibility.update(expected_seq=seq, op="replace" if seq else "create")
    return change


def test_only_matching_effective_lines_and_necessary_sources_are_loaded(store, monkeypatch):
    ledger = create_ledger()
    paid, other = str(uuid4()), str(uuid4())
    save(store, ledger, [origin("100"), _bank_journal(paid, -30), expense(business_id=other)])
    keys = (AccountBalanceKey("1002", BANK), AccountBalanceKey("1002"))
    with store.read_unit(COMPANY) as read:
        expected = _compare(ledger, read, keys, "2026-01", monkeypatch)
        repo = read.repository("ledger")
        loaded = []
        fetch = type(repo).read_balance_lines

        def observed(*args):
            rows = fetch(*args)
            loaded.extend(rows)
            return rows

        for name in ("financial_heads", "results", "vouchers", "children"):
            monkeypatch.setattr(type(repo), name, lambda *a, **kw: pytest.fail("加载了宽正文"))
        monkeypatch.setattr(type(repo), "read_balance_lines", observed)
        actual = ledger.account_balances(keys, "2026-01", read)
    assert actual == expected
    assert [row.ending["amount_fen"] for row in actual] == ["70", "70"]
    assert len(loaded) == 1 and loaded[0]["account_code"] == "1002"
    assert {ref["business_id"] for ref in actual[0].sources} == {ORIGIN, paid}


@pytest.mark.parametrize("view", ["current", "as_posted"])
def test_frozen_inheritance_empty_target_and_later_correction_match_wide(store, monkeypatch, view):
    ledger = create_ledger()
    movement = BUSINESS
    save(store, ledger, [origin("100"), expense("30")], freeze="2026-01")
    # An evidence-only formal adoption has no own journal and inherits January.
    proof = expense("30", seq=1)
    proof["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    save(store, ledger, [proof], correction="2026-02", freeze="2026-02")
    with store.read_unit(COMPANY) as read:
        assert [v["role"] for v in ledger.get_results([movement], read)[0]["vouchers"]] == []
        for period in ("2026-01", "2026-02"):
            selected = QuerySelection(view, period if view == "as_posted" else None)
            actual = _compare(
                ledger, read, [AccountBalanceKey("2202")], period, monkeypatch, selected
            )[0]
            assert actual.ending["amount_fen"] == "-30"
            if period == "2026-02":
                source = ledger.get_result_source(movement, "main", read)
                assert source in actual.sources
    with store.write_unit(COMPANY) as unit:
        baseline = ledger.get_result_source(movement, "main", unit)
        adopt_declared(ledger, [withdrawal(baseline, seq=2)], unit, correction_period="2026-03")
    with store.read_unit(COMPANY) as read:
        for period in ("2026-01", "2026-02", "2026-03"):
            selected = QuerySelection(view, period if view == "as_posted" else None)
            actual = _compare(
                ledger, read, [AccountBalanceKey("2202")], period, monkeypatch, selected
            )[0]
            assert actual.ending["amount_fen"] == (
                "-30" if view == "as_posted" and period < "2026-03" else "0"
            )
            assert actual.first_recorded_period == "2026-01"


def test_candidates_include_later_journals_but_respect_selected_account_and_object(
    store, monkeypatch
):
    ledger = create_ledger()
    movement, second = str(uuid4()), str(uuid4())
    original = _bank_journal(movement, 30)
    save(store, ledger, [origin("0"), original], freeze="2026-01")
    with store.write_unit(COMPANY) as unit:
        identity = unit.repository("identity")
        identity.session.execute(
            "INSERT INTO identity_object SELECT ?,kind,name,sort_key||'2',"
            "seq,data_json,content_digest,latest_expected_seq,latest_command_id "
            "FROM identity_object WHERE object_id=?",
            (second, BANK),
        )
    changed = _bank_journal(movement, 45, object_id=second, seq=1)
    changed["plans"][0]["lines"][0]["account_code"] = "1101"
    save(store, ledger, [changed], correction="2026-03")
    keys = (AccountBalanceKey("1002", BANK), AccountBalanceKey("1101", second))
    with store.read_unit(COMPANY) as read:
        current = _compare(ledger, read, keys, "2026-01", monkeypatch)
        january = _compare(
            ledger, read, keys, "2026-01", monkeypatch, QuerySelection("as_posted", "2026-01")
        )
        march = _compare(
            ledger, read, keys, "2026-03", monkeypatch, QuerySelection("as_posted", "2026-03")
        )
    assert [row.ending["amount_fen"] for row in current] == ["0", "45"]
    assert [row.ending["amount_fen"] for row in january] == ["30", "0"]
    assert [row.opening["amount_fen"] for row in march] == ["30", "0"]
    assert [row.ending["amount_fen"] for row in march] == ["0", "45"]
    assert current[1].first_recorded_period == "2026-03"


def test_opposite_contributions_keep_sources_and_static_months_keep_balances(store, monkeypatch):
    ledger = create_ledger()
    first, second = str(uuid4()), str(uuid4())
    save(store, ledger, [origin("0"), _bank_journal(first, 30), _bank_journal(second, -30)])
    with store.read_unit(COMPANY) as read:
        actual = _compare(ledger, read, [AccountBalanceKey("1002", BANK)], "2026-02", monkeypatch)[
            0
        ]
    assert actual.opening == actual.ending == {"state": "known", "amount_fen": "0"}
    assert {ref["business_id"] for ref in actual.sources} == {ORIGIN, first, second}


def test_empty_corrected_target_ends_inheritance_without_borrowing_old_lines(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), expense("30")], freeze="2026-01")
    corrected = expense("0", seq=1)
    corrected["plans"][0].update(lines=[], obligations=[])
    corrected["plans"][0]["basis"]["input"].pop("report_details")
    save(store, ledger, [corrected], correction="2026-02")
    with store.read_unit(COMPANY) as read:
        output = ledger.get_results([BUSINESS], read)[0]
        assert [row["role"] for row in output["vouchers"]] == ["reversal"]
        actual = _compare(ledger, read, [AccountBalanceKey("2202")], "2026-02", monkeypatch)[0]
        assert actual.opening == actual.ending == {"state": "known", "amount_fen": "0"}
        assert actual.first_recorded_period == "2026-01"


def test_open_replacement_removes_old_account_lines_and_old_first_recorded(store, monkeypatch):
    ledger = create_ledger()
    movement = str(uuid4())
    save(store, ledger, [origin("0"), _bank_journal(movement, 30)])
    changed = _bank_journal(movement, 45, seq=1)
    changed["plans"][0]["lines"][0]["account_code"] = "1101"
    save(store, ledger, [changed])
    with store.read_unit(COMPANY) as read:
        actual = _compare(
            ledger,
            read,
            [AccountBalanceKey("1002", BANK), AccountBalanceKey("1101", BANK)],
            "2026-01",
            monkeypatch,
        )
    assert [row.ending["amount_fen"] for row in actual] == ["0", "45"]
    assert [row.first_recorded_period for row in actual] == [None, "2026-01"]


def test_candidate_lookup_uses_existing_account_index_before_formal_head_selection(
    store, monkeypatch
):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), _bank_journal(str(uuid4()), 30), expense()])
    with store.read_unit(COMPANY) as read:
        session = read.repository("ledger")._session
        fetch = session.fetchall
        plans = []

        def observed(sql, args=()):
            if sql.startswith("WITH candidates AS"):
                plans.extend(str(row[3]) for row in fetch("EXPLAIN QUERY PLAN " + sql, args))
            return fetch(sql, args)

        monkeypatch.setattr(session, "fetchall", observed)
        ledger.account_balances([AccountBalanceKey("1002", BANK)], "2026-01", read)
    assert any("SEARCH l USING INDEX voucher_line_account (account_code=?)" in row for row in plans)


@pytest.mark.parametrize("amount", [None, "0", "100"])
def test_missing_zero_and_nonzero_opening_keep_full_validation(store, monkeypatch, amount):
    ledger = create_ledger()
    if amount is not None:
        save(store, ledger, [origin(amount)])
    with store.read_unit(COMPANY) as read:
        actual = _compare(ledger, read, [AccountBalanceKey("1002", BANK)], "2026-01", monkeypatch)[
            0
        ]
        assert actual.ending["state"] == ("known" if amount is not None else "unestablished")
        if amount is not None:
            assert actual.ending["amount_fen"] == amount


@pytest.mark.parametrize("store", ["2026-04"], indirect=True)
def test_comparison_before_cutover_keeps_unknown_and_exact_opening_boundary(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin("100", period="2026-04")])
    with store.read_unit(COMPANY) as read:
        rows = [
            _compare(ledger, read, [AccountBalanceKey("1002", BANK)], period, monkeypatch)[0]
            for period in ("2026-02", "2026-03", "2026-04")
        ]
    assert rows[0].opening["state"] == rows[0].ending["state"] == "unestablished"
    assert rows[1].opening["state"] == "unestablished"
    assert rows[1].ending == {"state": "known", "amount_fen": "100"}
    assert rows[2].opening == rows[2].ending == rows[1].ending


def test_unrequested_opening_account_still_participates_in_complete_origin_validation(
    store, monkeypatch
):
    ledger = create_ledger()
    save(store, ledger, [origin("100")])
    with store.read_unit(COMPANY) as read:
        repo = type(read.repository("ledger"))
        original = repo.financial_boundaries

        def damaged(*args, **kwargs):
            rows = original(*args, **kwargs)
            body = json.loads(rows[0]["basis_json"])
            equity = next(
                item for item in body["adopted_quantities"] if item.get("account_code") == "3001"
            )
            equity["credit_fen"] = "99"
            return [{**rows[0], "basis_json": json.dumps(body)}]

        monkeypatch.setattr(repo, "financial_boundaries", damaged)
        with pytest.raises(ContractError, match="完整目标总额") as error:
            ledger.account_balances([AccountBalanceKey("1002", BANK)], "2026-01", read)
        assert error.value.code == "storage_corrupt"


def test_named_fields_have_local_issues_exact_values_and_scope_version(store, monkeypatch):
    ledger = create_ledger()
    names = ("fixed_cost", "fixed_accumulated", "fixed_net", "long_term_net")
    item = journal(str(uuid4()), [("1601", 100), ("1602", -20), ("3001", -80)])
    save(store, ledger, [origin("0"), item])
    with store.read_unit(COMPANY) as read:
        summary = ledger.financial_summary(scope(view="current"), read)
        actual = ledger.financial_balances(scope(view="current"), names, read)
        assert actual.values == {name: getattr(summary, name) for name in names}
        assert actual.values["fixed_net"]["amount_fen"] == "80"
        assert not actual.issues
    save(store, ledger, [expense()])
    with store.read_unit(COMPANY) as read:
        repeated = ledger.financial_balances(
            {**scope(view="current"), "expected_head": actual.version}, names, read
        )
        assert repeated == actual
        for invalid in ("bank", ("not_a_field",), ("bank", "bank"), (1,)):
            with pytest.raises(ContractError) as error:
                ledger.financial_balances(scope(), invalid, read)
            assert error.value.code == "invalid_input"
        monkeypatch.setattr(
            type(read.repository("ledger")),
            "financial_heads",
            lambda *a, **kw: pytest.fail("宽取数"),
        )
        assert ledger.financial_balances(scope(view="current"), names, read) == actual
    save(store, ledger, [journal(str(uuid4()), [("1601", 20), ("3001", -20)])])
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            ledger.financial_balances(
                {**scope(view="current"), "expected_head": actual.version}, names, read
            )
        assert error.value.code == "snapshot_changed"


def test_named_fields_unknown_opening_have_no_invented_zero(store):
    names = ("fixed_cost", "fixed_net")
    with store.read_unit(COMPANY) as read:
        actual = create_ledger().financial_balances(scope(), names, read)
    assert len(actual.issues) == 2
    assert actual.values == {
        name: {"state": "unestablished", "issue_indexes": [index]}
        for index, name in enumerate(names)
    }
