"""Public asset reads use real saved lifecycle results, never today's formula."""

from dataclasses import replace as replace_dataclass
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.domains.assets.public import (
    AssetCardQuery,
    ProjectCostQuery,
    create_assets,
)

from .test_application import assembled as assembled  # real application fixture
from .test_application import replace, request
from .test_assets import bulk_fixed_assets, case


def scope(company, month, *, view="as_posted"):
    return {"company_id": company, "period_from": month, "period_to": month, "view": view}


def owner(app):
    return create_assets(ledger=app.ledger, identity=app.identity)


def activity(app, company, month, *, view="as_posted"):
    # Explicit selection works even when the shared read unit itself uses current.
    with app.storage.read_unit(company) as read:
        return owner(app).asset_activity(scope(company, month, view=view), read)


def save(app, item):
    result = app.accounting_apply(item)
    assert result.get("completion") == "complete", result


def test_month_precision_active_full_depreciation_and_exact_exited_card(assembled):
    app, company = assembled
    acquired, enabled, disposed = case("B20"), case("B21"), case("B22")
    save(app, request(company, acquired, enabled, disposed))
    january = activity(app, company, "2026-01")
    assert january.fixed.month_acquired_count == 1
    assert january.fixed.month_acquired_fen == 10001
    assert january.fixed.month_activated_count == 1
    first = january.cards[0]
    assert first.acquired_on is None and first.in_use_on is None
    assert (first.acquired_period, first.in_use_period) == ("2026-01", "2026-01")
    february = activity(app, company, "2026-02")
    assert (february.cards[0].accumulated_fen, february.cards[0].net_fen) == (3333, 6668)
    assert february.fixed.month_charge_fen == 3333
    april = activity(app, company, "2026-04")
    assert (april.cards[0].state, april.cards[0].completed_months) == ("active", 3)
    assert april.fixed.active_count == 1 and april.fixed.exited_count == 0
    may = activity(app, company, "2026-05")
    exited = may.cards[0]
    assert (exited.state, exited.net_fen, exited.accumulated_fen) == ("exited", 0, 10000)
    assert exited.exit.carrying_fen == 1
    assert exited.exit.gain_loss_fen == -1
    assert may.fixed.month_exited_count == 1 and may.fixed.month_charge_fen == 0
    with app.storage.read_unit(company) as read:
        public = owner(app)
        exact = public.query_cards(
            scope(company, "2026-05"), AssetCardQuery("all", exited.asset_id), PageQuery(1), read
        )
        assert exact.total_count == exact.filtered_count == 1
        assert exact.items == (exited,)
        assert (
            public.query_cards(
                scope(company, "2026-05"),
                AssetCardQuery("active", exited.asset_id),
                PageQuery(1),
                read,
            ).items
            == ()
        )


def test_pending_unactivated_exit_and_current_only_object_labels(assembled):
    app, company = assembled
    acquired, disposed = case("B20"), case("B22")
    save(app, request(company, acquired, disposed))
    pending = activity(app, company, "2026-01")
    assert pending.fixed.pending_count == 1 and pending.fixed.pending_cost_fen == 10001
    assert pending.cards[0].card is None and pending.cards[0].net_fen == 10001
    assert pending.cards[0].payment.amount_fen == 10001
    exited = activity(app, company, "2026-05").cards[0]
    assert exited.activation is None and exited.exit.carrying_fen == 10001 and exited.net_fen == 0
    later = str(uuid4())
    save(
        app,
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [],
            "objects": [
                {
                    "object_id": later,
                    "kind": "asset",
                    "name": "后来登记的设备",
                    "expected_seq": 0,
                    "data": {"asset_type": "fixed", "category": "设备"},
                }
            ],
        },
    )
    historical = activity(app, company, "2026-01")
    extra = next(card for card in historical.cards if card.asset_id == later)
    assert extra.state == "unestablished" and extra.origin is None
    assert extra.cost_fen is None and extra.acquired_period is None


def test_open_cost_change_reads_saved_cards_without_calling_calculators(assembled, monkeypatch):
    app, company = assembled
    acquired, enabled = case("B20"), case("B21")
    future = case("B18")
    future["period"] = "2026-03"
    save(app, request(company, acquired, enabled, future))
    acquired["data"]["asset"]["cost_fen"] = "13001"
    save(app, replace(company, acquired))
    monkeypatch.setattr(
        type(create_assets()), "evaluate_slot", lambda *args: pytest.fail("读取不应核算")
    )
    result = activity(app, company, "2026-03")
    card = result.cards[0]
    assert (card.cost_fen, card.accumulated_fen, card.net_fen) == (13001, 8666, 4335)
    assert card.month_charge_fen == 4333 and card.completed_months == 2


def test_missing_formal_future_charge_is_unknown_instead_of_recomputed(assembled):
    app, company = assembled
    save(app, request(company, case("B20"), case("B21")))
    result = activity(app, company, "2026-02")
    assert result.cards[0].accumulated_fen is None
    assert result.cards[0].net_fen is None and result.fixed.month_charge_fen is None
    assert result.card_issues[0]["code"] == "formal_asset_consumption_missing"


def test_balance_issue_indexes_are_local_and_do_not_mix_with_card_issues(assembled):
    app, company = assembled
    save(app, request(company, case("B20"), case("B21")))
    result = activity(app, company, "2026-02")
    fields = (
        "fixed_cost",
        "fixed_accumulated",
        "fixed_net",
        "intangible_cost",
        "intangible_accumulated",
        "intangible_net",
        "long_term_cost",
        "long_term_accumulated",
        "long_term_net",
    )
    assert tuple(issue["field"] for issue in result.ledger.issues) == tuple(
        "/" + key for key in fields
    )
    for index, field in enumerate(fields):
        assert getattr(result.ledger, field) == {"state": "unestablished", "issue_indexes": [index]}
    assert result.card_issues[0]["code"] == "formal_asset_consumption_missing"
    assert all(
        issue["code"] != "formal_asset_consumption_missing" for issue in result.ledger.issues
    )
    with app.storage.read_unit(company) as read:
        original = app.ledger.financial_summary(scope(company, "2026-02"), read)
    assert original.fixed_cost["issue_indexes"] != result.ledger.fixed_cost["issue_indexes"]
    for field in fields:
        saved_cell, local_cell = getattr(original, field), getattr(result.ledger, field)
        assert [original.issues[index] for index in saved_cell["issue_indexes"]] == [
            result.ledger.issues[index] for index in local_cell["issue_indexes"]
        ]


def test_shared_balance_issues_are_preserved_once_and_unused_issues_are_removed(
    assembled, monkeypatch
):
    app, company = assembled
    with app.storage.read_unit(company) as read:
        names = (
            "fixed_cost",
            "fixed_accumulated",
            "fixed_net",
            "intangible_cost",
            "intangible_accumulated",
            "intangible_net",
            "long_term_cost",
            "long_term_accumulated",
            "long_term_net",
        )
        original = app.ledger.financial_balances(scope(company, "2026-01"), names, read)
    source_indexes = [
        *original.values["fixed_cost"]["issue_indexes"],
        *original.values["fixed_accumulated"]["issue_indexes"],
    ]
    combined = replace_dataclass(
        original,
        values={
            **original.values,
            "fixed_net": {"state": "unestablished", "issue_indexes": source_indexes},
        },
    )
    monkeypatch.setattr(app.ledger, "financial_balances", lambda *_: combined)
    result = activity(app, company, "2026-01")
    assert result.ledger.fixed_net == {"state": "unestablished", "issue_indexes": [0, 1]}
    assert result.ledger.intangible_cost == {"state": "unestablished", "issue_indexes": [2]}
    assert len(result.ledger.issues) == 8
    assert "/fixed_net" not in {issue["field"] for issue in result.ledger.issues}
    assert result.ledger.issues[:2] == tuple(original.issues[index] for index in source_indexes)
    assert combined.values["fixed_net"]["issue_indexes"] == source_indexes


def test_batch_obligations_are_not_allocated_to_cards_by_their_cost(assembled):
    app, company = assembled
    batch = case("B20R")
    save(app, request(company, batch))
    result = activity(app, company, "2026-01")
    assert sorted(card.cost_fen for card in result.cards) == [10000, 20000]
    for card in result.cards:
        assert card.payment.scope == "batch"
        assert card.payment.obligation_count == 2
        assert card.payment.amount_fen == card.payment.remaining_fen == 30000
        assert card.payment.paid_fen == card.payment.other_settled_fen == 0


def test_project_cost_partial_consumption_and_pending_intangible_are_separate(assembled):
    app, company = assembled
    cost, acquired = case("B18"), case("B20P")
    acquired["data"]["asset"]["cost_fen"] = "40000"
    acquired["data"]["sources"][0]["amount_fen"] = "40000"
    save(app, request(company, cost, acquired))
    result = activity(app, company, "2026-01")
    assert result.intangible.pending_count == 1 and result.intangible.pending_cost_fen == 40000
    assert result.project_totals.cost_fen == 100000
    assert result.project_totals.consumed_fen == 40000
    assert result.project_totals.remaining_fen == 60000
    assert result.cards[0].payment.scope == "project_sources"
    assert result.cards[0].payment.amount_fen == 100000
    with app.storage.read_unit(company) as read:
        page = owner(app).query_project_costs(
            scope(company, "2026-01"),
            ProjectCostQuery(cost["data"]["project_id"]),
            PageQuery(1),
            read,
        )
        assert page.page.total_count == page.page.filtered_count == 1
        assert page.full_totals == page.filtered_totals == result.project_totals
    release = case("B19")
    release["data"]["sources"][0]["amount_fen"] = "60000"
    save(
        app,
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [{"op": "create", "expected_seq": 0, "fact": release}],
        },
    )
    with app.storage.read_unit(company) as read:
        public = owner(app)
        hidden = public.query_project_costs(
            scope(company, "2026-01"), ProjectCostQuery(), PageQuery(), read
        )
        assert hidden.page.total_count == 1 and hidden.page.filtered_count == 0
        assert hidden.full_totals.cost_fen == 100000 and hidden.full_totals.remaining_fen == 0
        complete = public.query_project_costs(
            scope(company, "2026-01"), ProjectCostQuery(include_consumed=True), PageQuery(), read
        )
        assert complete.page.filtered_count == 1 and complete.page.items[0].consumed_fen == 100000


@pytest.mark.parametrize("view", ["as_posted", "current"])
def test_project_costs_capture_the_complete_range_once(assembled, monkeypatch, view):
    app, company = assembled
    save(app, request(company, case("B18")))
    capture = app.ledger.capture_cost_positions
    calls = []

    def captured(query, read, *, selection):
        result = capture(query, read, selection=selection)
        calls.append((query.categories, selection.view, selection.as_of, result.filtered_count))
        return result

    monkeypatch.setattr(app.ledger, "capture_cost_positions", captured)
    monkeypatch.setattr(
        app.ledger, "query_cost_positions", lambda *_args, **_kwargs: pytest.fail("paged positions")
    )
    result = activity(app, company, "2026-01", view=view)
    assert result.project_totals.count == len(result.project_costs) == 1
    assert result.project_totals.cost_fen == result.project_totals.remaining_fen == 100000
    assert calls == [(("project",), view, "2026-01" if view == "as_posted" else None, 1)]


def test_501_cards_have_complete_activity_and_bounded_exact_pagination(assembled):
    app, company = assembled
    batch, enabled = bulk_fixed_assets(501)
    initial = request(company, batch, enabled)
    objects = initial.pop("objects")
    for start in range(0, len(objects), 500):
        save(
            app,
            {
                "contract": "ai-accounting-modular/1",
                "company_id": company,
                "command_id": str(uuid4()),
                "mutations": [],
                "objects": objects[start : start + 500],
            },
        )
    save(app, initial)
    result = activity(app, company, "2026-01")
    assert len(result.cards) == result.fixed.active_count == 501
    assert result.fixed.active_cost_fen == 50100
    with app.storage.read_unit(company) as read:
        public = owner(app)
        first = public.query_cards(
            scope(company, "2026-01"), AssetCardQuery("active"), PageQuery(500), read
        )
        assert first.total_count == first.filtered_count == 501
        assert len(first.items) == 500 and first.next_cursor
        last = public.query_cards(
            scope(company, "2026-01"),
            AssetCardQuery("active"),
            PageQuery(500, first.next_cursor, first.version),
            read,
        )
        assert len(last.items) == 1 and last.next_cursor is None
        assert len({card.asset_id for card in (*first.items, *last.items)}) == 501
        with pytest.raises(ContractError) as error:
            public.query_cards(
                scope(company, "2026-01"),
                AssetCardQuery("all"),
                PageQuery(500, first.next_cursor),
                read,
            )
        assert error.value.code == "snapshot_changed"


def test_uninjected_reader_and_invalid_scope_reject_explicitly(assembled):
    app, company = assembled
    with app.storage.read_unit(company) as read:
        with pytest.raises(ContractError) as error:
            create_assets().asset_activity(scope(company, "2026-01"), read)
        assert error.value.code == "unsupported_business"
        with pytest.raises(ContractError) as error:
            owner(app).asset_activity(scope(str(uuid4()), "2026-01"), read)
        assert error.value.code == "invalid_input"
