"""Fixed numeric oracles, explicit sources, dates, and stable asset identities."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    EvaluatedSlot,
    ResourceDefinition,
    content_digest,
    validate_contract,
)
from ai_accounting_modular.domains.assets.public import create_assets

_CASES = json.loads(
    (Path(__file__).parents[4] / "docs/redesign/examples/business-facts-cases.json").read_text(
        encoding="utf-8"
    )
)
CASES = {item["case_id"]: item for item in _CASES["valid_cases"]}
NEGATIVES = {item["case_id"]: item for item in _CASES["negative_cases"]}
ASSET = CASES["B20"]["when"]["data"]["asset"]["asset_id"]
SOURCE = CASES["B20"]["when"]["evidence_ids"][0]


def case(name):
    return deepcopy(CASES[name]["when"])


class Context:
    """Input transport and result binding only; no asset amount formula here."""

    read_context = None

    def __init__(self, *facts, period_to="2026-05"):
        self.evaluation_period_to = period_to
        self.facts = {fact["business_id"]: deepcopy(fact) for fact in facts}
        self.objects, self.results, self.plans = {}, {}, {}
        for fact in facts:
            data = fact["data"]
            for field in ("supplier_id", "buyer_id"):
                if field in data:
                    self.objects[data[field]] = {"kind": "organization"}
            if "project_id" in data:
                self.objects[data["project_id"]] = {"kind": "project"}
            for item in data.get("creditors", ()):
                self.objects[item["person_id"]] = {"kind": "person"}
            for item in self.asset_items(fact):
                if "asset_type" in item:
                    self.objects[item["asset_id"]] = {
                        "kind": "asset",
                        "data": {"asset_type": item["asset_type"]},
                    }

    @staticmethod
    def asset_items(fact):
        data = fact["data"]
        if fact["kind"] == "assets.acquisition":
            return data.get("assets", [data["asset"]] if "asset" in data else [])
        if fact["kind"] == "assets.activation":
            return data["items"]
        if fact["kind"] == "assets.disposal":
            return [data]
        return []

    def get_fact(self, business_id):
        return deepcopy(self.facts.get(business_id))

    def get_object(self, object_id):
        return deepcopy(self.objects.get(object_id))

    def related_facts(self, producer_id, relation_kind, *, producer_kind="business", **kwargs):
        return self.related_facts_many((producer_id,), relation_kind, producer_kind=producer_kind)

    def related_facts_many(
        self, producer_ids, relation_kind, *, producer_kind="business", **kwargs
    ):
        assert producer_kind == "object" and relation_kind == "identity"
        assert len(producer_ids) <= 500
        requested = set(producer_ids)
        return tuple(
            deepcopy(fact)
            for fact in self.facts.values()
            if any(item["asset_id"] in requested for item in self.asset_items(fact))
        )

    def resolve_sources(self, ids):
        return [{"source_id": value} for value in ids]

    def get_result(self, business_id, result_slot):
        key = business_id, result_slot
        if key not in self.results:
            fact = self.get_fact(business_id)
            if fact is None:
                return None
            if fact["kind"] == "taxes.used_asset_vat_policy":
                kind = fact["kind"]
                identity = fact["data"]["policy"]
                target = {
                    "kind": kind,
                    "result_slot": "main",
                    "values": {
                        "policy_version": identity["version"],
                        "effective_from": identity["effective_from"],
                        "official_url": identity["official_url"],
                    },
                }
                basis = {
                    "kind": kind,
                    "result_slot": "main",
                    "period": fact["period"],
                    "input": deepcopy(fact["data"]),
                    "source_results": [],
                    "adopted_quantities": [],
                    "policies": [],
                    "source_locations": self.resolve_sources(fact["evidence_ids"]),
                }
                plan = {"target": target, "basis": basis}
            else:
                plan = create_assets().evaluate_slot(business_id, result_slot, self)
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

    def resource_definitions(self, refs, kind):
        assert kind == "cost"
        assert len(refs) <= 500
        result = []
        sources = {
            business_id: self.get_result(business_id, "main")
            for business_id in {ref["business_id"] for ref in refs}
        }
        lots = {
            business_id: {lot["slot"]: lot for lot in self.plans[business_id, "main"]["cost_lots"]}
            for business_id in sources
        }
        for ref in refs:
            evaluated = sources[ref["business_id"]]
            lot = lots[ref["business_id"]].get(ref["slot"])
            if lot is not None:
                result.append(
                    ResourceDefinition("cost", deepcopy(ref), evaluated.source, deepcopy(lot))
                )
        return tuple(result)


def evaluate(fact, *sources, period_to="2026-05", slot="main"):
    context = Context(*sources, fact, period_to=period_to)
    plan = create_assets().evaluate_slot(fact["business_id"], slot, context)
    assert sum(int(row["debit_fen"]) for row in plan["lines"]) == sum(
        int(row["credit_fen"]) for row in plan["lines"]
    )
    return plan


@pytest.mark.parametrize("name", ["B18", "B19", "B20", "B20P", "B20R", "B21", "B22"])
def test_published_examples(name):
    fact = case(name)
    plan = evaluate(fact, case("B18"), case("B20"), case("B21"))
    validate_contract("business-facts.schema.json", "PostingPlan", plan)
    expected = CASES[name]["then"]
    for key, value in expected.items():
        if key in plan["target"]["values"]:
            assert plan["target"]["values"][key] == value
    if name == "B20R":
        assert [row["amount_fen"] for row in plan["obligations"]] == ["12000", "18000"]
        assert sum(int(row["debit_fen"]) for row in plan["lines"]) == 30000
        assert len(plan["cost_lots"]) == 2
    assert plan["fund_movements"] == []


def test_fixed_next_month_exact_tail_and_no_fifth_month():
    context = Context(case("B20"), case("B21"))
    business_id = case("B21")["business_id"]
    for month, expected, accumulated in [
        ("2026-02", "3333", "3333"),
        ("2026-03", "3333", "6666"),
        ("2026-04", "3334", "10000"),
    ]:
        result = context.get_result(business_id, "depreciation:" + month)
        assert result.target["values"]["total_amount_fen"] == expected
        assert result.target["values"]["items"][0]["closing_accumulated_fen"] == accumulated
    with pytest.raises(ContractError):
        context.get_result(business_id, "depreciation:2026-05")


def intangible():
    acquired, enabled = case("B20"), case("B21")
    acquired["data"]["asset"].update(asset_type="intangible", cost_fen="10000")
    enabled["data"]["items"][0].update(residual_fen="0", useful_life_months=3)
    return acquired, enabled


def test_intangible_starts_same_month_and_final_fen():
    acquired, enabled = intangible()
    context = Context(acquired, enabled)
    for month, amount in [("2026-01", "3333"), ("2026-02", "3333"), ("2026-03", "3334")]:
        result = context.get_result(enabled["business_id"], "depreciation:" + month)
        assert result.target["values"]["total_amount_fen"] == amount
    assert result.target["values"]["items"][0]["carrying_fen"] == "0"


def test_known_acquired_date_compares_day_but_unknown_does_not_get_fake_day():
    acquired, enabled = case("B20"), case("B21")
    acquired["effective_on"] = "2026-01-28"
    enabled["data"]["items"][0]["in_use_on"] = "2026-01-02"
    with pytest.raises(ContractError):
        evaluate(enabled, acquired)
    del enabled["data"]["items"][0]["in_use_on"]
    assert "in_use_on" not in evaluate(enabled, acquired)["basis"]["input"]["items"][0]


@pytest.mark.parametrize("change", ["duplicate", "residual", "rounding", "early", "date"])
def test_activation_rejects_false_or_impossible_cards(change):
    acquired, enabled = intangible()
    item = enabled["data"]["items"][0]
    if change == "duplicate":
        enabled["data"]["items"].append(deepcopy(item))
    elif change == "residual":
        item["residual_fen"] = "1"
    elif change == "rounding":
        item["rounding"] = "round_half_up_card"
    elif change == "early":
        enabled["period"] = "2025-12"
    else:
        item["in_use_on"] = "2026-02-01"
    with pytest.raises(ContractError):
        evaluate(enabled, acquired)


def test_half_up_card_rejects_early_exhaustion_and_uses_tail():
    acquired, enabled = case("B20"), case("B21")
    acquired["data"]["asset"]["cost_fen"] = "10001"
    enabled["data"]["items"][0].update(
        rounding="round_half_up_card", residual_fen="0", useful_life_months=3
    )
    context = Context(acquired, enabled)
    assert [
        context.get_result(enabled["business_id"], "depreciation:" + month).target["values"][
            "total_amount_fen"
        ]
        for month in ("2026-02", "2026-03", "2026-04")
    ] == ["3334", "3334", "3333"]
    acquired["data"]["asset"]["cost_fen"] = "6"
    enabled["data"]["items"][0]["useful_life_months"] = 4
    with pytest.raises(ContractError):
        evaluate(enabled, acquired)


def test_project_completion_never_creates_second_supplier_debt():
    cost, acquired = case("B18"), case("B20P")
    plan = evaluate(acquired, cost)
    assert plan["obligations"] == []
    assert plan["cost_consumptions"][0]["amount_fen"] == "100000"
    assert plan["lines"][1]["account_code"] == "189901"


def test_internal_development_release_uses_development_cost_not_inventory():
    cost, released = case("B18"), case("B19")
    cost["data"]["project_nature"] = "internal_development"
    plan = evaluate(released, cost)
    assert plan["lines"][1]["account_code"] == "4301"


def test_wrong_project_and_duplicate_cost_source_rejected():
    cost, released = case("B18"), case("B19")
    released["data"]["project_id"] = str(uuid4())
    with pytest.raises(ContractError):
        evaluate(released, cost)
    released = case("B19")
    released["data"]["sources"].append(deepcopy(released["data"]["sources"][0]))
    with pytest.raises(ContractError):
        evaluate(released, cost)


def test_batch_total_disagreement_and_repeated_asset_rejected():
    fact = deepcopy(NEGATIVES["N14"]["when"])
    with pytest.raises(ContractError):
        evaluate(fact)
    fact = case("B20R")
    fact["data"]["assets"][1]["asset_id"] = fact["data"]["assets"][0]["asset_id"]
    with pytest.raises(ContractError):
        evaluate(fact)


def test_disposal_truncates_continuous_months_and_has_no_cash():
    acquired, enabled, disposed = case("B20"), case("B21"), case("B22")
    disposed.update(period="2026-03", effective_on="2026-03-12")
    plan = evaluate(disposed, acquired, enabled)
    assert plan["target"]["values"]["accumulated_fen"] == "6666"
    assert plan["target"]["values"]["gain_loss_fen"] == "-3335"
    assert plan["fund_movements"] == []
    with pytest.raises(ContractError):
        evaluate(enabled, acquired, disposed, slot="depreciation:2026-04")


def test_intangible_scrap_only_real_month_end_no_income():
    acquired, enabled = intangible()
    disposed = case("B22")
    disposed.update(period="2026-02", effective_on="2026-02-28")
    assert evaluate(disposed, acquired, enabled)["target"]["values"]["accumulated_fen"] == "6666"
    disposed["effective_on"] = "2026-02-27"
    with pytest.raises(ContractError):
        evaluate(disposed, acquired, enabled)


def test_premature_month_scope_never_books_future_life():
    enabled, acquired = case("B21"), case("B20")
    with pytest.raises(ContractError):
        evaluate(enabled, acquired, period_to="2026-01", slot="depreciation:2026-02")


def test_closed_monthly_basis_contains_values_not_only_a_pointer():
    enabled, acquired = case("B21"), case("B20")
    plan = evaluate(enabled, acquired, slot="depreciation:2026-03")
    card = plan["basis"]["adopted_cards"][0]
    assert card["cost_fen"] == "10001"
    assert card["accumulated_fen"] == "3333"
    assert card["completed_months"] == 1
    assert card["useful_life_months"] == 3
    assert len(plan["basis"]["source_results"]) == 2


def test_asset_account_cannot_be_used_as_supplier_or_card():
    acquired = case("B20")
    context = Context(acquired)
    context.objects[acquired["data"]["supplier_id"]] = {"kind": "bank_account"}
    with pytest.raises(ContractError, match="角色"):
        create_assets().evaluate_slot(acquired["business_id"], "main", context)


def test_sale_reduced_vat_retains_tax_sales_and_actual_gross_receivable():
    acquired, enabled, sold, policy = case("B20"), case("B21"), case("B22"), case("B37")
    sold["data"].update(
        type="sale",
        buyer_id=str(uuid4()),
        gross_proceeds_fen="10300",
        used_asset_vat_policy_id=policy["business_id"],
        exemption_eligible=False,
        invoice_kind="ordinary",
    )
    plan = evaluate(sold, acquired, enabled, policy)
    values = plan["target"]["values"]
    assert values["gross_proceeds_fen"] == "10300"
    assert values["net_proceeds_fen"] == "10100"
    assert values["vat_fen"] == "200"
    assert values["gain_loss_fen"] == "10099"
    assert plan["obligations"][0]["amount_fen"] == "10300"
    assert plan["tax_sales"][0]["net_sales_fen"] == "10000"
    assert plan["fund_movements"] == []


def test_unactivated_scrap_consumes_pending_cost_instead_of_faking_activation():
    acquired, disposed = case("B20"), case("B22")
    plan = evaluate(disposed, acquired)
    assert plan["target"]["values"]["accumulated_fen"] == "0"
    assert plan["target"]["values"]["gain_loss_fen"] == "-10001"
    assert plan["cost_consumptions"][0]["amount_fen"] == "10001"
    assert {row["account_code"] for row in plan["lines"]} == {"1604", "571101"}


def test_description_uses_own_submission_overlay_and_only_entered_calendar_months():
    acquired, enabled = case("B20"), case("B21")
    context = Context(acquired, enabled, period_to="2026-03")
    # No persisted dependency exists for either newly submitted fact.
    context.related_facts_many = lambda *args, **kwargs: ()
    changes = [{"op": "create", "expected_seq": 0, "fact": fact} for fact in (enabled, acquired)]
    description = create_assets().describe_change(changes, context)
    responsibility = description["businesses"][0]
    assert responsibility["complete_result_slots"] == [
        "main",
        "depreciation:2026-02",
        "depreciation:2026-03",
    ]
    assert responsibility["natural_key"].endswith(ASSET)
    calendar = [edge for edge in description["dependencies"] if edge["producer_kind"] == "calendar"]
    assert len(calendar) == 1
    assert calendar[0]["period_to"] == "2026-04"
    assert any(
        edge["producer_id"] == acquired["business_id"] and edge["read_mode"] == "result"
        for edge in description["dependencies"]
    )
    assert any(
        edge["consumer_result_slot"] == "depreciation:2026-03"
        and edge["producer_public_slot"] == "depreciation:2026-02"
        for edge in description["dependencies"]
    )


def test_two_cards_are_details_under_one_grouped_monthly_journal():
    batch, enabled = case("B20R"), case("B21")
    enabled["data"]["items"] = [
        {
            "asset_id": item["asset_id"],
            "useful_life_months": 3,
            "residual_fen": "0",
            "benefit_area": "administration",
            "rounding": "floor_final_remainder",
        }
        for item in batch["data"]["assets"]
    ]
    plan = evaluate(enabled, batch, slot="depreciation:2026-02")
    assert plan["target"]["values"]["total_amount_fen"] == "9999"
    assert {row["asset_id"]: row["amount_fen"] for row in plan["target"]["values"]["items"]} == {
        batch["data"]["assets"][0]["asset_id"]: "3333",
        batch["data"]["assets"][1]["asset_id"]: "6666",
    }
    assert len(plan["lines"]) == 2
    assert [row["debit_fen"] for row in plan["lines"]] == ["9999", "0"]
    main = evaluate(enabled, batch)
    assert len(main["lines"]) == 2
    assert len(main["basis"]["adopted_quantities"]) == 2
    assert all(item["type"] == "asset_card" for item in main["basis"]["adopted_quantities"])


def test_composite_asset_cost_overflow_is_rejected_without_float_rounding():
    fact = case("B20R")
    for item in fact["data"]["assets"]:
        item["cost_fen"] = str(2**63 - 1)
    with pytest.raises(ContractError, match="64位"):
        evaluate(fact)


def test_reordering_asset_creditor_and_activation_sets_does_not_change_formal_plan():
    batch = case("B20R")
    context = Context(batch)
    first = create_assets().evaluate_slot(batch["business_id"], "main", context)
    context.facts[batch["business_id"]]["data"]["assets"].reverse()
    context.facts[batch["business_id"]]["data"]["creditors"].reverse()
    second = create_assets().evaluate_slot(batch["business_id"], "main", context)
    assert first == second


def test_missing_asset_issue_uses_actual_submitted_json_pointer():
    acquired = case("B20")
    context = Context(acquired)
    context.objects.pop(acquired["data"]["asset"]["asset_id"])
    with pytest.raises(ContractError) as caught:
        create_assets().evaluate_slot(acquired["business_id"], "main", context)
    assert caught.value.fact_issues[0]["field"] == "/data/asset/asset_id"


def test_mixed_fixed_and_intangible_batch_keeps_both_start_and_end_rules():
    batch, enabled = case("B20R"), case("B21")
    batch["data"]["assets"][1]["asset_type"] = "intangible"
    enabled["data"]["items"] = [
        {
            "asset_id": item["asset_id"],
            "useful_life_months": 3,
            "residual_fen": "0",
            "benefit_area": "administration",
            "rounding": "floor_final_remainder",
        }
        for item in batch["data"]["assets"]
    ]
    context = Context(batch, enabled)
    amounts = []
    for month in ("2026-01", "2026-02", "2026-03", "2026-04"):
        result = context.get_result(enabled["business_id"], "depreciation:" + month)
        amounts.append(result.target["values"]["total_amount_fen"])
    assert amounts == ["6666", "9999", "10001", "3334"]
    assert len(result.target["values"]["items"]) == 1
    assert result.target["values"]["items"][0]["asset_id"] == batch["data"]["assets"][0]["asset_id"]


def bulk_fixed_assets(count):
    batch, enabled = case("B20R"), case("B21")
    batch["data"]["assets"] = [
        {"asset_id": str(uuid4()), "asset_type": "fixed", "cost_fen": "100"} for _ in range(count)
    ]
    batch["data"]["creditors"] = [{"person_id": str(uuid4()), "amount_fen": str(100 * count)}]
    batch["data"]["total_cost_fen"] = str(100 * count)
    enabled["data"]["items"] = [
        {
            "asset_id": item["asset_id"],
            "useful_life_months": 2,
            "residual_fen": "0",
            "benefit_area": "administration",
            "rounding": "floor_final_remainder",
        }
        for item in batch["data"]["assets"]
    ]
    return batch, enabled


def test_one_large_source_is_read_by_identity_batches_and_never_once_per_card():
    class CountedContext(Context):
        def __init__(self, *facts):
            super().__init__(*facts)
            self.result_reads, self.relation_reads, self.cost_reads = [], [], []

        def get_result(self, business_id, result_slot):
            self.result_reads.append((business_id, result_slot))
            return super().get_result(business_id, result_slot)

        def related_facts_many(self, producer_ids, *args, **kwargs):
            self.relation_reads.append(tuple(producer_ids))
            return super().related_facts_many(producer_ids, *args, **kwargs)

        def resource_definitions(self, refs, kind):
            self.cost_reads.append(tuple(refs))
            return super().resource_definitions(refs, kind)

    batch, enabled = bulk_fixed_assets(501)
    context = CountedContext(batch, enabled)
    plan = create_assets().evaluate_slot(enabled["business_id"], "main", context)
    assert len(plan["cost_consumptions"]) == 501
    assert len(plan["basis"]["adopted_quantities"]) == 501
    assert len(plan["basis"]["source_results"]) == 1
    assert len(plan["lines"]) == 2
    assert plan["lines"][0]["debit_fen"] == "50100"
    # This guards the shared-source workload, not a wall-clock race on a busy CI host.
    assert len(context.result_reads) <= 3
    assert len(context.relation_reads) <= 4
    assert len(context.cost_reads) <= 2
    assert sum(len(refs) for refs in context.cost_reads) == 501
    assert all(len(refs) <= 500 for refs in context.cost_reads)
