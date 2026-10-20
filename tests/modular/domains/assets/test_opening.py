"""Sourced opening cards continue real accounting without recreating history."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, EvaluatedSlot, content_digest
from ai_accounting_modular.domains.assets.public import create_assets
from ai_accounting_modular.ledger.opening.public import create_opening
from tests.modular.ledger.opening._support import (
    FIXED,
    INTANGIBLE,
    PARTY,
    assemble,
    fact,
    get,
    member,
    objects,
    opening,
    replace,
    request,
)

from .test_assets import Context


def asset_member(asset_id=FIXED, asset_type="fixed", **changes):
    data = {
        "type": "asset",
        "asset_id": asset_id,
        "asset_type": asset_type,
        "cost_fen": "10001" if asset_type == "fixed" else "10000",
        "accumulated_fen": "3333",
        "completed_months": 1,
        "in_use_period": "2026-05" if asset_type == "fixed" else "2026-06",
        "useful_life_months": 3,
        "residual_fen": "1" if asset_type == "fixed" else "0",
        "benefit_area": "administration",
        "rounding": "floor_final_remainder",
    }
    data.update(changes)
    return member(data)


def asset_opening(*members):
    members = members or (asset_member(), asset_member(INTANGIBLE, "intangible"))
    net = sum(
        int(item["data"]["cost_fen"]) - int(item["data"]["accumulated_fen"]) for item in members
    )
    return opening(
        *members,
        member(
            {
                "type": "equity",
                "equity_kind": "retained_earnings",
                "holder_or_basis_id": PARTY,
                "balance_fen": str(net),
            }
        ),
    )


class OpeningContext(Context):
    def __init__(self, *facts, period_to="2026-08"):
        super().__init__(*facts, start_period="2026-07", period_to=period_to)
        self.objects.update({obj["object_id"]: obj for obj in objects()})
        self.reads = []

    def get_result(self, business_id, result_slot):
        self.reads.append((business_id, result_slot))
        key = business_id, result_slot
        item = self.get_fact(business_id)
        if item is None or item["kind"] != "opening.package":
            return super().get_result(business_id, result_slot)
        if key not in self.results:
            plan = create_opening(asset_accounting=create_assets()).evaluate_slot(
                business_id, result_slot, self
            )
            self.plans[key] = plan
            self.results[key] = EvaluatedSlot(
                {
                    "business_id": business_id,
                    "result_id": str(uuid4()),
                    "result_slot": result_slot,
                    "result_generation": 1,
                    "content_digest": content_digest(plan),
                },
                plan["target"],
                plan["basis"],
            )
        return self.results[key]


def results(app, company, item):
    return {
        result["reference"]["result_slot"]: result
        for result in get(app, company, item["business_id"])["businesses"][0]["results"]
    }


def scrap(asset_id=FIXED, month="2026-07", day="31"):
    return fact(
        "assets.disposal",
        {"type": "scrap", "asset_id": asset_id},
        period=month,
        actual=month + "-" + day,
    )


def test_public_card_and_months_use_one_source_with_exact_tail_and_no_previous_book_reads():
    item = asset_opening()
    context, assets = OpeningContext(item), create_assets()
    cards = assets.opening_asset_cards(item, context)
    assert [(card["consumption_start"], card["accumulated_fen"]) for card in cards] == [
        ("2026-06", "3333"),
        ("2026-06", "3333"),
    ]
    assert all("in_use_on" not in card for card in cards)
    described = assets.describe_opening_assets(item, context)
    assert described["complete_result_slots"] == ["depreciation:2026-07", "depreciation:2026-08"]
    assert (
        len([edge for edge in described["dependencies"] if edge["producer_kind"] == "calendar"])
        == 1
    )
    for month, amount, accumulated in (("2026-07", "6666", "6666"), ("2026-08", "6668", "10000")):
        result = context.get_result(item["business_id"], "depreciation:" + month)
        assert result.target["values"]["total_amount_fen"] == amount
        assert {row["closing_accumulated_fen"] for row in result.target["values"]["items"]} == {
            accumulated
        }
    assert not any(slot == "depreciation:2026-06" for _, slot in context.reads)
    assert context.plans[item["business_id"], "main"]["lines"] == []
    assert all(
        not plan["fund_movements"] and not plan["cost_lots"] for plan in context.plans.values()
    )


def test_fixed_next_month_and_intangible_same_month_at_book_boundary():
    fixed = asset_member(
        cost_fen="100",
        residual_fen="0",
        in_use_period="2026-06",
        useful_life_months=2,
        completed_months=0,
        accumulated_fen="0",
    )
    intangible = asset_member(
        INTANGIBLE, "intangible", cost_fen="100", useful_life_months=2, accumulated_fen="50"
    )
    item = asset_opening(fixed, intangible)
    context = OpeningContext(item)
    july = context.get_result(item["business_id"], "depreciation:2026-07")
    august = context.get_result(item["business_id"], "depreciation:2026-08")
    assert july.target["values"]["total_amount_fen"] == "100"
    assert august.target["values"]["items"] == [
        {
            "asset_id": FIXED,
            "amount_fen": "50",
            "completed_months": 2,
            "closing_accumulated_fen": "100",
            "carrying_fen": "0",
        }
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"accumulated_fen": "3332"},
        {"completed_months": 0},
        {"in_use_on": "2026-04-30"},
        {"in_use_period": "2026-07"},
        {"residual_fen": "10001"},
        {"cost_fen": "2", "residual_fen": "0"},
        {"useful_life_months": 1, "completed_months": 2},
    ],
)
def test_opening_card_rejects_inconsistent_real_history(changes):
    item = asset_opening(asset_member(**changes))
    with pytest.raises(ContractError):
        create_assets().opening_asset_cards(item, OpeningContext(item))


def test_fully_depreciated_card_needs_no_historical_result_to_dispose():
    item = asset_opening(
        asset_member(in_use_period="2026-03", useful_life_months=1, accumulated_fen="10000")
    )
    disposed = scrap()
    context = OpeningContext(item, disposed)
    assert create_assets().describe_opening_assets(item, context)["complete_result_slots"] == []
    result = context.get_result(disposed["business_id"], "main")
    assert result.target["values"]["carrying_fen"] == "1"
    assert result.target["values"]["gain_loss_fen"] == "-1"
    plan = context.plans[disposed["business_id"], "main"]
    assert len(plan["basis"]["source_results"]) == 1
    assert plan["cost_consumptions"] == []
    assert not any(slot.startswith("depreciation:") for _, slot in context.reads)


def test_opening_preserves_explicit_half_up_card_and_its_smaller_final_tail():
    item = asset_opening(
        asset_member(
            cost_fen="10005",
            residual_fen="1",
            accumulated_fen="3335",
            rounding="round_half_up_card",
        )
    )
    context = OpeningContext(item)
    assert (
        context.get_result(item["business_id"], "depreciation:2026-07").target["values"][
            "total_amount_fen"
        ]
        == "3335"
    )
    assert (
        context.get_result(item["business_id"], "depreciation:2026-08").target["values"][
            "total_amount_fen"
        ]
        == "3334"
    )


def test_withdrawn_disposal_restores_necessary_opening_months_without_new_activation(tmp_path):
    app, company = assemble(tmp_path, asset_accounting=create_assets())
    item, disposed, later = asset_opening(), scrap(), scrap(INTANGIBLE, "2026-08")
    saved = app.accounting_apply(request(company, item, disposed, later))
    assert saved["completion"] == "complete", saved
    assert (
        results(app, company, item)["depreciation:2026-08"]["target"]["values"]["total_amount_fen"]
        == "3334"
    )
    command = replace(company, disposed)
    command["mutations"] = [
        {"op": "withdraw", "business_id": disposed["business_id"], "expected_seq": 1}
    ]
    saved = app.accounting_apply(command)
    assert saved["completion"] == "complete", saved
    assert (
        results(app, company, item)["depreciation:2026-08"]["target"]["values"]["total_amount_fen"]
        == "6668"
    )
    withdrawn = get(app, company, disposed["business_id"])["businesses"][0]
    assert withdrawn["state"]["state"] == "withdrawn"
    assert withdrawn["results"] == [] and "fact" not in withdrawn


def test_real_opening_automatically_continues_when_explicit_work_month_advances(tmp_path):
    app, company = assemble(tmp_path, asset_accounting=create_assets())
    item = asset_opening()
    saved = app.accounting_apply(request(company, item))
    assert saved["completion"] == "complete", saved
    assert set(results(app, company, item)) == {"main", "depreciation:2026-07"}
    disposed = scrap(month="2026-08")
    command = request(company, disposed)
    command.pop("objects")
    command.pop("sources")
    saved = app.accounting_apply(command)
    assert saved["completion"] == "complete", saved
    current = results(app, company, item)
    assert current["main"]["vouchers"] == []
    assert set(current) == {"main", "depreciation:2026-07", "depreciation:2026-08"}
    assert current["depreciation:2026-07"]["target"]["values"]["total_amount_fen"] == "6666"
    assert current["depreciation:2026-08"]["target"]["values"]["total_amount_fen"] == "6668"
    assert results(app, company, disposed)["main"]["target"]["values"]["gain_loss_fen"] == "-1"
    assert len(get(app, company, item["business_id"], disposed["business_id"])["businesses"]) == 2


def test_same_unit_cross_owner_disposal_truncates_only_its_card_and_replacement_updates_it(
    tmp_path,
):
    app, company = assemble(tmp_path, asset_accounting=create_assets())
    item, disposed = asset_opening(), scrap()
    # A real later intangible disposal advances work, while the fixed card ends in July.
    later = scrap(INTANGIBLE, "2026-08")
    saved = app.accounting_apply(request(company, disposed, later, item))
    assert saved["completion"] == "complete", saved
    before = results(app, company, item)
    assert before["depreciation:2026-08"]["target"]["values"]["total_amount_fen"] == "3334"
    assert results(app, company, disposed)["main"]["target"]["values"]["gain_loss_fen"] == "-3335"
    fixed = item["data"]["members"][0]["data"]
    fixed.update(cost_fen="13001", accumulated_fen="4333")
    item["data"]["members"][-1]["data"]["balance_fen"] = "15335"
    changed = app.accounting_apply(replace(company, item))
    assert changed["completion"] == "complete", changed
    after = results(app, company, item)
    assert after["depreciation:2026-07"]["target"]["values"]["total_amount_fen"] == "7666"
    assert results(app, company, disposed)["main"]["target"]["values"]["gain_loss_fen"] == "-4335"
    assert (
        after["depreciation:2026-07"]["reference"]["voucher_number"]
        == before["depreciation:2026-07"]["reference"]["voucher_number"]
    )


@pytest.mark.parametrize("operation", ["withdraw", "remove_card"])
def test_opening_cannot_erase_real_disposal_and_failure_keeps_all_current_results(
    tmp_path, operation
):
    app, company = assemble(tmp_path, asset_accounting=create_assets())
    item, disposed = asset_opening(), scrap()
    saved = app.accounting_apply(request(company, item, disposed))
    assert saved["completion"] == "complete", saved
    before = get(app, company, item["business_id"], disposed["business_id"])
    if operation == "withdraw":
        command = replace(company, item)
        command["mutations"] = [
            {"op": "withdraw", "business_id": item["business_id"], "expected_seq": 1}
        ]
    else:
        item["data"]["members"].pop(0)
        item["data"]["members"][-1]["data"]["balance_fen"] = "6667"
        command = replace(company, item)
    failed = app.accounting_apply(command)
    assert failed["completion"] == "not_saved", failed
    assert failed["error"] in {"needs_information", "dependency_conflict"}, failed
    assert get(app, company, item["business_id"], disposed["business_id"]) == before


@pytest.mark.parametrize("duplicate", ["acquisition", "activation"])
def test_opening_cannot_be_acquired_or_activated_again_in_same_unit(tmp_path, duplicate):
    app, company = assemble(tmp_path, asset_accounting=create_assets())
    item = asset_opening()
    acquired = fact(
        "assets.acquisition",
        {
            "type": "direct",
            "asset": {"asset_id": FIXED, "asset_type": "fixed", "cost_fen": "10001"},
            "supplier_id": PARTY,
        },
    )
    duplicate_fact = (
        acquired
        if duplicate == "acquisition"
        else fact(
            "assets.activation",
            {
                "items": [
                    {
                        "asset_id": FIXED,
                        "residual_fen": "1",
                        "useful_life_months": 3,
                        "benefit_area": "administration",
                        "rounding": "floor_final_remainder",
                    }
                ]
            },
        )
    )
    failed = app.accounting_apply(request(company, duplicate_fact, item))
    assert failed["completion"] == "not_saved", failed
    assert failed["error"] == "dependency_conflict", failed
    assert get(app, company, item["business_id"], duplicate_fact["business_id"])["businesses"] == []
