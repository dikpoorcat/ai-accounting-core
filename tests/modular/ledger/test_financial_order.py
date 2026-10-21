"""Financial arithmetic order belongs to Ledger, never a repository query plan."""

from copy import deepcopy

import pytest
import test_adoption
from test_adoption import BANK, BUSINESS, COMPANY, SUPPLIER
from test_aggregations import journal
from test_financial_core import origin, save

from ai_accounting_modular.contracts.public import MAX_FEN, ContractError
from ai_accounting_modular.ledger._balance_cube import balance_cube
from ai_accounting_modular.ledger._cashflow import cash
from ai_accounting_modular.ledger._financial_cells import Issues, known
from ai_accounting_modular.ledger._financial_positions import positions
from ai_accounting_modular.ledger._financial_profit import pretax_slices, profit
from ai_accounting_modular.ledger._financial_selection import Selection
from ai_accounting_modular.ledger.public import AccountBalanceKey, create_ledger
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory

FIRST = "50000000-0000-4000-8000-000000000099"
SECOND = "50000000-0000-4000-8000-000000000011"
THIRD = "50000000-0000-4000-8000-000000000022"


@pytest.fixture(params=MODES)
def implementation(request, monkeypatch):
    factory = repository_factory(request.param)
    monkeypatch.setattr(test_adoption, "repository_factory", lambda: factory)


@pytest.fixture
def store(implementation, tmp_path, request):
    return test_adoption.store.__wrapped__(tmp_path, request)


def reverse_results(repo, monkeypatch):
    for name in (
        "financial_heads",
        "adoptions_between",
        "financial_results",
        "financial_lines",
        "children",
        "read_balance_heads",
        "read_balance_lines",
    ):
        original = getattr(repo, name)

        def reversed_rows(*args, _original=original, **kwargs):
            return list(reversed(_original(*args, **kwargs)))

        monkeypatch.setattr(repo, name, reversed_rows)


@pytest.mark.parametrize("view", ["current", "as_posted"])
def test_real_posting_order_keeps_prefix_overflow_when_repository_returns_reverse(
    store, monkeypatch, view
):
    ledger = create_ledger()
    save(
        store,
        ledger,
        [
            origin("0"),
            journal(FIRST, [("5602", MAX_FEN), ("2202", -MAX_FEN)]),
            journal(SECOND, [("5602", 1), ("2202", -1)], period="2026-02"),
            journal(THIRD, [("5602", -1), ("2202", 1)], period="2026-03"),
        ],
    )
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        reverse_results(repo, monkeypatch)
        selected = Selection(repo, view, "2026-03", read.start_period, Issues())
        # Posting Jan->Feb->Mar is MAX,+1,-1, never the business-id order +1,-1,MAX.
        with pytest.raises(ContractError, match="资源累计金额超出"):
            profit(selected, "2026-01", "2026-03", "current")


def _unknown(change, line, code, meaning):
    plan = change["plans"][0]
    plan["report_contributions"].append(
        {
            "statement": "profit",
            "line": line,
            "detail_code": {
                15: "management_startup",
                16: "management_entertainment",
                17: "management_research",
            }[line],
            "state": "unestablished",
            "issue": {
                "code": code,
                "field": f"/report_details/{line}",
                "meaning": meaning,
                "message": meaning + "未确定。",
                "allowed_precision": ["source"],
                "reusable_sources": [],
                "blocking_scope": "none",
            },
        }
    )
    return change


@pytest.mark.parametrize("view", ["current", "as_posted"])
@pytest.mark.parametrize("mixed_codes", [False, True])
def test_real_unknown_first_meaning_and_issue_indexes_are_independent_of_sql_order(
    store, monkeypatch, view, mixed_codes
):
    ledger = create_ledger()
    first = _unknown(
        journal(FIRST, [("5602", 5), ("2202", -5)]),
        15,
        "report_basis_missing" if mixed_codes else "same",
        "一月原义",
    )
    _unknown(first, 16, "entertainment", "一月招待")
    second = _unknown(
        journal(SECOND, [("5602", 7), ("2202", -7)], period="2026-02"),
        15,
        "other" if mixed_codes else "same",
        "二月原义",
    )
    _unknown(second, 17, "research", "二月研发")
    changes = [origin("0"), first, second]
    if mixed_codes:
        changes.append(
            _unknown(
                journal(THIRD, [("5602", 3), ("2202", -3)], period="2026-03"),
                15,
                "report_basis_missing",
                "三月原义",
            )
        )
    save(store, ledger, changes)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        before = Selection(repo, view, "2026-03", read.start_period, Issues())
        expected = profit(before, "2026-01", "2026-03", "current")
        sources = tuple(before.source(before.heads[key]) for key in before.used)
        scope = {
            "company_id": COMPANY,
            "period_from": "2026-01",
            "period_to": "2026-03",
            "view": view,
        }
        snapshot = ledger.financial_snapshot(scope, read)
        reverse_results(repo, monkeypatch)
        selected = Selection(repo, view, "2026-03", read.start_period, Issues())
        actual = profit(selected, "2026-01", "2026-03", "current")
        assert actual == expected
        assert selected.issues.values == before.issues.values
        assert [issue["field"] for issue in selected.issues.values] == [
            "/profit_statement/15/current",
            "/profit_statement/16/current",
            "/profit_statement/17/current",
        ]
        assert selected.issues.values[0]["meaning"] == (
            "报表字段采用依据" if mixed_codes else "一月原义"
        )
        assert {source["result_id"] for source in sources} == selected.used
        assert pretax_slices(selected, "2026-01", "2026-03") == pretax_slices(
            before, "2026-01", "2026-03"
        )
        assert ledger.financial_snapshot(scope, read) == snapshot


def test_frozen_baseline_legs_follow_all_positive_heads_and_preserve_counter_cancellation(
    store, monkeypatch
):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), test_adoption.expense("100")], freeze="2026-01")
    save(store, ledger, [test_adoption.expense("90", seq=1)], correction="2026-02")
    other = journal(SECOND, [("5602", 5), ("2202", -5)], period="2026-02")
    save(store, ledger, [other], freeze="2026-02")
    save(store, ledger, [test_adoption.expense("80", seq=2)], correction="2026-03")
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        january = repo.adoptions_between("2026-01", "2026-01", metadata_only=True)
        original = next(head for head in january if head["business_id"] == BUSINESS)
        heads = repo.adoptions_between("2026-02", "2026-03", metadata_only=True)
        latest = next(head for head in heads if head["posting_period"] == "2026-03")
        other_head = next(head for head in heads if head["business_id"] == SECOND)
        reverse_results(repo, monkeypatch)
        selected = Selection(repo, "as_posted", "2026-03", read.start_period, Issues())
        assert list(selected.flow("2026-02", "2026-03").items()) == [
            (other_head["result_id"], 1),
            (latest["result_id"], 1),
            (original["result_id"], -1),
        ]
        assert profit(selected, "2026-02", "2026-03", "current")[14] == known(-15)


@pytest.mark.parametrize("view", ["current", "as_posted"])
def test_nullable_dimensions_and_party_fold_are_stable_not_first_seen_order(
    store, monkeypatch, view
):
    ledger = create_ledger()
    changes = []
    for business, month, amount, obj in (
        (FIRST, "2026-01", MAX_FEN, BANK),
        (SECOND, "2026-02", 1, None),
        (THIRD, "2026-03", -1, SUPPLIER),
    ):
        change = journal(business, [("2202", amount), ("3001", -amount)], period=month)
        row = change["plans"][0]["lines"][0]
        row["party_id"] = SUPPLIER
        if obj is not None:
            row["object_id"] = obj
        changes.append(change)
    save(store, ledger, [origin("0"), *changes])
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        reverse_results(repo, monkeypatch)
        selected = Selection(repo, view, "2026-03", read.start_period, Issues())
        cube = balance_cube(selected, "2026-03")
        assert cube == {
            ("2202", SUPPLIER, None): 1,
            ("2202", SUPPLIER, SUPPLIER): -1,
            ("2202", SUPPLIER, BANK): MAX_FEN,
            ("3001", None, None): -MAX_FEN,
        }
        result = positions(selected, "2026-03", "ending")
        assert result[5] == known(MAX_FEN) and result[48] == known(MAX_FEN)
        # The aggregate February comparison really exceeds int64; the explicit
        # BANK object stays MAX at both comparisons and must not load other objects.
        balances = ledger.account_balances((AccountBalanceKey("2202", BANK),), "2026-03", read)
        assert balances[0].ending == known(MAX_FEN)


@pytest.mark.parametrize("overflow", [False, True])
def test_real_cash_movements_use_stable_movement_identity_before_prefix_addition(
    store, monkeypatch, overflow
):
    ledger = create_ledger()
    change = journal(FIRST, [("1002", MAX_FEN), ("3001", -MAX_FEN)])
    change["plans"][0]["fund_movements"] = [
        {
            "movement_id": identity,
            "public_slot": f"funds:{index}",
            "funds_account_id": BANK,
            "actual_on": "2026-01-15",
            "direction": "inflow" if signed > 0 else "outflow",
            "amount_fen": str(abs(signed)),
            "cashflow_parts": [{"category": "loan_receipts", "amount_fen": str(abs(signed))}],
        }
        for index, (identity, signed) in enumerate(
            ((FIRST, -1), (SECOND, MAX_FEN), (THIRD, 1))
            if overflow
            else ((FIRST, MAX_FEN), (SECOND, 1), (THIRD, -1)),
            start=1,
        )
    ]
    # Stable movement IDs order SECOND, THIRD, FIRST, independent of the
    # enclosing business order or a reversed child query.
    save(store, ledger, [origin("0"), change])
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        reverse_results(repo, monkeypatch)
        selected = Selection(repo, "as_posted", "2026-01", read.start_period, Issues())
        if overflow:
            with pytest.raises(ContractError, match="资源累计金额超出"):
                cash(selected, "2026-01", "2026-01", "current")
        else:
            assert cash(selected, "2026-01", "2026-01", "current")[14] == known(MAX_FEN)


def test_real_financial_children_and_lines_have_their_actual_stable_identity_order(
    store, monkeypatch
):
    ledger = create_ledger()
    change = test_adoption.expense("10")
    plan = change["plans"][0]
    plan["obligations"].append(deepcopy(plan["obligations"][0]) | {"slot": "z_extra"})
    save(store, ledger, [origin("0"), change])
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        reverse_results(repo, monkeypatch)
        selected = Selection(repo, "current", "2026-01", read.start_period, Issues())
        heads = list(selected.stock("2026-01").values())
        head = next(head for head in heads if head["business_id"] == BUSINESS)
        assert [
            row["public_slot"] for row in selected.children("obligation", [head])[head["result_id"]]
        ] == ["primary", "z_extra"]
        assert [row["line_no"] for row in selected.lines([head])[head["result_id"]]] == [1, 2]


def test_saved_closed_report_does_not_enter_new_live_traversal(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin("100"), test_adoption.expense("10")])
    with store.write_unit(COMPANY) as unit:
        checked = ledger.check_period("2026-01", unit)
        closed = ledger.close_period("2026-01", checked.head, unit)
        expected = closed.report
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        monkeypatch.setattr(repo, "financial_heads", lambda *a, **kw: pytest.fail("重算冻结报告"))
        monkeypatch.setattr(repo, "adoptions_between", lambda *a, **kw: pytest.fail("重扫冻结发生"))
        scope = {
            "company_id": COMPANY,
            "period_from": "2026-01",
            "period_to": "2026-01",
            "view": "as_posted",
        }
        actual = ledger.financial_snapshot(scope, read)
        assert actual["pre_tax"] == expected["pre_tax"]
        assert actual["profit_statement"] == expected["profit_statement"]
        assert actual["selection"]["selection_digest"] == expected["selection"]["selection_digest"]


def _cost_changes(first):
    return [
        journal(identity, [("5401", amount), ("2202", -amount)], period=period)
        for identity, period, amount in (
            (FIRST, "2026-01", first),
            (SECOND, "2026-02", 1),
            (THIRD, "2026-03", -1),
        )
    ]


def test_real_current_pretax_keeps_economic_order_under_reverse_query(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), *_cost_changes(MAX_FEN)])
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        original = repo.current_heads_in_period
        monkeypatch.setattr(repo, "current_heads_in_period", lambda *args: original(*args)[::-1])
        with pytest.raises(ContractError, match="资源累计金额超出"):
            ledger.pre_tax("2026-01", "2026-03", read)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("overflow", [False, True])
def test_candidate_and_adopted_current_pretax_share_economic_target_order(store, reverse, overflow):
    ledger = create_ledger()
    save(store, ledger, [origin("0")])
    changes = _cost_changes(MAX_FEN if overflow else 100)
    if reverse:
        changes.reverse()

    def compare():
        with store.write_unit(COMPANY) as unit:
            for change in changes:
                unit.repository("application").business(change["businesses"][0]["business_id"])
            candidate = ledger.begin_candidate(
                [item for change in changes for item in change["businesses"]],
                unit,
                publications=[item for change in changes for item in change["publications"]],
            )
            for change in changes:
                candidate.stage(change["plans"][0])
            before = candidate.pre_tax("2026-01", "2026-03")
            assert before.fields["operating_cost"] == known(100)
            assert before.fields["profit_before_tax"] == known(-100)
            candidate.complete()
            test_adoption.adopt_declared(ledger, changes, unit)
            assert ledger.pre_tax("2026-01", "2026-03", unit) == before

    if overflow:
        with pytest.raises(ContractError, match="资源累计金额超出"):
            compare()
    else:
        compare()


def test_discovery_month_does_not_reorder_complete_targets_or_replace_adoption_flow(
    store, monkeypatch
):
    ledger = create_ledger()
    first = journal(FIRST, [("5401", 5), ("2202", -5)])
    save(store, ledger, [origin("0"), first], freeze="2026-01")
    second = journal(SECOND, [("5401", 7), ("2202", -7)], period="2026-02")
    changed = journal(FIRST, [("5401", 9), ("2202", -9)])
    changed["businesses"][0].update(expected_seq=1, op="replace")
    save(store, ledger, [second, changed], correction="2026-03")
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        reverse_results(repo, monkeypatch)
        current = Selection(repo, "current", "2026-03", read.start_period, Issues())
        historical = Selection(repo, "as_posted", "2026-03", read.start_period, Issues())
        assert [head["business_id"] for head in current.stock("2026-03").values()] == [
            FIRST,
            SECOND,
        ]
        assert [
            current.heads[key]["business_id"] for key in current.flow("2026-01", "2026-03")
        ] == [FIRST, SECOND]
        assert [
            historical.heads[key]["business_id"] for key in historical.flow("2026-01", "2026-03")
        ] == [SECOND, FIRST]
        assert profit(current, "2026-01", "2026-03", "current")[2] == known(16)
        assert profit(historical, "2026-03", "2026-03", "current")[2] == known(4)
        assert ledger.pre_tax("2026-01", "2026-03", read).fields["operating_cost"] == known(16)
