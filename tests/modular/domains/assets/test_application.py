"""Temporary companies exercise the real asset/application/ledger/money boundary."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as app_repository
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

from .test_assets import SOURCE, Context, bulk_fixed_assets, case


@pytest.fixture
def assembled(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-hash"))
    storage = Storage(
        catalog,
        repositories=[
            app_repository(),
            identity_repository(),
            ledger_repository(),
            records_repository(),
            assets_repository(),
            money_repository(),
            operations_repository(),
        ],
    )
    company_id = str(uuid4())
    storage.create_company(
        CompanySpec(
            company_id,
            str(uuid4()),
            "913100000000000001",
            "合成资产公司",
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
        records=create_records(root_for=lambda read: storage.company_metadata(read).company_root),
        domains=[create_assets(), create_operations(), money],
        money=money,
    )
    return app, company_id


def request(company_id, *facts):
    context = Context(*facts)
    text = "合成资产原件及实际接受、启用资料，仅用于软件验收。"
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company_id,
        "command_id": str(uuid4()),
        "objects": [
            {
                "object_id": object_id,
                "kind": obj["kind"],
                "name": "合成对象",
                "expected_seq": 0,
                "data": {**obj.get("data", {}), "category": "合成设备"}
                if obj["kind"] == "asset"
                else {},
            }
            for object_id, obj in context.objects.items()
        ],
        "sources": [
            {
                "source_id": SOURCE,
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": "2026-01",
            }
        ],
        "mutations": [
            {"op": "create", "expected_seq": 0, "fact": deepcopy(fact)} for fact in facts
        ],
    }


def get(app, company_id, *ids):
    return app.accounting_get(
        {
            "company_id": company_id,
            "business_ids": list(ids),
            "include": ["facts", "results", "sources"],
        }
    )


def replace(company_id, *facts):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company_id,
        "command_id": str(uuid4()),
        "mutations": [{"op": "replace", "expected_seq": 1, "fact": fact} for fact in facts],
    }


def result_by_slot(app, company_id, business_id):
    return {
        result["reference"]["result_slot"]: result
        for result in get(app, company_id, business_id)["businesses"][0]["results"]
    }


def test_same_unit_acquisition_activation_and_disposal_build_continuous_three_months(assembled):
    app, company_id = assembled
    acquired, enabled, disposed = case("B20"), case("B21"), case("B22")
    created = app.accounting_apply(request(company_id, disposed, enabled, acquired))
    assert created["completion"] == "complete", created
    actual = result_by_slot(app, company_id, enabled["business_id"])
    assert set(actual) == {
        "main",
        "depreciation:2026-02",
        "depreciation:2026-03",
        "depreciation:2026-04",
    }
    assert [
        actual["depreciation:" + month]["target"]["values"]["total_amount_fen"]
        for month in ("2026-02", "2026-03", "2026-04")
    ] == ["3333", "3333", "3334"]
    disposed_result = result_by_slot(app, company_id, disposed["business_id"])["main"]
    assert disposed_result["target"]["values"]["gain_loss_fen"] == "-1"
    assert (
        app.accounting_apply(request(company_id, disposed, enabled, acquired))["accounting"]
        == "unchanged"
    )


def test_later_explicit_month_extends_calendar_without_clock_or_extra_ai_fact(assembled):
    app, company_id = assembled
    acquired, enabled, future = case("B20"), case("B21"), case("B18")
    assert app.accounting_apply(request(company_id, enabled, acquired))["completion"] == "complete"
    assert set(result_by_slot(app, company_id, enabled["business_id"])) == {"main"}
    future["period"] = "2026-03"
    later = request(company_id, future)
    # Reuse the registered source; do not propose a second version of it.
    later.pop("sources")
    result = app.accounting_apply(later)
    assert result["completion"] == "complete", result
    actual = result_by_slot(app, company_id, enabled["business_id"])
    assert set(actual) == {"main", "depreciation:2026-02", "depreciation:2026-03"}
    assert (
        actual["depreciation:2026-03"]["target"]["values"]["items"][0]["closing_accumulated_fen"]
        == "6666"
    )


def test_open_cost_change_recalculates_card_and_preserves_original_voucher_numbers(assembled):
    app, company_id = assembled
    acquired, enabled, disposed = case("B20"), case("B21"), case("B22")
    created = app.accounting_apply(request(company_id, acquired, enabled, disposed))
    assert created["completion"] == "complete", created
    before = result_by_slot(app, company_id, enabled["business_id"])
    acquired["data"]["asset"]["cost_fen"] = "13001"
    changed = app.accounting_apply(replace(company_id, acquired))
    assert changed["completion"] == "complete", changed
    after = result_by_slot(app, company_id, enabled["business_id"])
    assert after["depreciation:2026-02"]["target"]["values"]["total_amount_fen"] == "4333"
    assert after["depreciation:2026-04"]["target"]["values"]["total_amount_fen"] == "4334"
    assert {slot: item["reference"]["voucher_number"] for slot, item in after.items()} == {
        slot: item["reference"]["voucher_number"] for slot, item in before.items()
    }


def test_same_asset_cannot_be_hidden_in_two_different_batches(assembled):
    app, company_id = assembled
    first, second = case("B20R"), case("B20R")
    second["business_id"] = str(uuid4())
    result = app.accounting_apply(request(company_id, first, second))
    assert result["completion"] == "not_saved", result
    assert result["error"] in {"conflict", "dependency_conflict"}, result
    assert get(app, company_id, first["business_id"], second["business_id"])["businesses"] == []


def test_project_consumed_twice_in_same_unit_fails_atomically(assembled):
    app, company_id = assembled
    cost, released, acquired = case("B18"), case("B19"), case("B20P")
    result = app.accounting_apply(request(company_id, cost, released, acquired))
    assert result["completion"] == "not_saved", result
    assert result["error"] == "dependency_conflict", result
    assert get(app, company_id, cost["business_id"], acquired["business_id"])["businesses"] == []


def test_paid_asset_cost_change_keeps_actual_payment_and_reports_remaining(assembled):
    app, company_id = assembled
    acquired, bank = case("B20"), str(uuid4())
    supplier = acquired["data"]["supplier_id"]
    paid = {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": "2026-01",
        "effective_on": "2026-01-25",
        "evidence_ids": [SOURCE],
        "data": {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "amount_fen": "10001",
            "payment_method": "individual",
            "counterparty_id": supplier,
            "allocations": [
                {
                    "source": {"business_id": acquired["business_id"], "slot": "primary"},
                    "amount_fen": "10001",
                }
            ],
        },
    }
    initial = request(company_id, acquired, paid)
    initial["objects"].append(
        {
            "object_id": bank,
            "kind": "bank_account",
            "name": "合成银行",
            "expected_seq": 0,
            "data": {
                "bank_name": "合成银行",
                "account_number": "SYNTHETIC-001",
                "currency": "CNY",
                "owner": "company",
            },
        }
    )
    created = app.accounting_apply(initial)
    assert created["completion"] == "complete", created
    payment = get(app, company_id, paid["business_id"])
    acquired["data"]["asset"]["cost_fen"] = "12001"
    result = app.accounting_apply(replace(company_id, acquired))
    assert result["completion"] == "complete", result
    after = get(app, company_id, paid["business_id"])
    assert after["businesses"][0]["fact"] == payment["businesses"][0]["fact"]
    with app.storage.read_unit(company_id) as read:
        balance = app.ledger.resolve(
            [{"business_id": acquired["business_id"], "slot": "primary"}], "obligation", read
        )[0]["balance"]
    assert balance["money_settled_fen"] == "10001"
    assert balance["remaining_fen"] == "2000"
    acquired["data"]["asset"]["cost_fen"] = "8000"
    too_low = replace(company_id, acquired)
    too_low["mutations"][0]["expected_seq"] = 2
    rejected = app.accounting_apply(too_low)
    assert rejected["completion"] == "not_saved", rejected
    assert rejected["error"] == "dependency_conflict", rejected


def test_secondary_asset_in_batch_is_queryable_and_open_withdraw_removes_only_current_body(
    assembled,
):
    app, company_id = assembled
    batch = case("B20R")
    assert app.accounting_apply(request(company_id, batch))["completion"] == "complete"
    with app.storage.read_unit(company_id) as read:
        for object_id in [item["asset_id"] for item in batch["data"]["assets"]] + [
            item["person_id"] for item in batch["data"]["creditors"]
        ]:
            assert create_assets().query_current(
                {
                    "company_id": company_id,
                    "family": "assets",
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "object_id": object_id,
                    "limit": 10,
                },
                read,
            ) == (batch,)
    removed = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company_id,
            "command_id": str(uuid4()),
            "mutations": [
                {"op": "withdraw", "expected_seq": 1, "business_id": batch["business_id"]}
            ],
        }
    )
    assert removed["completion"] == "complete", removed
    with app.storage.read_unit(company_id) as read:
        assert create_assets().export_current([batch["business_id"]], read) == ()


def test_different_assets_with_same_vendor_day_and_amount_are_independent(assembled):
    app, company_id = assembled
    first, second = case("B20"), case("B20")
    second["business_id"] = str(uuid4())
    second["data"]["asset"]["asset_id"] = str(uuid4())
    result = app.accounting_apply(request(company_id, first, second))
    assert result["completion"] == "complete", result
    assert len(get(app, company_id, first["business_id"], second["business_id"])["businesses"]) == 2


def test_501_card_source_is_atomic_and_reloads_the_next_candidate_after_cost_change(assembled):
    app, company_id = assembled
    batch, enabled = bulk_fixed_assets(501)
    initial = request(company_id, batch, enabled)
    objects = initial.pop("objects")
    for offset in range(0, len(objects), 500):
        registered = app.accounting_apply(
            {
                "contract": "ai-accounting-modular/1",
                "company_id": company_id,
                "command_id": str(uuid4()),
                "mutations": [],
                "objects": objects[offset : offset + 500],
            }
        )
        assert registered["completion"] == "complete", registered
    created = app.accounting_apply(initial)
    assert created["completion"] == "complete", created
    result = result_by_slot(app, company_id, enabled["business_id"])["main"]
    assert len(result["target"]["values"]["assets"]) == 501
    assert len(result["basis"]["adopted_quantities"]) == 501
    assert result["vouchers"][0]["lines"][0]["debit_fen"] == "50100"
    original_number = result["reference"]["voucher_number"]
    batch["data"]["assets"][0]["cost_fen"] = "200"
    batch["data"]["total_cost_fen"] = "50200"
    batch["data"]["creditors"][0]["amount_fen"] = "50200"
    changed = app.accounting_apply(replace(company_id, batch))
    assert changed["completion"] == "complete", changed
    current = result_by_slot(app, company_id, enabled["business_id"])["main"]
    assert current["vouchers"][0]["lines"][0]["debit_fen"] == "50200"
    assert current["reference"]["voucher_number"] == original_number
    assert (
        next(
            item["cost_fen"]
            for item in current["basis"]["adopted_quantities"]
            if item["asset_id"] == batch["data"]["assets"][0]["asset_id"]
        )
        == "200"
    )
