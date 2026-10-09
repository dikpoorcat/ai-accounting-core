"""Displayed historical values keep exact sources independently of accounting snapshots."""

import json

import pytest
from entity_fixture import save_entity_display_profile, seed_entities
from test_dashboard_projection import diagnostic_open_items, diagnostic_vouchers
from test_dashboard_voucher_adapter import activity_members
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401
from test_payroll_corrections import company as company  # noqa: F401
from test_payroll_tax_declarations import declare
from test_reimbursement_assets import accepted_batch, batch_card, pay
from test_reimbursement_assets import book as _asset_book

from ai_accounting.kernel import dashboard
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.domains.assets import ReimbursedAsset, ReimbursedAssetBatch
from ai_accounting.kernel.domains.transactions import Payment
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.response_contracts import http_response, validate_response
from ai_accounting.kernel.tax_import import TaxImportIdentity

asset_book = _asset_book


def profile(book, kind, entity_id, revision=0, **fields):
    return save_entity_display_profile(
        book,
        {"kind": kind, "entity_id": entity_id, "source": f"明确资料 {revision + 1}", **fields},
        expected_revision=revision,
        request_id=f"display:{kind}:{entity_id}:{revision}",
    )


def frozen_rows(book):
    with book.store.connection(read_only=True) as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("period_close", "voucher_version", "voucher_line", "calculation")
        }


def test_mixed_historical_fields_reach_employee_and_business_outputs(company, monkeypatch):
    company.publish("january", "february")
    old = profile(company.engine, "employee", "employee", display_name="原姓名")
    business = profile(company.engine, "business", "january", display_name="原业务", note="")
    company.close("2026-01")
    frozen = frozen_rows(company.engine)
    latest = profile(
        company.engine,
        "employee",
        "employee",
        1,
        display_name="新姓名",
        employment_start="2025-12",
        employment_end="2026-04",
        evidence_digest=company.owner_confirmation,
    )
    note = profile(
        company.engine, "business", "january", 1, display_name="新业务", note="后来补齐备注"
    )
    calls = []
    original = dashboard.recorded_times

    def counted(connection, references):
        calls.append(set(references))
        return original(connection, references)

    monkeypatch.setattr(dashboard, "recorded_times", counted)
    response = Dashboard(company.engine).employees("2026-01")
    assert validate_response("dashboard_employees", response) == response
    wire = http_response("dashboard_employees", response)
    assert wire["data"]["collections"]["employees"]["items"][0]["employment_end_date"] == "2026-04"
    employee = response["data"]["collections"]["employees"]["items"][0]
    assert calls == []
    assert employee["name"] == "原姓名" and employee["in_period"] is True
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        selected = snapshot.profile("employee", "employee")
        person = snapshot.party_details("employee")
        payroll_profile = snapshot.fact(snapshot.fact_ids_of_kind("payroll_profile")[0])
        tax_start = snapshot.fact_source(payroll_profile, field="withholding_start_date")
        snapshot.attach_recorded_times()
    sources = {
        "name": person["field_sources"]["name"],
        "employment_end_date": selected["field_sources"]["employment_end"],
        "record_status": selected["field_sources"]["employment_status"],
        "tax_withholding_start_date": tax_start,
    }
    assert sources["name"]["id"] == old["id"]
    assert sources["name"]["basis"] == "frozen"
    assert sources["employment_end_date"]["id"] == latest["id"]
    assert sources["employment_end_date"]["revision"] == latest["revision"]
    assert sources["employment_end_date"]["source"] == "明确资料 2"
    assert sources["employment_end_date"]["evidence_digest"] == company.owner_confirmation
    assert sources["employment_end_date"]["basis"] == "current_supplement"
    assert sources["employment_end_date"]["recorded_at"]
    assert selected["employment_status"] == "unknown"
    assert sources["record_status"]["id"] == old["id"]
    assert sources["tax_withholding_start_date"]["source_type"] == "fact"
    assert sources["tax_withholding_start_date"]["basis"] == "frozen"
    assert response["read_semantics"]["knowledge"] == "current_knowledge"
    assert response["read_semantics"]["accounting"] == "as_posted"
    assert response["read_semantics"]["business_basis"] == "frozen_adoption"
    brief = Dashboard(company.engine).brief("2026-01")["data"]
    voucher = diagnostic_vouchers(company.engine, "2026-01")[0]
    management = voucher["components"][0]["management"]
    assert management["version"] == business["revision"]
    assert management["version_scope"] == "base_profile"
    assert management["metadata"]["description"] == "后来补齐备注"
    assert management["field_sources"]["description"]["id"] == note["id"]
    assert voucher["field_sources"]["list_summary"]["id"] == business["id"]
    group = brief["collections"]["activity"]["items"][0]
    assert group["title"] == "工资计提"
    assert "vouchers" not in brief["collections"]
    activity = activity_members(company.engine, "2026-01")[0]
    assert activity["title"] == voucher["list_summary"] == "原业务"
    assert activity["party"] == "原姓名"
    assert activity["description"] == "原业务（2026-01）；后来补齐备注"
    assert voucher["display_summary"] == "原业务（2026-01） · 原姓名；后来补齐备注"
    assert any(item["id"] == note["id"] for item in voucher["field_sources"]["display_summary"])
    assert frozen_rows(company.engine) == frozen
    assert (
        Display(company.engine).display_profiles(period="2026-01")["profiles"]["employee"][
            "employee"
        ]["id"]
        == old["id"]
    )


@pytest.mark.parametrize(
    ("start", "end", "conflict"),
    [
        ("2026-03-31", "2026-03", False),
        ("2026-03", "2026-03-01", False),
        ("2026-03-31", "2026-03-01", True),
        ("2026-03", "2026-02", True),
    ],
)
def test_mixed_employment_dates_only_conflict_when_precision_proves_inversion(
    engine, start, end, conflict
):
    save(engine)
    publish(engine)
    old = profile(engine, "employee", "person", employment_start=start)
    close(engine)
    latest = profile(
        engine, "employee", "person", 1, employment_start="2026-01", employment_end=end
    )
    with Dashboard(engine)._snapshot("2026-01") as snapshot:
        selected = snapshot.profile("employee", "person")
        assert bool(selected["field_conflicts"]) == conflict
        assert selected["employment_start"] == start and selected["employment_end"] == end
        assert selected["field_sources"]["employment_start"]["id"] == old["id"]
        assert selected["field_sources"]["employment_end"]["id"] == latest["id"]


def test_date_conflict_cannot_claim_a_historical_employee_is_in_period(company):
    company.publish("january", "february")
    profile(company.engine, "employee", "employee", employment_start="2026-01-20")
    company.close("2026-01")
    profile(
        company.engine,
        "employee",
        "employee",
        1,
        employment_start="2025-12",
        employment_end="2026-01-05",
    )
    response = Dashboard(company.engine).employees("2026-01")
    validate_response("dashboard_employees", response)
    wire = http_response("dashboard_employees", response)
    employee = response["data"]["collections"]["employees"]["items"][0]
    assert employee["period_state"] == "unknown" and employee["in_period"] is None
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        assert (
            snapshot.profile("employee", "employee")["field_conflicts"][0]["code"]
            == "employment_interval_conflict"
        )
    assert wire["data"]["collections"]["employees"]["items"][0]["in_period"] is None


def test_false_and_empty_management_fields_keep_their_actual_selected_sources(engine):
    save(engine)
    publish(engine)
    account = profile(engine, "fund_account", "bank", active=False, note="")
    periods = Periods(engine)
    periods.management(
        "charge",
        note="",
        payment_period=None,
        payment_category=None,
        expected_revision=0,
        request_id="management:1",
    )
    close(engine)
    latest = profile(engine, "fund_account", "bank", 1, active=True, note="后补用途")
    periods.management(
        "charge",
        note="后补说明",
        payment_period=None,
        payment_category=None,
        expected_revision=1,
        request_id="management:2",
    )
    with Dashboard(engine)._snapshot("2026-01") as snapshot:
        selected = snapshot.profile("fund_account", "bank")
        assert selected["active"] is False
        assert selected["field_sources"]["active"]["id"] == account["id"]
        assert selected["note"] == "后补用途"
        assert selected["field_sources"]["note"]["id"] == latest["id"]
    component = diagnostic_vouchers(engine, "2026-01")[0]["components"][0]
    metadata = component["management"]
    assert metadata["metadata"]["description"] == "后补说明"
    assert metadata["field_sources"]["description"]["source_type"] == "management"
    assert metadata["field_sources"]["description"]["revision"] == 2
    assert metadata["field_sources"]["description"]["recorded_at"] is None


def test_payee_and_tax_identity_fallbacks_keep_exact_sources(company):
    seed_entities(company.engine, [("payee-only", "person", None)])
    company.publish("january", "february")
    export = Exports(company.engine)
    payee = export.save_payee(
        "payee-only",
        name="原收款人",
        account="00123",
        evidence_digest=company.owner_confirmation,
        expected_revision=0,
        request_id=company.request(),
    )
    company.close("2026-01")
    export.save_payee(
        "payee-only",
        name="后来的收款人",
        account="00456",
        evidence_digest=company.owner_confirmation,
        expected_revision=1,
        request_id=company.request(),
    )
    identity = company.save(
        TaxImportIdentity(
            period="2026-02",
            employee_id="employee",
            employee_code="0007",
            name="申报姓名",
            document_type="居民身份证",
            document_number="001234567890123456",
        ),
        "tax-identity",
    )
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        party = snapshot.party_details("payee-only")
        snapshot.attach_recorded_times()
        assert party["name"] == "原收款人"
        assert party["id"] == payee["payee_revision_id"]
        assert party["field_sources"]["name"]["basis"] == "frozen"
        assert party["field_sources"]["name"]["recorded_at"]
    employee = Dashboard(company.engine).employees("2026-01")["data"]["collections"]["employees"][
        "items"
    ][0]
    assert employee["name"] == "申报姓名"
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        assert snapshot.party_code("employee") == "0007"
        person = snapshot.party_details("employee")
        code, code_sources = snapshot.party_code_details("employee")
        assert person["field_sources"]["name"]["id"] == identity["fact_id"]
        assert person["field_sources"]["name"]["basis"] == "current_supplement"
        assert code_sources[0]["id"] == identity["fact_id"]


def test_declaration_recording_time_is_separate_from_business_month_and_actual_date(company):
    company.publish("january", "february")
    company.close("2026-01")
    frozen = frozen_rows(company.engine)
    _, saved = declare(company, period="2026-02", declaration_date="2026-02-06")
    response = Dashboard(company.engine).employees("2026-01")
    employee = response["data"]["collections"]["employees"]["items"][0]
    assert {"payroll_sources", "settlement_events"}.isdisjoint(response["data"]["collections"])
    assert "declared_tax_fen" not in employee
    declaration = BusinessQueries(company.engine).business_status("declared", "2026-01")[
        "latest_fact"
    ]
    assert declaration["id"] == saved["fact_id"]
    assert declaration["period"] == "2026-02"
    assert declaration["data"]["tax_period"] == "2026-01"
    assert declaration["data"]["declaration_date"] == "2026-02-06"
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        metadata = snapshot.fact_source(declaration)
        snapshot.attach_recorded_times()
        assert metadata["basis"] == "current_supplement"
        assert (
            metadata["recorded_at"]
            and metadata["recorded_at"] != declaration["data"]["declaration_date"]
        )
    assert frozen_rows(company.engine) == frozen


def test_asset_and_fund_account_fields_reach_the_actual_dashboard_outputs(asset_book):
    engine, save, publish = asset_book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    save("reimbursed_asset", "chair", batch_card(30000, asset_id="chair"))
    publish("batch", "computer", "chair")
    save(
        "payment",
        "alice-paid",
        pay("reimbursed_asset_batch", "batch", "alice", "alice", 90000),
    )
    publish("alice-paid")
    asset = profile(engine, "asset", "computer", display_name="工作电脑", display_number="A001")
    bank = profile(engine, "fund_account", "bank", display_name="基本户", active=False)
    assets = Dashboard(engine).assets("2026-03", section="assets", asset_filter="fixed")["data"][
        "collections"
    ]["assets"]["items"]
    computer = next(item for item in assets if item["asset_id"] == "computer")
    assert computer["name"] == "工作电脑" and computer["code"] == "A001"
    with Dashboard(engine)._snapshot("2026-03") as snapshot:
        selected = snapshot.profile("asset", "computer")
        snapshot.attach_recorded_times()
        assert selected["field_sources"]["display_name"]["id"] == asset["id"]
        assert selected["field_sources"]["display_name"]["recorded_at"]
    funds = Dashboard(engine).funds("2026-03")["data"]
    account = next(
        item for item in funds["collections"]["accounts"]["items"] if item["account_id"] == "bank"
    )
    assert account["name"] == "基本户" and account["active"] is False
    with Dashboard(engine)._snapshot("2026-03") as snapshot:
        selected_bank = snapshot.profile("fund_account", "bank")
        assert selected_bank["field_sources"]["active"]["id"] == bank["id"]
        assert selected_bank["field_sources"]["display_name"]["id"] == bank["id"]
    assert funds["collections"]["movements"]["items"][0]["account_name"] == "基本户"


def test_asset_creditor_names_and_single_payee_fallback_keep_later_sources(company, monkeypatch):
    company.publish("january", "february")
    company.save(ReimbursedAssetBatch.model_validate_json(json.dumps(accepted_batch())), "batch")
    company.save(ReimbursedAsset.model_validate_json(json.dumps(batch_card())), "computer")
    company.save(
        ReimbursedAsset.model_validate_json(json.dumps(batch_card(30000, asset_id="chair"))),
        "chair",
    )
    company.publish("batch", "computer", "chair")
    alice = profile(company.engine, "employee", "alice", display_name="关账前甲")
    company.close("2026-01")
    company.close("2026-02")
    frozen = frozen_rows(company.engine)
    profile(company.engine, "employee", "alice", 1, display_name="后来甲")
    bob = Exports(company.engine).save_payee(
        "bob",
        name="后来乙",
        account="00123",
        evidence_digest=company.owner_confirmation,
        expected_revision=0,
        request_id=company.request(),
    )
    calls = []
    original = dashboard.recorded_times

    def counted(connection, references):
        calls.append(set(references))
        return original(connection, references)

    monkeypatch.setattr(dashboard, "recorded_times", counted)
    assets = Dashboard(company.engine).assets("2026-02", section="assets", asset_filter="fixed")[
        "data"
    ]["collections"]["assets"]["items"]
    assert calls == []
    computer = next(item for item in assets if item["asset_id"] == "computer")
    assert "source_parties" not in computer
    assert computer["settlement_scope"] == "本验收批次结算"
    with Dashboard(company.engine)._snapshot("2026-02") as snapshot:
        sources = [
            snapshot.party_details(ident)["field_sources"]["name"] for ident in ("alice", "bob")
        ]
        snapshot.attach_recorded_times()
    assert [(source["id"], source["basis"]) for source in sources] == [
        (alice["id"], "frozen"),
        (bob["payee_revision_id"], "current_supplement"),
    ]
    assert all(source["recorded_at"] for source in sources)
    Dashboard(company.engine).brief("2026-02")["data"]
    diagnostic = diagnostic_open_items(company.engine, "2026-02")
    category = next(item for item in diagnostic["categories"] if item["key"] == "employee_payables")
    open_items = Dashboard(company.engine).brief("2026-02", section="open_items")["data"][
        "collections"
    ]["open_items"]["items"]
    item = next(item for item in diagnostic["collection"]["items"] if item["party_key"] == "bob")
    assert any(item["party"] == "后来乙" for item in open_items)
    group = next(item for item in category["groups"] if item["key"] == "bob")
    assert item["party"] == group["party"] == "后来乙"
    assert item["field_sources"] == group["field_sources"]
    assert item["field_sources"]["party"] == sources[1]
    assert frozen_rows(company.engine) == frozen
    company.save(
        Payment.model_validate_json(
            json.dumps(pay("reimbursed_asset_batch", "batch", "bob", "bob", 60000))
        ),
        "bob-paid",
    )
    company.publish("bob-paid")
    movement_items = Dashboard(company.engine).business_status(
        "2026-03", "batch", section="settlement_events"
    )["data"]["collections"]["settlement_events"]["items"]
    movement = next(item for item in movement_items if item["subject_id"] == "bob-paid")
    assert movement["source_subject_id"] == "batch"
    exact = BusinessQueries(company.engine).business_status("bob-paid", "2026-03")["settlements"][
        "movements"
    ][0]
    assert exact["source_business"] == {
        "kind": "reimbursed_asset_batch",
        "subject_id": "batch",
    }
    assert exact["recipient_id"] == "bob"
    assert movement["relation_state"] == "resolved"
    voucher = diagnostic_vouchers(company.engine, "2026-03")[0]
    voucher_settlement = next(
        item for item in voucher["settlements"] if item["source_subject_id"] == "batch"
    )
    assert voucher_settlement["party"] == "后来乙"
    assert voucher_settlement["field_sources"]["party"]["id"] == bob["payee_revision_id"]
    assert voucher_settlement["field_sources"]["party"]["basis"] == "current"
    assert voucher_settlement["field_sources"]["party"]["recorded_at"] == sources[1]["recorded_at"]
    assert voucher["lines"][0]["field_sources"]["party"] == [
        voucher_settlement["field_sources"]["party"]
    ]
