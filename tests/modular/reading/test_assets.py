"""The frozen asset page reads complete saved collections through public owners."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    loads_json,
    validate_contract,
)
from ai_accounting_modular.domains.assets.public import create_assets
from ai_accounting_modular.reading import _assets, _monthly
from ai_accounting_modular.reading._context import period
from tests.modular.application._working_case import case as case
from tests.modular.domains.assets.test_application import request as asset_request
from tests.modular.domains.assets.test_assets import bulk_fixed_assets
from tests.modular.domains.assets.test_assets import case as asset_case
from tests.modular.domains.assets.test_reading import save
from tests.modular.domains.assets.test_reading_integration import _asset

from ._support import rebuild_guard


def scope(company, month="2026-01"):
    return {"company_id": company, "period_from": month, "period_to": month, "view": "as_posted"}


def publish(app, company, month="2026-01"):
    normalized = _monthly.normalize(scope(company, month))
    reader = SimpleNamespace(
        assets=create_assets(ledger=app.ledger, identity=app.identity), identity=app.identity
    )
    with app.storage.read_unit(company) as read:
        model = _assets.prepare(reader, normalized, read)
        selected = period(month, app.ledger.closed_through(read))
        database_id = read.database_id
    with app.storage.maintenance_unit(company) as unit:
        repository = unit.repository("reading")
        repository.register(_monthly.key(normalized), "assets", unit.head, _monthly.FORMAT_VERSION)
        repository.mark(_monthly.key(normalized), "assets", unit.head)
        assert repository.publish(
            _monthly.key(normalized),
            "assets",
            unit.head,
            _monthly.FORMAT_VERSION,
            model,
            guard=rebuild_guard(repository, _monthly.key(normalized), "assets", unit.head),
            current_head=unit.head,
        )
    snapshot = _monthly.MonthSnapshot(
        _monthly.key(normalized),
        normalized,
        content_digest(
            {"assets": model.content_digest, "scope": normalized, "status": selected["status"]}
        ),
        {"assets": loads_json(model.payload_json)},
        selected,
        company,
        database_id,
    )
    return snapshot, reader


def page(app, snapshot, **query):
    with app.storage.read_unit(snapshot.company_id) as read:
        result = _assets.build(snapshot, read, query, identity=app.identity)
    envelope = {
        "schema_version": 10,
        **result,
        "read_context": {
            "company_id": snapshot.company_id,
            "database_id": snapshot.database_id,
            "as_of": "2026-10-21",
            "read_version": snapshot.version,
        },
        "read_semantics": {
            "knowledge": "current_knowledge",
            "accounting": "as_posted",
            "business_basis": "frozen_adoption"
            if snapshot.selected_period["status"] == "closed"
            else "current_known",
            "display": "frozen_with_current_supplements"
            if snapshot.selected_period["status"] == "closed"
            else "current",
            "system_time_replay": False,
            "recorded_at": "system_recording_time",
            "recording_period": "business_recording_period",
            "recorded_later": "business_recording_period_after_selected_period",
        },
    }
    validate_contract("dashboard-response-v2.json", "dashboard_assets", envelope)
    return result


def test_formal_card_dates_payments_and_exit_expand_without_extra_owner_reads(case, monkeypatch):
    case.seed(expense=False)
    acquired, enabled, disposed = asset_case("B20"), asset_case("B21"), asset_case("B22")
    save(case.app, asset_request(case.company, acquired, enabled, disposed))
    january, reader = publish(case.app, case.company)
    monkeypatch.setattr(
        reader.assets, "asset_activity", lambda *_: pytest.fail("hot page reread assets")
    )
    monkeypatch.setattr(
        case.kernel.ledger,
        "financial_summary",
        lambda *_: pytest.fail("hot page recalculated balances"),
    )
    first = page(case.app, january)["data"]
    assert set(first["collections"]) == {"assets", "projects"}
    card = first["collections"]["assets"]["items"][0]
    assert card["acquisition_date"] == card["in_service_date"] == "2026-01"
    assert card["recognition_label"] == "2026-01（按月确认）"
    assert card["cost_fen"] == "10001" and card["book_value_fen"] == "10001"
    assert card["payment_summary"] == {
        "obligation_count": 1,
        "checking": False,
        "amount_fen": "10001",
        "paid_fen": "0",
        "other_settled_fen": "0",
        "remaining_fen": "10001",
    }
    monkeypatch.undo()
    may, _ = publish(case.app, case.company, "2026-05")
    exited = page(case.app, may, asset_id=card["asset_id"], asset_filter="all")["data"][
        "collections"
    ]["assets"]["items"][0]
    assert exited["book_value_fen"] == "0" and exited["accumulated_charge_fen"] == "10000"
    assert exited["disposal"] == {
        "date": "2026-05-31",
        "book_value_fen": "1",
        "kind": "retirement",
        "gross_proceeds_fen": "0",
        "gain_fen": "0",
        "loss_fen": "1",
        "party": "",
    }
    active = page(case.app, may, asset_id=card["asset_id"], asset_filter="active")["data"]
    assert active["collections"]["assets"]["page"]["filtered_count"] == 0
    assert active["month_exited_count"] == 1


def test_501_cards_keep_full_summary_exact_large_money_and_scoped_pages(case, monkeypatch):
    case.seed(expense=False)
    batch, enabled = bulk_fixed_assets(501)
    batch["data"]["assets"][0]["cost_fen"] = "9007199254740993"
    total = 9007199254740993 + 50000
    batch["data"]["total_cost_fen"] = batch["data"]["creditors"][0]["amount_fen"] = str(total)
    initial = asset_request(case.company, batch, enabled)
    objects = initial.pop("objects")
    for start in range(0, len(objects), 500):
        save(case.app, case.request(objects=objects[start : start + 500]))
    save(case.app, initial)
    snapshot, reader = publish(case.app, case.company)
    monkeypatch.setattr(
        reader.assets, "asset_activity", lambda *_: pytest.fail("pagination reread assets")
    )
    first = page(case.app, snapshot)["data"]
    assert first["registered_count"] == first["active_count"] == 501
    assert first["ledger_cost_fen"] == first["fixed"]["active_cost_fen"] == str(total)
    collection = first["collections"]["assets"]
    assert collection["page"]["total_count"] == collection["page"]["filtered_count"] == 501
    assert len(collection["items"]) == 20 and collection["page"]["next_cursor"]
    later = page(
        case.app, snapshot, section="assets", cursor=collection["page"]["next_cursor"], limit=500
    )["data"]
    assert set(later["collections"]) == {"assets"}
    last = later["collections"]["assets"]
    assert len(last["items"]) == 481 and last["page"]["next_cursor"] is None
    all_cards = [*collection["items"], *last["items"]]
    assert len({card["asset_id"] for card in all_cards}) == 501
    assert any(card["cost_fen"] == "9007199254740993" for card in all_cards)
    assert all(card["payment_summary"]["amount_fen"] == str(total) for card in all_cards)
    with pytest.raises(ContractError) as error:
        page(
            case.app,
            snapshot,
            section="assets",
            asset_filter="pending",
            cursor=collection["page"]["next_cursor"],
        )
    assert error.value.code == "snapshot_changed"


def test_unestablished_current_identity_is_unknown_and_not_an_active_frozen_card(case):
    case.seed(expense=False)
    _asset(case, activate=True)
    unknown = str(uuid4())
    save(
        case.app,
        case.request(
            objects=[
                {
                    "object_id": unknown,
                    "kind": "asset",
                    "name": "后来补充的资产",
                    "expected_seq": 0,
                    "data": {"asset_type": "intangible", "category": "软件"},
                }
            ]
        ),
    )
    snapshot, _ = publish(case.app, case.company)
    result = page(case.app, snapshot)["data"]
    assert (
        result["registered_count"] == 2
        and result["active_count"] == result["unestablished_count"] == 1
    )
    assert result["checking"]
    unknown_card = next(
        item for item in result["collections"]["assets"]["items"] if item["asset_id"] == unknown
    )
    assert unknown_card == {
        "asset_id": unknown,
        "asset_type": "intangible",
        "name": "后来补充的资产",
        "selection_status": "unestablished",
        "cost_fen": None,
        "accumulated_charge_fen": None,
        "month_charge_fen": None,
        "book_value_fen": None,
    }
    active = page(case.app, snapshot, asset_filter="active")["data"]["collections"]["assets"]
    assert active["page"]["total_count"] == 2 and active["page"]["filtered_count"] == 1
    with pytest.raises(ContractError) as error:
        page(case.app, snapshot, asset_id=str(uuid4()))
    assert error.value.code == "not_found"


def test_missing_opening_prepares_ready_business_gap_and_build_returns_real_issues(case):
    app, company = case.app, case.company
    save(app, asset_request(company, asset_case("B20")))
    snapshot, _ = publish(app, company)
    saved = snapshot.components["assets"]
    assert saved["_ledger"]["fixed_cost"]["state"] == "unestablished"
    with pytest.raises(ContractError) as error:
        page(app, snapshot)
    assert error.value.code == "needs_information"
    assert error.value.fact_issues == tuple(saved["_ledger"]["issues"])
    with app.storage.read_unit(company) as read:
        assert read.repository("reading").state(snapshot.scope_key, "assets")["state"] == "ready"


def test_real_http_keeps_unknown_balance_as_a_stable_business_gap(case):
    save(case.app, asset_request(case.company, asset_case("B20")))
    parameters = {"company_id": case.company, "period": "2026-01"}
    with case.kernel.dashboard.get("/api/dashboard/assets", parameters) as response:
        assert response.status == 200 and response.body["state"] == "preparing"
    assert case.kernel.reading.run_pending(case.company)
    for _ in range(2):
        with case.kernel.dashboard.get("/api/dashboard/assets", parameters) as response:
            assert response.status == 409
            assert response.body["code"] == "needs_information" and response.body["fact_issues"]
        assert case.kernel.reading.run_pending(case.company) == ()


def test_real_http_ready_page_and_exact_section_use_one_asset_capture(case, monkeypatch):
    case.seed(expense=False)
    acquired, enabled = asset_case("B20"), asset_case("B21")
    save(case.app, asset_request(case.company, acquired, enabled))
    calls = []
    original = case.kernel.reading.assets.asset_activity

    def capture(scope, read):
        calls.append(scope)
        return original(scope, read)

    monkeypatch.setattr(case.kernel.reading.assets, "asset_activity", capture)
    parameters = {"company_id": case.company, "period": "2026-01"}
    with case.kernel.dashboard.get("/api/dashboard/assets", parameters) as response:
        assert response.status == 200 and response.body["state"] == "preparing"
    assert case.kernel.reading.run_pending(case.company)
    with case.kernel.dashboard.get("/api/dashboard/assets", parameters) as response:
        assert response.status == 200 and response.body["state"] == "ready"
        inner = response.body["response"]
        assert response.body["model_version"] == inner["snapshot_version"]
        assert inner["data"]["collections"]["assets"]["items"][0]["in_service_date"] == "2026-01"
        validate_contract("dashboard-response-v2.json", "dashboard_assets", inner)
    exact = {
        **parameters,
        "section": "assets",
        "asset_filter": "all",
        "asset_id": acquired["data"]["asset"]["asset_id"],
        "expected_version": inner["snapshot_version"],
    }
    with case.kernel.dashboard.get("/api/dashboard/assets", exact) as response:
        assert response.status == 200
        assert set(response.body["data"]["collections"]) == {"assets"}
        assert response.body["snapshot_version"] == inner["snapshot_version"]
    assert len(calls) == 1


def test_current_asset_code_updates_display_without_changing_formal_results(case):
    case.seed(expense=False)
    acquired, enabled = asset_case("B20"), asset_case("B21")
    registration = asset_request(case.company, acquired, enabled)
    asset = next(obj for obj in registration["objects"] if obj["kind"] == "asset")
    asset["data"]["code"] = "设备-001"
    save(case.app, registration)
    business_ids = (acquired["business_id"], enabled["business_id"])
    before = case.get(*business_ids)["businesses"]
    parameters = {"company_id": case.company, "period": "2026-01"}

    def ready():
        with case.kernel.dashboard.get("/api/dashboard/assets", parameters) as response:
            assert response.status == 200 and response.body["state"] == "preparing"
        assert case.kernel.reading.run_pending(case.company)
        with case.kernel.dashboard.get("/api/dashboard/assets", parameters) as response:
            assert response.status == 200 and response.body["state"] == "ready"
            inner = response.body["response"]
            validate_contract("dashboard-response-v2.json", "dashboard_assets", inner)
            return inner

    original = ready()
    assert original["data"]["collections"]["assets"]["items"][0]["code"] == "设备-001"
    revised = deepcopy(asset)
    revised.update(expected_seq=1)
    revised["data"]["code"] = "设备-修订002"
    updated = case.app.accounting_apply(case.request(objects=[revised]))
    assert updated["completion"] == "complete" and updated["changes"] == "committed", updated
    assert updated["accounting"] == "unchanged"
    after = case.get(*business_ids)["businesses"]
    assert after == before
    assert all(result["reference"]["generation"] == 1 for b in after for result in b["results"])
    current = ready()
    assert current["snapshot_version"] != original["snapshot_version"]
    expected_data = deepcopy(original["data"])
    expected_assets = expected_data["collections"]["assets"]
    current_version = current["data"]["collections"]["assets"]["page"]["collection_version"]
    assert current_version != expected_assets["page"]["collection_version"]
    expected_assets["page"]["collection_version"] = current_version
    expected_assets["items"][0]["code"] = "设备-修订002"
    assert current["data"] == expected_data


def test_frozen_card_type_does_not_follow_current_classification_after_correction(case):
    case.seed(expense=False)
    acquired, _ = _asset(case, activate=True)
    save(case.app, case.request(case.expense("1000", period="2026-02")))
    assert case.close(case.check("2026-02"))["state"] == "closed"
    changed = deepcopy(acquired)
    changed["data"]["asset"].update(asset_type="intangible", cost_fen="132000")
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
    command["mutations"] = [{"op": "replace", "expected_seq": 1, "fact": changed}]
    save(case.app, command)
    january, _ = publish(case.app, case.company)
    original = page(case.app, january)["data"]
    assert original["collections"]["assets"]["items"][0]["asset_type"] == "fixed"
    assert (
        original["fixed_asset_cost_fen"] == "120000"
        and original["intangible_asset_cost_fen"] == "0"
    )
    march, _ = publish(case.app, case.company, "2026-03")
    current = page(case.app, march)["data"]
    assert current["month_acquired_count"] == 0 and current["month_cost_adjustment_fen"] == "12000"
    assert current["fixed"]["month_depreciation_fen"] == "-10000"
    assert current["intangible"]["month_amortization_fen"] == "33000"
    assert current["month_charge_fen"] == "23000"
