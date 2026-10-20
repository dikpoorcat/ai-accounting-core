"""Real cross-owner corrections preserve one business, one debt and actual money."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.domains.assets.public import create_assets
from ai_accounting_modular.domains.assets.public import repository_factory as assets_repository
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.money.public import create_money
from ai_accounting_modular.money.public import repository_factory as money_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog

from .conftest import get, initial_request, source


@pytest.fixture
def assembled(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成交接负责人", "synthetic-hash"))
    storage = Storage(
        catalog,
        repositories=[
            application_repository(),
            identity_repository(),
            ledger_repository(),
            operations_repository(),
            assets_repository(),
            money_repository(),
            records_repository(),
        ],
    )
    company = str(uuid4())
    storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000001",
            "合成交接公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    identity, ledger = create_identity(), create_ledger()
    money = create_money(identity=identity, ledger=ledger)
    app = create_application(
        storage=storage,
        identity=identity,
        ledger=ledger,
        money=money,
        records=create_records(root_for=lambda read: storage.company_metadata(read).company_root),
        domains=[create_operations(), create_assets(), money],
    )
    return app, company


def as_asset(current, asset_id):
    result = deepcopy(current)
    data = current["data"]
    result.update(
        kind="assets.acquisition",
        data={
            "type": "direct",
            "supplier_id": data["creditor_id"],
            "asset": {"asset_id": asset_id, "asset_type": "fixed", "cost_fen": data["amount_fen"]},
        },
    )
    return result


def as_expense(current):
    result = deepcopy(current)
    data = current["data"]
    result.update(
        kind="operations.expense",
        data={
            "creditor_id": data["supplier_id"],
            "creditor_kind": "supplier",
            "amount_fen": data["asset"]["cost_fen"],
            "expense_class": "administration",
        },
    )
    return result


def seed(app, company, *, asset=False, paid="0"):
    submitted = initial_request(company)
    asset_id = str(uuid4())
    submitted["objects"].append(
        {
            "object_id": asset_id,
            "kind": "asset",
            "name": "合成固定资产",
            "expected_seq": 0,
            "data": {"asset_type": "fixed", "category": "合成设备"},
        }
    )
    current = submitted["mutations"][0]["fact"]
    if asset:
        current = as_asset(current, asset_id)
        submitted["mutations"][0]["fact"] = current
    money = None
    if paid != "0":
        bank_id = str(uuid4())
        submitted["objects"].append(
            {
                "object_id": bank_id,
                "kind": "bank_account",
                "name": "合成银行",
                "expected_seq": 0,
                "data": {
                    "bank_name": "合成银行",
                    "account_number": "handoff-only",
                    "currency": "CNY",
                    "owner": "company",
                },
            }
        )
        money = {
            "business_id": str(uuid4()),
            "kind": "money.movement",
            "period": "2026-01",
            "effective_on": "2026-01-20",
            "evidence_ids": current["evidence_ids"],
            "data": {
                "type": "settlement",
                "funds_account_id": bank_id,
                "direction": "outflow",
                "amount_fen": paid,
                "payment_method": "individual",
                "allocations": [
                    {
                        "source": {"business_id": current["business_id"], "slot": "primary"},
                        "amount_fen": paid,
                    }
                ],
            },
        }
        submitted["mutations"].append({"op": "create", "expected_seq": 0, "fact": money})
    response = app.accounting_apply(submitted)
    assert response.get("completion") == "complete", response
    return current, asset_id, money


def replace(company, *facts, seq=1, **extra):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [
            {"op": "replace", "expected_seq": seq, "fact": deepcopy(fact)} for fact in facts
        ],
        **extra,
    }


def business(app, company, current):
    return get(app, company, current["business_id"])["businesses"][0]


def body_owner(app, kind):
    return app.registry.owner(kind)


@pytest.mark.parametrize("start_asset", [False, True])
@pytest.mark.parametrize("paid", ["0", "40000", "100000"])
def test_real_bidirectional_handoff_keeps_one_seq_number_primary_and_actual_money(
    assembled, start_asset, paid
):
    app, company = assembled
    current, asset_id, money = seed(app, company, asset=start_asset, paid=paid)
    before = business(app, company, current)
    original_movement = None
    if money:
        with app.storage.read_unit(company) as read:
            original_movement = app.ledger.fund_movements(
                money["data"]["funds_account_id"], "2026-01", "2026-01", read
            )[0].value
    old_kind = current["kind"]
    target = as_expense(current) if start_asset else as_asset(current, asset_id)
    submitted = replace(company, target)
    response = app.accounting_apply(submitted)
    assert response.get("completion") == "complete", response
    after = business(app, company, target)
    assert after["state"]["seq"] == 2
    assert after["fact"] == target
    original, final = before["results"][0], after["results"][0]
    assert final["reference"]["result_id"] == original["reference"]["result_id"]
    assert final["reference"]["voucher_id"] == original["reference"]["voucher_id"]
    assert final["reference"]["voucher_number"] == original["reference"]["voucher_number"]
    assert {item["source_id"] for item in after["sources"]} == set(current["evidence_ids"])
    with app.storage.read_unit(company) as read:
        assert body_owner(app, old_kind).export_current([current["business_id"]], read) == ()
        assert body_owner(app, target["kind"]).export_current([current["business_id"]], read) == (
            target,
        )
        debt = app.ledger.resolve(
            [{"business_id": target["business_id"], "slot": "primary"}], "obligation", read
        )[0]
        assert debt["obligation"]["amount_fen"] == "100000"
        assert (
            debt["balance"]["remaining_fen"]
            == {"0": "100000", "40000": "60000", "100000": "0"}[paid]
        )
        if money:
            movement = app.ledger.fund_movements(
                money["data"]["funds_account_id"], "2026-01", "2026-01", read
            )
            assert len(movement) == 1
            assert movement[0].value["movement_id"] == original_movement["movement_id"]
            assert movement[0].value["amount_fen"] == paid
            assert movement[0].value["actual_on"] == "2026-01-20"
        head = read.head
    if money:
        assert business(app, company, money)["fact"] == money
    retried = app.accounting_apply(submitted)
    assert retried["changes"] == "none", retried
    assert business(app, company, target) == after
    with app.storage.read_unit(company) as read:
        assert read.head == head


def test_mixed_two_way_handoffs_do_not_let_outgoing_overwrite_slots_or_natural_keys(assembled):
    app, company = assembled
    expense, _, _ = seed(app, company)
    acquired, asset_id, _ = seed(app, company, asset=True)
    target_asset, target_expense = as_asset(expense, asset_id), as_expense(acquired)
    result = app.accounting_apply(replace(company, target_asset, target_expense))
    assert result.get("completion") == "complete", result
    assert business(app, company, target_asset)["state"]["seq"] == 2
    assert business(app, company, target_expense)["state"]["seq"] == 2
    with app.storage.read_unit(company) as read:
        heads = read.repository("application").heads(
            [target_asset["business_id"], target_expense["business_id"]]
        )
        assert heads[target_asset["business_id"]]["natural_key"] == "assets.acquisition:" + asset_id
        assert heads[target_expense["business_id"]]["natural_key"] is None
        assert (
            body_owner(app, "operations.expense").export_current([expense["business_id"]], read)
            == ()
        )
        assert (
            body_owner(app, "assets.acquisition").export_current([acquired["business_id"]], read)
            == ()
        )


@pytest.mark.parametrize(
    "mismatch",
    [
        "party",
        "amount",
        "individual",
        "employee",
        "project_completion",
        "unsupported_kind",
        "stale",
    ],
)
def test_handoff_preconditions_reject_before_saving_any_new_participant(assembled, mismatch):
    app, company = assembled
    current, asset_id, _ = seed(app, company)
    if mismatch == "stale":
        current["data"]["expense_class"] = "service"
        assert app.accounting_apply(replace(company, current))["completion"] == "complete"
    if mismatch in {"individual", "employee"}:
        person = str(uuid4())
        current["data"]["creditor_id"] = person
        current["data"]["creditor_kind"] = mismatch
        prepared = replace(
            company,
            current,
            objects=[
                {
                    "object_id": person,
                    "kind": "person",
                    "name": "合成个人垫款人",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
        )
        assert app.accounting_apply(prepared)["completion"] == "complete"
    before = business(app, company, current)
    target = as_asset(current, asset_id)
    new_source = source("合成错误交接，不得留下来源或新对象。")
    new_party = str(uuid4())
    if mismatch == "party":
        target["data"]["supplier_id"] = new_party
    elif mismatch == "amount":
        target["data"]["asset"]["cost_fen"] = "99999"
    elif mismatch == "project_completion":
        target["data"] = {
            "type": "project_completion",
            "asset": target["data"]["asset"],
            "project_id": str(uuid4()),
            "sources": [
                {
                    "source": {"business_id": str(uuid4()), "slot": "primary"},
                    "amount_fen": "100000",
                }
            ],
        }
    elif mismatch == "unsupported_kind":
        target = deepcopy(current)
        target["kind"] = "operations.advance"
        target["data"] = {
            "side": "supplier",
            "counterparty_id": new_party,
            "amount_fen": "100000",
            "purpose": "operating",
        }
    target["evidence_ids"] = [new_source["source_id"]]
    request = replace(
        company,
        target,
        seq=2 if mismatch in {"individual", "employee"} else 1,
        sources=[new_source],
        objects=[
            {
                "object_id": new_party,
                "kind": "organization",
                "name": "不应保存",
                "expected_seq": 0,
                "data": {},
            }
        ],
    )
    result = app.accounting_apply(request)
    assert result.get("completion") == "not_saved", result
    expected = (
        "conflict"
        if mismatch == "stale"
        else "unsupported_change_kind"
        if mismatch in {"individual", "employee", "project_completion", "unsupported_kind"}
        else "dependency_conflict"
    )
    assert result["error"] == expected, result
    assert business(app, company, current) == before
    with app.storage.read_unit(company) as read:
        assert app.identity.get_batch([new_party], read) == ()
        assert app.records.resolve_sources([new_source["source_id"]], read) == ()


def test_source_supplement_uses_final_fact_once_without_keeping_wrong_source_edges(assembled):
    app, company = assembled
    current, asset_id, _ = seed(app, company)
    target = as_asset(current, asset_id)
    corrected = source("合成已补正采购依据，替换错误的原确认。")
    target["evidence_ids"] = [corrected["source_id"]]
    result = app.accounting_apply(replace(company, target, sources=[corrected]))
    assert result.get("completion") == "complete", result
    assert {item["source_id"] for item in business(app, company, target)["sources"]} == {
        corrected["source_id"]
    }


def test_handoff_can_move_period_and_publishes_only_the_final_owner_document(assembled):
    app, company = assembled
    current, asset_id, _ = seed(app, company)
    target = as_asset(current, asset_id)
    target["period"] = "2026-02"
    result = app.accounting_apply(replace(company, target))
    assert result.get("completion") == "complete", result
    assert business(app, company, target)["fact"] == target
    with app.storage.read_unit(company) as read:
        with app.records.lease_document_set(read) as manifest:
            shards = {item["key"]: item for item in manifest["shards"]}
            assert "facts:operations:2026-01" not in shards
            root = app.storage.company_metadata(read).company_root
            facts = loads_json((root / shards["facts:assets:2026-02"]["path"]).read_bytes())
            assert facts["facts"] == [target]


def test_failure_after_both_owners_save_rolls_back_old_body_new_body_head_and_journal(
    assembled, monkeypatch
):
    app, company = assembled
    current, asset_id, _ = seed(app, company, paid="40000")
    before = business(app, company, current)
    target = as_asset(current, asset_id)
    owner = body_owner(app, "assets.acquisition")
    original = owner.apply_current

    def fail_after_save(plan, unit):
        original(plan, unit)
        raise ContractError("dependency_conflict", "合成新旧正文写入后的失败。")

    monkeypatch.setattr(owner, "apply_current", fail_after_save)
    result = app.accounting_apply(replace(company, target))
    assert result.get("completion") == "not_saved", result
    assert business(app, company, current) == before
    with app.storage.read_unit(company) as read:
        assert owner.export_current([current["business_id"]], read) == ()


@pytest.mark.parametrize("start_asset", [False, True])
def test_old_owner_cannot_report_success_without_releasing_its_body(
    assembled, monkeypatch, start_asset
):
    app, company = assembled
    current, asset_id, _ = seed(app, company, asset=start_asset)
    before = business(app, company, current)
    target = as_expense(current) if start_asset else as_asset(current, asset_id)
    old_owner = body_owner(app, current["kind"])

    def ignore_outgoing(plan, unit):
        assert all(item["op"] == "handoff_out" for item in plan["businesses"])

    monkeypatch.setattr(old_owner, "apply_current", ignore_outgoing)
    result = app.accounting_apply(replace(company, target))
    assert result.get("completion") == "not_saved", result
    assert result["error"] == "storage_corrupt", result
    assert business(app, company, current) == before
    with app.storage.read_unit(company) as read:
        assert body_owner(app, target["kind"]).export_current([target["business_id"]], read) == ()


def activation(current, asset_id):
    return {
        "business_id": str(uuid4()),
        "kind": "assets.activation",
        "period": "2026-01",
        "evidence_ids": current["evidence_ids"],
        "data": {
            "items": [
                {
                    "asset_id": asset_id,
                    "useful_life_months": 12,
                    "residual_fen": "0",
                    "benefit_area": "administration",
                    "rounding": "round_half_up_card",
                }
            ]
        },
    }


def disposal(current, asset_id):
    return {
        "business_id": str(uuid4()),
        "kind": "assets.disposal",
        "period": "2026-01",
        "effective_on": "2026-01-31",
        "evidence_ids": current["evidence_ids"],
        "data": {"type": "scrap", "asset_id": asset_id},
    }


def test_activation_can_be_withdrawn_atomically_when_reclassifying_acquisition_to_expense(
    assembled,
):
    app, company = assembled
    current, asset_id, _ = seed(app, company, asset=True, paid="40000")
    enabled = activation(current, asset_id)
    request = replace(company, enabled)
    request["mutations"][0].update(op="create", expected_seq=0)
    assert app.accounting_apply(request)["completion"] == "complete"
    target = as_expense(current)
    submitted = replace(company, target)
    submitted["mutations"].append(
        {"op": "withdraw", "business_id": enabled["business_id"], "expected_seq": 1}
    )
    result = app.accounting_apply(submitted)
    assert result.get("completion") == "complete", result
    assert business(app, company, target)["fact"] == target
    assert business(app, company, enabled)["state"]["state"] == "withdrawn"


def test_new_asset_owner_can_activate_in_the_same_unit_as_handoff(assembled):
    app, company = assembled
    current, asset_id, _ = seed(app, company, paid="40000")
    target = as_asset(current, asset_id)
    enabled = activation(target, asset_id)
    submitted = replace(company, target)
    submitted["mutations"].append({"op": "create", "expected_seq": 0, "fact": enabled})
    result = app.accounting_apply(submitted)
    assert result.get("completion") == "complete", result
    assert business(app, company, target)["state"]["seq"] == 2
    assert business(app, company, enabled)["state"]["state"] == "active"
    with app.storage.read_unit(company) as read:
        edges = read.repository("application").dependencies([enabled["business_id"]])
        assert any(
            item["producer_id"] == target["business_id"]
            and item["producer_public_slot"] == "main"
            and item["read_mode"] == "result"
            for item in edges
        )


@pytest.mark.parametrize("followup", [activation, disposal], ids=["activation", "disposal"])
def test_live_asset_dependency_can_relink_to_a_real_new_acquisition_in_the_same_unit(
    assembled, followup
):
    app, company = assembled
    current, asset_id, _ = seed(app, company, asset=True)
    enabled = followup(current, asset_id)
    submitted = replace(company, enabled)
    submitted["mutations"][0].update(op="create", expected_seq=0)
    assert app.accounting_apply(submitted)["completion"] == "complete"
    replacement = deepcopy(current)
    replacement["business_id"] = str(uuid4())
    submitted = replace(company, as_expense(current))
    submitted["mutations"].append({"op": "create", "expected_seq": 0, "fact": replacement})
    result = app.accounting_apply(submitted)
    assert result.get("completion") == "complete", result
    with app.storage.read_unit(company) as read:
        edges = read.repository("application").dependencies([enabled["business_id"]])
        costs = {item["producer_id"] for item in edges if item["relation_kind"] == "cost"}
        assert replacement["business_id"] in costs
        assert current["business_id"] not in costs


@pytest.mark.parametrize("followup", [activation, disposal], ids=["activation", "disposal"])
def test_active_asset_dependency_cannot_silently_lose_its_cost_origin(assembled, followup):
    app, company = assembled
    current, asset_id, _ = seed(app, company, asset=True)
    enabled = followup(current, asset_id)
    request = replace(company, enabled)
    request["mutations"][0].update(op="create", expected_seq=0)
    assert app.accounting_apply(request)["completion"] == "complete"
    before = business(app, company, current)
    result = app.accounting_apply(replace(company, as_expense(current)))
    assert result.get("completion") == "not_saved", result
    assert result["error"] == "dependency_conflict", result
    assert business(app, company, current) == before


def test_frozen_handoff_and_dependent_exit_use_ledger_corrections_and_keep_actual_payment(
    assembled,
):
    app, company = assembled
    current, asset_id, money = seed(app, company, asset=True, paid="40000")
    enabled = activation(current, asset_id)
    submitted = replace(company, enabled)
    submitted["mutations"][0].update(op="create", expected_seq=0)
    assert app.accounting_apply(submitted)["completion"] == "complete"
    # This marks real formal heads for selection tests; it does not claim that
    # an application close/freeze workflow has been delivered.
    with app.storage.write_unit(company) as unit:
        unit.repository("ledger").freeze_heads("2026-01")
    original = business(app, company, current)["results"][0]
    reason = "已核对采购事实，原资产误分类，应按费用承接并退出错误启用。"
    target = as_expense(current)
    submitted = replace(company, target, correction_period="2026-03", explanation=reason)
    refused = app.accounting_apply(submitted)
    assert refused.get("completion") == "not_saved", refused
    assert refused["error"] == "dependency_conflict", refused
    submitted["mutations"].append(
        {"op": "withdraw", "business_id": enabled["business_id"], "expected_seq": 1}
    )
    result = app.accounting_apply(submitted)
    assert result.get("completion") == "complete", result
    assert business(app, company, target)["state"]["seq"] == 2
    retired = business(app, company, enabled)
    assert retired["state"]["state"] == "withdrawn"
    assert retired["results"][0]["target"]["kind"] == "system.withdrawal"
    assert retired["results"][0]["basis"]["explanation"] == reason
    with app.storage.read_unit(company) as read:
        old = app.ledger.get_results([], read, result_refs=[original["reference"]])[0]
        assert old["target"] == original["target"]
        assert old["basis"] == original["basis"]
        assert old["vouchers"] == original["vouchers"]
        movements = app.ledger.fund_movements(
            money["data"]["funds_account_id"], "2026-01", "2026-03", read
        )
        assert len(movements) == 1
        assert movements[0].value["amount_fen"] == "40000"
        assert movements[0].value["actual_on"] == "2026-01-20"
    assert business(app, company, money)["fact"] == money
    assert app.accounting_apply(submitted)["changes"] == "none"
