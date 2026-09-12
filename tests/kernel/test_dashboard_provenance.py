"""Displayed historical values keep exact sources independently of accounting snapshots."""

import json

import pytest
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401
from test_payroll_corrections import company as company  # noqa: F401
from test_payroll_tax_declarations import declare
from test_reimbursement_assets import accepted_batch, batch_card, pay
from test_reimbursement_assets import book as _asset_book

from ai_accounting.kernel import dashboard
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.domains.assets import ReimbursedAsset, ReimbursedAssetBatch
from ai_accounting.kernel.domains.transactions import Payment
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.tax_import import TaxImportIdentity

asset_book = _asset_book


def profile(book, kind, entity_id, revision=0, **fields):
    return Display(book).save_display_profile(
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
    employee = response["data"]["employees"]["items"][0]
    assert len(calls) == 1
    assert employee["name"] == "原姓名" and employee["in_period"] is True
    sources = employee["field_sources"]
    assert sources["name"]["id"] == old["id"]
    assert sources["name"]["basis"] == "frozen"
    assert sources["employment_end_date"]["id"] == latest["id"]
    assert sources["employment_end_date"]["revision"] == 2
    assert sources["employment_end_date"]["source"] == "明确资料 2"
    assert sources["employment_end_date"]["evidence_digest"] == company.owner_confirmation
    assert sources["employment_end_date"]["basis"] == "current_supplement"
    assert sources["employment_end_date"]["recorded_at"]
    assert employee["record_status"] == "unknown"
    assert sources["record_status"]["id"] == old["id"]
    assert sources["tax_withholding_start_date"]["source_type"] == "fact"
    assert sources["tax_withholding_start_date"]["basis"] == "frozen"
    assert response["read_semantics"]["knowledge"] == "current_knowledge"
    assert response["read_semantics"]["accounting"] == "frozen_close"
    brief = Dashboard(company.engine).brief("2026-01")["data"]
    voucher = brief["vouchers"][0]
    management = voucher["components"][0]["management"]
    assert management["version"] == business["revision"]
    assert management["version_scope"] == "base_profile"
    assert management["metadata"]["description"] == "后来补齐备注"
    assert management["field_sources"]["description"]["id"] == note["id"]
    assert voucher["field_sources"]["list_summary"]["id"] == business["id"]
    activity = brief["activity_groups"][0]["rows"][0]
    assert activity["field_sources"]["title"]["id"] == business["id"]
    assert any(
        item["id"] == note["id"] for item in activity["field_sources"]["display_description"]
    )
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
    employee = Dashboard(company.engine).employees("2026-01")["data"]["employees"]["items"][0]
    assert employee["period_state"] == "unknown" and employee["in_period"] is None
    assert employee["field_conflicts"][0]["code"] == "employment_interval_conflict"


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
    component = Dashboard(engine).brief("2026-01")["data"]["vouchers"][0]["components"][0]
    metadata = component["management"]
    assert metadata["metadata"]["description"] == "后补说明"
    assert metadata["field_sources"]["description"]["source_type"] == "management"
    assert metadata["field_sources"]["description"]["revision"] == 2
    assert metadata["field_sources"]["description"]["recorded_at"] is None


def test_payee_and_tax_identity_fallbacks_keep_exact_sources(company):
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
    employee = Dashboard(company.engine).employees("2026-01")["data"]["employees"]["items"][0]
    assert employee["name"] == "申报姓名" and employee["code"] == "0007"
    assert employee["field_sources"]["name"]["id"] == identity["fact_id"]
    assert employee["field_sources"]["name"]["basis"] == "current_supplement"
    assert employee["field_sources"]["code"][0]["id"] == identity["fact_id"]


def test_declaration_recording_time_is_separate_from_business_month_and_actual_date(company):
    company.publish("january", "february")
    company.close("2026-01")
    frozen = frozen_rows(company.engine)
    _, saved = declare(company, period="2026-02", declaration_date="2026-02-06")
    response = Dashboard(company.engine).employees("2026-01")
    employee = response["data"]["employees"]["items"][0]
    source = employee["payroll_sources"][0]
    declaration = source["declarations"][0]
    assert declaration["fact_id"] == saved["fact_id"]
    assert declaration["recording_period"] == "2026-02"
    assert declaration["date"] == "2026-02-06" and declaration["recorded_later"]
    assert declaration["recorded_at"] == declaration["source_metadata"]["recorded_at"]
    assert declaration["recorded_at"] and declaration["recorded_at"] != declaration["date"]
    assert declaration["source_metadata"]["basis"] == "current_supplement"
    assert employee["field_sources"]["declared_tax_fen"]["id"] == saved["fact_id"]
    assert (
        employee["field_sources"]["declared_tax_fen"]["recorded_at"] == declaration["recorded_at"]
    )
    assert frozen_rows(company.engine) == frozen


def test_asset_and_fund_account_fields_reach_the_actual_dashboard_outputs(asset_book):
    engine, save, publish = asset_book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    save("reimbursed_asset", "chair", batch_card(30000))
    publish("batch", "computer", "chair")
    save(
        "payment",
        "alice-paid",
        pay("reimbursed_asset_batch", "batch", "alice", "alice", 90000),
    )
    publish("alice-paid")
    asset = profile(engine, "asset", "computer", display_name="工作电脑", display_number="A001")
    bank = profile(engine, "fund_account", "bank", display_name="基本户", active=False)
    assets = Dashboard(engine).assets("2026-03")["data"]["fixed"]["items"]
    computer = next(item for item in assets if item["asset_id"] == "computer")
    assert computer["name"] == "工作电脑" and computer["code"] == "A001"
    assert computer["field_sources"]["name"]["id"] == asset["id"]
    assert computer["field_sources"]["name"]["recorded_at"]
    funds = Dashboard(engine).funds("2026-03")["data"]
    account = next(item for item in funds["accounts"] if item["account_id"] == "bank")
    assert account["name"] == "基本户" and account["active"] is False
    assert account["field_sources"]["active"]["id"] == bank["id"]
    movement = funds["movements"][0]
    assert movement["field_sources"]["account_name"]["id"] == bank["id"]


def test_asset_creditor_names_and_single_payee_fallback_keep_later_sources(company, monkeypatch):
    company.publish("january", "february")
    company.save(ReimbursedAssetBatch.model_validate_json(json.dumps(accepted_batch())), "batch")
    company.save(ReimbursedAsset.model_validate_json(json.dumps(batch_card())), "computer")
    company.save(ReimbursedAsset.model_validate_json(json.dumps(batch_card(30000))), "chair")
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
    assets = Dashboard(company.engine).assets("2026-02")["data"]["fixed"]["items"]
    assert len(calls) == 1
    computer = next(item for item in assets if item["asset_id"] == "computer")
    assert computer["source_parties"] == "关账前甲、后来乙"
    sources = computer["field_sources"]["source_parties"]
    assert [(source["id"], source["basis"]) for source in sources] == [
        (alice["id"], "frozen"),
        (bob["payee_revision_id"], "current_supplement"),
    ]
    assert all(source["recorded_at"] for source in sources)
    brief = Dashboard(company.engine).brief("2026-02")["data"]
    category = next(
        item for item in brief["open_items"]["categories"] if item["key"] == "employee_payables"
    )
    item = next(item for item in category["items"] if item["party_key"] == "bob")
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
    assets = Dashboard(company.engine).assets("2026-03")["data"]["fixed"]["items"]
    movement = next(
        item for item in assets[0]["settlements"][0]["movements"] if item["source_id"] == "bob-paid"
    )
    assert movement["party"] == "后来乙"
    assert movement["field_sources"]["party"]["id"] == bob["payee_revision_id"]
    assert movement["field_sources"]["party"]["basis"] == "current"
    assert movement["field_sources"]["party"]["recorded_at"] == sources[1]["recorded_at"]
    voucher = Dashboard(company.engine).brief("2026-03")["data"]["vouchers"][0]
    assert voucher["settlements"][0]["field_sources"]["party"] == movement["field_sources"]["party"]
    assert voucher["lines"][0]["field_sources"]["party"] == [movement["field_sources"]["party"]]
