"""Owner reads retain full totals while technical presentation reads stop."""

import pytest
import test_investments as investments
from entity_fixture import seed_entities
from test_banking import book as _bank_book
from test_close_review_transport import prepared_company
from test_close_review_transport import resident as _resident
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_provenance import profile as display_profile
from test_payroll_corrections import company as payroll_company_fixture
from test_reimbursement_assets import asset as reimbursed_asset
from test_reimbursement_assets import book as asset_book_fixture

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.close_review import public_owner_review
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _Snapshot
from ai_accounting.kernel.dashboard_metadata import Records, TaxIdentityCandidates
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.response_contracts import http_response
from ai_accounting.kernel.security.window import _owner_review_text
from ai_accounting.kernel.tax_import import TaxImportIdentity

bank_book, resident = _bank_book, _resident
payroll_company, asset_book = payroll_company_fixture, asset_book_fixture


def test_brief_default_is_bounded_and_does_not_load_technical_payload(bank_book, monkeypatch):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 31)

    def forbidden(*_args, **_kwargs):
        pytest.fail("owner brief loaded a technical presentation graph")

    monkeypatch.setattr(BusinessQueries, "_period_readiness", forbidden)
    monkeypatch.setattr(_Snapshot, "voucher", forbidden)
    monkeypatch.setattr(_Snapshot, "owner_voucher", forbidden)
    monkeypatch.setattr(_Snapshot, "evidence_details", forbidden)
    import ai_accounting.kernel.close_review as review_module
    import ai_accounting.kernel.dashboard as dashboard_module

    summary_reads = {"position": 0, "workforce": 0}
    original_position = dashboard_module._position
    original_workforce = dashboard_module._brief_workforce_cost

    def position(snap):
        summary_reads["position"] += 1
        return original_position(snap)

    def workforce(snap):
        summary_reads["workforce"] += 1
        return original_workforce(snap)

    monkeypatch.setattr(dashboard_module, "_position", position)
    monkeypatch.setattr(dashboard_module, "_brief_workforce_cost", workforce)
    monkeypatch.setattr(review_module, "business_adopted_basis", forbidden)
    dashboard = Dashboard(engine)
    response = dashboard.brief("2026-09")
    data = response["data"]
    assert response["schema_version"] == 17
    assert data["month_state"] == "open"
    assert data["owner_review_request"] is None
    assert data["activity_count"] == 31
    assert len(data["collections"]["activity"]["items"]) == 20
    assert data["voucher_count"] == 31
    assert "vouchers" not in data["collections"]
    assert data["group_count"] == 31
    assert data["funds_overview"]["total_fen"] == 31
    assert data["financial_position"]["assets_fen"] == 31
    assert data["financial_position"]["bank_fen"] == 31
    assert data["financial_position"]["liabilities_fen"] == 0
    assert data["financial_position"]["equity_fen"] == 31
    assert data["financial_position"]["bank_calculation"] == {
        "opening_fen": 0, "inflow_fen": 31, "outflow_fen": 0,
    }
    assert data["workforce_cost"]["total_fen"] == 0
    assert summary_reads == {"position": 1, "workforce": 1}
    assert data["owner_tasks"] == []
    # Missing statement/matching work belongs to the AI accountant and funds
    # detail; it does not change the confirmed money into an owner warning.
    assert data["risks"] == []
    assert not (
        {"period_preparation", "adopted_basis", "total_debit_fen", "vouchers"} & data.keys()
    )
    row = data["collections"]["activity"]["items"][0]
    assert row["amount_fen"] == 1
    assert not ({"lines", "evidence", "field_sources", "journal_total_fen"} & row.keys())
    following = dashboard.brief(
        "2026-09",
        section="activity",
        expected_version=response["snapshot_version"],
        cursor=data["collections"]["activity"]["page"]["next_cursor"],
    )
    assert len(following["data"]["collections"]["activity"]["items"]) == 11
    assert following["data"]["funds_overview"] == data["funds_overview"]
    assert {"financial_position", "workforce_cost"}.isdisjoint(following["data"])
    assert summary_reads == {"position": 1, "workforce": 1}
    assert http_response("dashboard_brief", response)["data"]["funds_overview"]["total_fen"] == "31"
    # An explicit former mode cannot re-enable the removed technical workload.
    assert (
        dashboard.brief("2026-09", preparation="complete")["data"]["funds_overview"]
        == data["funds_overview"]
    )


def test_page_and_native_window_project_the_same_exact_review(resident):
    _, _, _, _, _, execute, _, preview = prepared_company(resident)
    stored = preview["manifest"]["owner_review"]
    public = public_owner_review(stored)
    response = execute("dashboard_close_review", period="2026-01", preview_digest=preview["digest"])
    assert response["schema_version"] == 2
    assert response["owner_review"] == public
    assert "collection" not in response
    assert stored["presentation_contract"] == "ai-accounting-kernel/2/close-review/1"
    assert stored["collections"]
    text = _owner_review_text(stored)
    assert "本月收入：¥0.00" in text
    assert "月末账面资金：¥0.00" in text
    for forbidden in ("凭证", "借方", "贷方", "政策", "保全依据", "精确来源"):
        assert forbidden not in text
    assert set(public["amounts"]) == {
        "month_revenue_fen",
        "month_expense_fen",
        "month_result_fen",
        "funds_total_fen",
        "actual_receipts_fen",
        "actual_payments_fen",
    }


def _owner_without_technical_reads(monkeypatch):
    import ai_accounting.kernel.business_queries as query_module

    def forbidden(*_args, **_kwargs):
        pytest.fail("owner business detail expanded vouchers or recorded technical provenance")

    monkeypatch.setattr(BusinessQueries, "_voucher", forbidden)
    monkeypatch.setattr(query_module, "recorded_times", forbidden)


def _exact_object_reads(monkeypatch):
    """Observe named reads; a business object must not reopen the employee roster."""
    import ai_accounting.kernel.entities as entity_module

    profiles, tax_identities = [], []
    original_profile, original_tax = Records._query, TaxIdentityCandidates.prime

    def profile_scope(self, *, records=False, identifiers=None):
        if records and self.record_type in {"entity_profile", "payee"}:
            assert identifiers is not None
            profiles.append((self.record_type, self.kind, set(identifiers)))
        return original_profile(self, records=records, identifiers=identifiers)

    def tax_scope(self, identifiers):
        identifiers = set(identifiers)
        missing = identifiers - self.cache.keys() - self.missing
        if missing and self.available:
            tax_identities.append(missing)
        return original_tax(self, identifiers)

    def forbidden(*_args, **_kwargs):
        pytest.fail("exact business detail enumerated employee-roster membership")

    monkeypatch.setattr(entity_module, "employee_entities", forbidden)
    monkeypatch.setattr(Records, "_query", profile_scope)
    monkeypatch.setattr(TaxIdentityCandidates, "prime", tax_scope)
    return profiles, tax_identities


@pytest.mark.parametrize("entity_kind", ["person", "organization"])
@pytest.mark.parametrize("state", ["current", "frozen", "supplement"])
def test_owner_business_reuses_exact_payee_name_and_frozen_supplements(
    bank_book, monkeypatch, entity_kind, state
):
    engine, save, publish, proof = bank_book
    seed_entities(engine, [("named-creditor", entity_kind, None)])
    save(
        "expense",
        "expense-payee",
        {
            "period": "2026-09",
            "amount_fen": 321,
            "counterparty_id": "named-creditor",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish("expense-payee")
    export = Exports(engine)
    if state != "supplement":
        export.save_payee(
            "named-creditor",
            name="原确认收款人",
            account="001",
            evidence_digest=proof,
            expected_revision=0,
            request_id="original-payee",
        )
    if state != "current":
        investments.close(engine, "2026-09")
        export.save_payee(
            "named-creditor",
            name="后来确认收款人",
            account="002",
            evidence_digest=proof,
            expected_revision=int(state == "frozen"),
            request_id="later-payee",
        )
    # Unrelated person profiles may exist, but this exact object requires none of them.
    seed_entities(engine, [(f"unrelated-person-{index}", "person", None) for index in range(35)])
    _owner_without_technical_reads(monkeypatch)
    profile_reads, tax_reads = _exact_object_reads(monkeypatch)
    response = Dashboard(engine).business_status("2026-09", "expense-payee")
    data = response["data"]
    expected_name = "后来确认收款人" if state == "supplement" else "原确认收款人"
    assert [
        (item["entity_id"], item["values"]["display_name"])
        for item in data["display_profiles"]["counterparties"]
    ] == [("named-creditor", expected_name)]
    assert data["current_business_result"]["amount_fen"] == 321
    assert (data["frozen_adoption"] is None) == (state == "current")
    assert all(ids <= {"named-creditor"} for _, _, ids in profile_reads)
    assert not any(kind == "employee" for _, kind, _ in profile_reads)
    assert tax_reads == []
    assert (
        http_response("dashboard_business_status", response)["data"]["display_profiles"][
            "counterparties"
        ][0]["values"]["display_name"]
        == expected_name
    )


@pytest.mark.parametrize("state", ["current", "frozen", "supplement"])
def test_owner_payroll_reuses_exact_tax_identity_name_and_employee_code(
    payroll_company, monkeypatch, state
):
    company = payroll_company
    company.publish("january", "february")
    identity = TaxImportIdentity(
        period="2026-02" if state == "supplement" else "2026-01",
        employee_id="employee",
        employee_code="00007",
        name="已确认工资姓名",
        document_type="居民身份证",
        document_number="001234567890123456",
    )
    if state != "supplement":
        company.save(identity, "owner-tax-identity")
    if state != "current":
        company.close("2026-01")
    if state == "supplement":
        company.save(identity, "owner-tax-identity")
    seed_entities(
        company.engine, [(f"unrelated-person-{index}", "person", None) for index in range(35)]
    )
    _owner_without_technical_reads(monkeypatch)
    profile_reads, tax_reads = _exact_object_reads(monkeypatch)
    response = Dashboard(company.engine).business_status("2026-01", "january")
    data = response["data"]
    profile = data["display_profiles"]["employees"][0]
    assert profile["entity_id"] == "employee"
    assert profile["values"]["display_name"] == "已确认工资姓名"
    assert profile["values"]["display_number"] == "00007"
    assert data["current_business_result"]["amount_fen"] == 1_000_000
    assert (data["frozen_adoption"] is None) == (state == "current")
    assert all(ids <= {"employee"} for _, _, ids in profile_reads)
    assert not any(kind == "employee" for _, kind, _ in profile_reads)
    assert tax_reads and all(ids <= {"employee"} for ids in tax_reads)
    assert (
        http_response("dashboard_business_status", response)["data"]["display_profiles"][
            "employees"
        ][0]["values"]["display_number"]
        == "00007"
    )


def test_exact_business_beyond_twenty_uses_explicit_account_and_party_profiles(
    bank_book, monkeypatch
):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 31)
    display_profile(
        engine, "fund_account", "bank-a", display_name="经营账户", display_number="尾号1234"
    )
    dashboard = Dashboard(engine)
    first = dashboard.brief("2026-09")
    assert len(first["data"]["collections"]["activity"]["items"]) == 20
    loaded = {item["subject_id"] for item in dashboard.brief(
        "2026-09", section="vouchers", expected_version=first["snapshot_version"],
    )["data"]["collections"]["vouchers"]["items"]}
    subject = next(
        f"filter-2026-09-{index:04}"
        for index in range(31)
        if f"filter-2026-09-{index:04}" not in loaded
    )
    index = int(subject.rsplit("-", 1)[1])
    party_id = f"filter-owner-2026-09-{index:04}"
    display_profile(engine, "counterparty", party_id, display_name="实际出资人")
    display_profile(
        engine, "business", subject, display_name="单笔资本投入", display_number="业务单号独立"
    )
    _owner_without_technical_reads(monkeypatch)
    response = dashboard.business_status("2026-09", subject)
    data = response["data"]
    assert data["identity"]["subject_id"] == subject
    assert data["current_business_result"]["amount_fen"] == 1
    assert data["frozen_adoption"] is None and data["closure"]["state"] == "open"
    assert data["settlements"]["obligations"] == []
    assert data["settlements"]["checking"] is False
    assert data["current_followups"]["settlements"]["checking"] is False
    profiles = data["display_profiles"]
    assert profiles["business"]["values"]["display_number"] == "业务单号独立"
    assert [
        (item["entity_id"], item["values"]["display_name"]) for item in profiles["fund_accounts"]
    ] == [("bank-a", "经营账户")]
    assert [
        (item["entity_id"], item["values"]["display_name"]) for item in profiles["counterparties"]
    ] == [(party_id, "实际出资人")]
    assert set(profiles) == {"business", "fund_accounts", "counterparties"}
    assert not ({"evidence", "trace_targets", "adopted_basis", "basis_integrity"} & data.keys())
    assert (
        http_response("dashboard_business_status", response)["data"]["current_business_result"][
            "amount_fen"
        ]
        == "1"
    )


def test_owner_payroll_objects_use_employee_identity_independent_of_business_number(
    payroll_company, monkeypatch
):
    payroll_company.publish("january", "february")
    engine = payroll_company.engine
    display_profile(engine, "employee", "employee", display_name="明确员工姓名")
    display_profile(engine, "business", "january", display_number="工资业务001")
    _owner_without_technical_reads(monkeypatch)
    response = Dashboard(engine).business_status("2026-01", "january")
    profiles = response["data"]["display_profiles"]
    assert profiles["business"]["values"]["display_number"] == "工资业务001"
    assert [
        (item["entity_id"], item["values"]["display_name"]) for item in profiles["employees"]
    ] == [("employee", "明确员工姓名")]
    assert response["data"]["current_business_result"]["amount_fen"] > 0
    assert all("field_sources" not in item["values"] for item in profiles["employees"])


def test_unpublished_owner_business_reads_verified_fact_objects_without_result(
    bank_book, monkeypatch
):
    engine, save, _, _ = bank_book
    save(
        "expense",
        "awaiting-expense",
        {
            "period": "2026-09",
            "amount_fen": 321,
            "counterparty_id": "supplier-awaiting",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    display_profile(engine, "counterparty", "supplier-awaiting", display_name="待核对供应商")
    _owner_without_technical_reads(monkeypatch)
    response = Dashboard(engine).business_status("2026-09", "awaiting-expense")
    data = response["data"]
    assert data["current_business_result"] is None and data["frozen_adoption"] is None
    assert [
        (item["entity_id"], item["values"]["display_name"])
        for item in data["display_profiles"]["counterparties"]
    ] == [("supplier-awaiting", "待核对供应商")]
    assert "data" not in data["latest_source"]
    assert "trace_targets" not in data and "evidence" not in data
    assert (
        http_response("dashboard_business_status", response)["data"]["current_business_result"]
        is None
    )


def test_unpublished_owner_business_rejects_model_valid_fact_with_wrong_digest(bank_book):
    from ai_accounting.kernel.domains.transactions import Expense

    engine, save, _, _ = bank_book
    fields = {
        "period": "2026-09",
        "amount_fen": 321,
        "counterparty_id": "supplier-awaiting",
        "expense_class": "administration",
        "creditor_kind": "supplier",
    }
    saved = save("expense", "awaiting-expense", fields)
    assert Expense(**(fields | {"amount_fen": 322})).amount_fen == 322
    with engine.store.connection() as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='immutable_fact_expense_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_fact_expense_UPDATE")
        connection.execute(
            "UPDATE fact_expense SET amount_fen=322 WHERE revision_id=?", (saved["fact_id"],)
        )
        connection.execute(trigger)
        connection.commit()
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).business_status("2026-09", "awaiting-expense")
    assert failure.value.code == "content_integrity_failed"
    assert failure.value.details["reason"] == "fact_digest_mismatch"


def test_withdrawn_owner_business_reads_last_verified_objects_without_result(
    bank_book, monkeypatch
):
    engine, save, _, proof = bank_book
    save(
        "expense",
        "withdrawn-expense",
        {
            "period": "2026-09",
            "amount_fen": 321,
            "counterparty_id": "withdrawn-creditor",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    display_profile(engine, "counterparty", "withdrawn-creditor", display_name="原业务供应商")
    preview = engine.preview_delete("withdrawn-expense", recording_error_evidence=proof)
    engine.delete(
        "withdrawn-expense",
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        recording_error_evidence=proof,
        request_id="withdraw-expense",
    )
    # Another explicit source keeps the selected period available after withdrawal.
    save(
        "expense",
        "retained-expense",
        {
            "period": "2026-09",
            "amount_fen": 123,
            "counterparty_id": "retained-creditor",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    _owner_without_technical_reads(monkeypatch)
    profile_reads, _ = _exact_object_reads(monkeypatch)
    response = Dashboard(engine).business_status("2026-09", "withdrawn-expense")
    data = response["data"]
    assert data["latest_source"]["deleted"] is True and data["review"]["status"] == "deleted"
    assert data["current_business_result"] is None and data["frozen_adoption"] is None
    assert data["settlements"]["obligations"] == []
    assert [
        (item["entity_id"], item["values"]["display_name"])
        for item in data["display_profiles"]["counterparties"]
    ] == [("withdrawn-creditor", "原业务供应商")]
    assert all(ids <= {"withdrawn-creditor"} for _, _, ids in profile_reads)
    assert http_response("dashboard_business_status", response)["data"]["latest_source"]["deleted"]


def test_owner_asset_objects_use_adopted_asset_and_explicit_creditors(asset_book, monkeypatch):
    engine, save, publish = asset_book
    save("reimbursed_asset", "asset-business-independent", reimbursed_asset())
    publish("asset-business-independent")
    display_profile(engine, "asset", "computer", display_name="研发电脑")
    display_profile(engine, "employee", "alice", display_name="员工甲")
    display_profile(engine, "employee", "bob", display_name="员工乙")
    display_profile(
        engine, "business", "asset-business-independent", display_number="资产验收单001"
    )
    _owner_without_technical_reads(monkeypatch)
    response = Dashboard(engine).business_status("2026-02", "asset-business-independent")
    profiles = response["data"]["display_profiles"]
    assert profiles["business"]["values"]["display_number"] == "资产验收单001"
    assert [(item["entity_id"], item["values"]["display_name"]) for item in profiles["assets"]] == [
        ("computer", "研发电脑")
    ]
    people = [item for group in ("employees", "counterparties") for item in profiles.get(group, [])]
    assert {(item["entity_id"], item["values"]["display_name"]) for item in people} == {
        ("alice", "员工甲"),
        ("bob", "员工乙"),
    }
    assert response["data"]["current_business_result"]["amount_fen"] == 120000


def test_frozen_detail_keeps_adopted_objects_amounts_and_core_proof(bank_book, monkeypatch):
    engine, save, publish, proof = bank_book
    fields = {
        "period": "2026-09",
        "amount_fen": 100,
        "counterparty_id": "adopted-supplier",
        "expense_class": "administration",
        "creditor_kind": "supplier",
    }
    save("expense", "expense-independent", fields)
    publish("expense-independent")
    display_profile(engine, "counterparty", "adopted-supplier", display_name="原采用供应商")
    display_profile(engine, "business", "expense-independent", display_number="原业务单001")
    investments.close(engine, "2026-09")
    save("expense", "expense-independent", fields | {"amount_fen": 140}, revision=1)
    preview = engine.preview(["expense-independent"], posting_period="2026-10")
    engine.confirm(
        ["expense-independent"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        posting_period="2026-10",
        request_id="expense-correction",
    )
    display_profile(
        engine,
        "business",
        "expense-independent",
        1,
        display_number="新业务单002",
        note="后来补齐经营用途",
    )
    display_profile(engine, "counterparty", "adopted-supplier", 1, display_name="原供应商后来改名")
    with monkeypatch.context() as patch:
        _owner_without_technical_reads(patch)
        response = Dashboard(engine).business_status("2026-09", "expense-independent")
    data = response["data"]
    assert data["closure"]["state"] == "exact_close"
    assert data["frozen_adoption"]["amount_fen"] == 100
    assert data["current_business_result"]["amount_fen"] == 140
    assert data["display_profiles"]["business"]["values"]["display_number"] == "原业务单001"
    assert [
        (item["entity_id"], item["values"]["display_name"])
        for item in data["display_profiles"]["counterparties"]
    ] == [("adopted-supplier", "原采用供应商")]
    assert data["display_profiles"]["business"]["values"]["note"] == "后来补齐经营用途"
    assert "selection_proof" not in data["frozen_adoption"]
    import ai_accounting.kernel.business_queries as query_module

    calls = []
    original = query_module.recorded_times

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(query_module, "recorded_times", counted)
    core = BusinessQueries(engine).business_status("expense-independent", "2026-09")
    assert calls
    assert core["frozen_adoption"]["selection_proof"]["basis"] == "direct_adoption"
    assert core["latest_fact"]["data"]["counterparty_id"] == "adopted-supplier"
    assert core["latest_fact"]["evidence"]
    assert core["current_business_result"]["voucher_versions"]
