"""Frozen corrections, opening cards and real money retain their own meanings."""

from copy import deepcopy
from uuid import uuid4

from ai_accounting_modular.contracts.public import PageQuery
from ai_accounting_modular.domains.assets.public import ProjectCostQuery, create_assets
from tests.modular.application._working_case import case as case
from tests.modular.ledger.opening._support import FIXED, INTANGIBLE, assemble
from tests.modular.ledger.opening._support import request as opening_request

from .test_assets import case as asset_case
from .test_opening import asset_member, asset_opening
from .test_reading import activity, owner, save, scope


def _fact(case, kind, data, month="2026-01", actual=None):
    result = {
        "business_id": str(uuid4()),
        "kind": kind,
        "period": month,
        "evidence_ids": [case.source_id],
        "data": deepcopy(data),
    }
    if actual is not None:
        result["effective_on"] = actual
    return result


def _asset(case, *, amount="120000", activate=False):
    acquired, enabled = asset_case("B20"), asset_case("B21")
    acquired["evidence_ids"] = enabled["evidence_ids"] = [case.source_id]
    acquired["data"]["supplier_id"] = case.party
    acquired["data"]["asset"]["cost_fen"] = amount
    enabled["data"]["items"][0].update(useful_life_months=12, residual_fen="0")
    obj = {
        "object_id": acquired["data"]["asset"]["asset_id"],
        "kind": "asset",
        "name": "合成设备",
        "expected_seq": 0,
        "data": {"asset_type": "fixed", "category": "设备"},
    }
    save(case.app, case.request(*([acquired, enabled] if activate else [acquired]), objects=[obj]))
    return acquired, enabled


def test_closed_cost_adjustment_changes_stock_without_inventing_a_new_card(case):
    case.seed(expense=False)
    acquired, enabled = _asset(case, activate=True)
    save(case.app, case.request(case.expense("1000", period="2026-02")))
    assert case.close(case.check("2026-02"))["state"] == "closed"
    changed = deepcopy(acquired)
    changed["data"]["asset"]["cost_fen"] = "132000"
    command = case.request(correction_period="2026-03")
    command["mutations"] = [{"op": "replace", "expected_seq": 1, "fact": changed}]
    save(case.app, command)
    january = activity(case.app, case.company, "2026-01")
    assert january.cards[0].cost_fen == 120000
    assert january.fixed.month_acquired_count == 1 and january.fixed.month_cost_adjustment_fen == 0
    march = activity(case.app, case.company, "2026-03")
    assert march.fixed.month_acquired_count == 0 and march.fixed.month_acquired_fen == 0
    assert march.fixed.month_cost_adjustment_fen == 12000
    assert march.cards[0].cost_fen == 132000 and march.cards[0].accumulated_fen == 22000
    # March adopts 11,000 of its own charge plus the frozen February correction of 1,000.
    assert march.cards[0].month_charge_fen == march.fixed.month_charge_fen == 12000
    assert january.cards[0].origin["result_id"] != march.cards[0].origin["result_id"]
    assert activity(case.app, case.company, "2026-01", view="current").cards[0].cost_fen == 132000
    with case.kernel.storage.read_unit(case.company) as read:
        public = owner(case.app)
        current = public.asset_activity(scope(case.company, "2026-02", view="current"), read)
        historical = public.asset_activity(scope(case.company, "2026-02"), read)
        assert current.cards[0].accumulated_fen == 11000
        assert historical.cards[0].accumulated_fen == 10000
        assert read.view == "current" and read.as_of is None


def test_ordinary_expense_keeps_the_entire_asset_activity_equal(case):
    case.seed(expense=False)
    _asset(case, activate=True)
    before = activity(case.app, case.company, "2026-01")
    save(case.app, case.request(case.expense("700")))
    after = activity(case.app, case.company, "2026-01")
    assert before == after
    assert before.ledger.fixed_cost == {"state": "known", "amount_fen": "120000"}
    assert not hasattr(before.ledger, "net_profit")


def test_open_paid_asset_correction_keeps_real_paid_and_negative_other_settled(case):
    case.seed(expense=False)
    acquired, _ = _asset(case, amount="100000")
    cash = str(uuid4())
    paid = _fact(
        case,
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": cash,
            "direction": "outflow",
            "amount_fen": "100000",
            "payment_method": "individual",
            "counterparty_id": case.party,
            "allocations": [
                {
                    "source": {"business_id": acquired["business_id"], "slot": "primary"},
                    "amount_fen": "100000",
                }
            ],
        },
        actual="2026-01-20",
    )
    save(
        case.app,
        case.request(
            paid,
            objects=[
                {
                    "object_id": cash,
                    "kind": "cash_account",
                    "name": "合成现金",
                    "expected_seq": 0,
                    "data": {"currency": "CNY"},
                }
            ],
        ),
    )
    final = deepcopy(acquired)
    final["data"]["asset"]["cost_fen"] = "80000"
    claim = _fact(
        case,
        "operations.overpayment",
        {
            "source": {"business_id": acquired["business_id"], "slot": "primary"},
            "counterparty_id": case.party,
            "amount_fen": "20000",
        },
    )
    command = case.request(claim)
    command["mutations"].append({"op": "replace", "expected_seq": 1, "fact": final})
    save(case.app, command)
    card = activity(case.app, case.company, "2026-01").cards[0]
    assert card.cost_fen == card.payment.amount_fen == 80000
    assert card.payment.paid_fen == 100000
    assert card.payment.other_settled_fen == -20000
    assert card.payment.remaining_fen == 0


def test_closed_asset_type_correction_groups_each_formal_leg_by_its_adopted_type(case):
    case.seed(expense=False)
    acquired, _ = _asset(case, activate=True)
    save(case.app, case.request(case.expense("1000", period="2026-02")))
    assert case.close(case.check("2026-02"))["state"] == "closed"
    final = deepcopy(acquired)
    final["data"]["asset"].update(asset_type="intangible", cost_fen="132000")
    command = case.request(
        correction_period="2026-03",
        objects=[
            {
                "object_id": acquired["data"]["asset"]["asset_id"],
                "kind": "asset",
                "name": "合成软件",
                "expected_seq": 1,
                "data": {"asset_type": "intangible", "category": "软件"},
            }
        ],
    )
    command["mutations"] = [{"op": "replace", "expected_seq": 1, "fact": final}]
    save(case.app, command)
    march = activity(case.app, case.company, "2026-03")
    assert march.fixed.month_acquired_count == march.intangible.month_acquired_count == 0
    assert march.fixed.month_cost_adjustment_fen == -120000
    assert march.intangible.month_cost_adjustment_fen == 132000
    assert march.fixed.month_charge_fen == -10000
    assert march.intangible.month_charge_fen == 33000
    assert march.cards[0].month_charge_fen == 23000
    assert march.cards[0].asset_type == "intangible" and march.cards[0].accumulated_fen == 33000
    january = activity(case.app, case.company, "2026-01")
    assert january.cards[0].asset_type == "fixed" and january.cards[0].cost_fen == 120000


def test_capitalized_labor_and_partial_project_sources_are_counted_before_paging(case):
    case.seed(expense=False)
    project, person, intangible = str(uuid4()), str(uuid4()), str(uuid4())
    cost = _fact(
        case,
        "assets.project_cost",
        {
            "project_id": project,
            "project_nature": "purchased_intangible",
            "supplier_id": case.party,
            "amount_fen": "30000",
        },
    )
    labor = _fact(
        case,
        "people.remuneration",
        {
            "type": "labor",
            "person_id": person,
            "gross_fen": "60000",
            "destination": {
                "type": "project",
                "project_id": project,
                "project_nature": "purchased_intangible",
            },
            "withholding_state": "not_withheld_not_filed",
        },
    )
    completion = _fact(
        case,
        "assets.acquisition",
        {
            "type": "project_completion",
            "asset": {"asset_id": intangible, "asset_type": "intangible", "cost_fen": "40000"},
            "project_id": project,
            "sources": [
                {
                    "source": {"business_id": cost["business_id"], "slot": "cost"},
                    "amount_fen": "10000",
                },
                {
                    "source": {"business_id": labor["business_id"], "slot": "cost"},
                    "amount_fen": "30000",
                },
            ],
        },
    )
    save(
        case.app,
        case.request(
            cost,
            labor,
            completion,
            objects=[
                {
                    "object_id": project,
                    "kind": "project",
                    "name": "合成软件项目",
                    "expected_seq": 0,
                    "data": {},
                },
                {
                    "object_id": person,
                    "kind": "person",
                    "name": "合成劳务人员",
                    "expected_seq": 0,
                    "data": {},
                },
                {
                    "object_id": intangible,
                    "kind": "asset",
                    "name": "合成软件",
                    "expected_seq": 0,
                    "data": {"asset_type": "intangible", "category": "软件"},
                },
            ],
        ),
    )
    result = activity(case.app, case.company, "2026-01")
    assert result.project_totals.cost_fen == 90000 and result.project_totals.remaining_fen == 50000
    assert {item.cost_type for item in result.project_costs} == {"project_cost", "labor"}
    assert result.cards[0].payment.amount_fen == 90000
    assert result.cards[0].payment.obligation_count == 2
    with case.kernel.storage.read_unit(case.company) as read:
        page = owner(case.app).query_project_costs(
            scope(case.company, "2026-01"), ProjectCostQuery(project), PageQuery(1), read
        )
        assert page.page.total_count == page.page.filtered_count == 2
        assert len(page.page.items) == 1 and page.page.next_cursor
        assert (
            page.filtered_totals.cost_fen == 90000 and page.filtered_totals.remaining_fen == 50000
        )


def test_fully_depreciated_opening_card_stays_active_without_synthetic_charge(tmp_path):
    app, company = assemble(tmp_path, asset_accounting=create_assets())
    item = asset_opening(
        asset_member(
            cost_fen="12000",
            accumulated_fen="12000",
            completed_months=12,
            useful_life_months=12,
            residual_fen="0",
            in_use_period="2025-06",
        )
    )
    save(app, opening_request(company, item))
    result = activity(app, company, "2026-07")
    card = next(card for card in result.cards if card.asset_id == FIXED)
    assert card.state == "active" and card.completed_months == 12
    assert card.accumulated_fen == 12000 and card.net_fen == card.month_charge_fen == 0
    assert (
        card.acquired_period is None and card.in_use_period == "2025-06" and card.in_use_on is None
    )
    assert card.consumption is None and card.payment.scope == "opening"
    assert card.payment.paid_fen is None
    assert result.fixed.active_count == 1 and result.fixed.month_acquired_count == 0
    assert (
        next(card for card in result.cards if card.asset_id == INTANGIBLE).state == "unestablished"
    )
