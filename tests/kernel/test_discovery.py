import sqlite3

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.discovery import Discovery
from ai_accounting.kernel.discovery_indexes import rebuild_discovery_indexes
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


@pytest.fixture
def company(tmp_path):
    store = Store.create(
        tmp_path / "company.sqlite", production_bundle(), "co", "911100000000000001", "db"
    )
    return Engine(store)


def test_company_note_cas_survives_new_session_and_changes_only_management(company):
    first, second = Discovery(company), Discovery(Engine(company.store))
    before = first.company_context()
    result = first.update_company_note(
        "从事软件服务；工资来源另行登记", expected_revision=0, request_id="note"
    )
    assert result == first.update_company_note(
        "从事软件服务；工资来源另行登记", expected_revision=0, request_id="note"
    )
    assert second.company_context()["company_note"]["text"] == result["text"]
    with pytest.raises(KernelError) as error:
        second.update_company_note(
            "另一个会话的旧编辑", expected_revision=0, request_id="stale-note"
        )
    assert error.value.code == "company_note_conflict"
    assert error.value.details["current"]["revision"] == 1
    after = second.company_context()
    assert after["epochs"]["accounting"] == before["epochs"]["accounting"]
    assert after["epochs"]["material"] == before["epochs"]["material"]
    assert after["epochs"]["management"] == before["epochs"]["management"] + 1
    assert after["identity"]["company_id"] == "co"


def test_note_versions_and_evidence_are_append_only(company):
    proof = company.register_evidence(b"note source", "text/plain", "note", request_id="proof")[
        "digest"
    ]
    discovery = Discovery(company)
    discovery.update_company_note(
        "第一版", expected_revision=0, request_id="v1", evidence_digest=proof
    )
    discovery.update_company_note("第二版", expected_revision=1, request_id="v2")
    with company.store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM company_note_revision").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE company_note_revision SET text='overwritten' WHERE revision=1"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM company_note_revision WHERE revision=1")


def test_find_facts_recovers_people_periods_versions_and_publication_states(company):
    proof = company.register_evidence(b"source", "text/plain", "proof", request_id="proof")[
        "digest"
    ]
    employee = Entities(company).register_entity(
        "person", {"display_name": "Employee 1"}, source="test", request_id="employee"
    )["entity_id"]
    profile = {
        "period": "2026-02",
        "employee_id": employee,
        "effective_from": "2026-02",
        "effective_to": None,
        "withholding_start_date": "2026-02-01",
        "social_insurance_base_fen": None,
        "housing_fund_base_fen": None,
        "social_insurance_participating": False,
        "housing_fund_participating": False,
        "contribution_shortfall": "reject",
    }
    for revision in range(2):
        company.save_fact(
            "payroll_profile",
            "profile",
            profile | {"effective_to": None if not revision else "2026-12"},
            evidence=(proof,),
            expected_revision=revision,
            request_id=f"profile-{revision}",
        )
    query = Discovery(Engine(company.store))
    current = query.find_facts(period_from="2026-02", period_to="2026-02")
    assert current["schema_version"] == 2
    assert current["sort"] == "period_desc_fact_id_desc"
    assert len(current["items"]) == 1
    assert current["items"][0]["revision"] == 2
    assert current["items"][0]["evidence"] == [proof]
    assert query.find_facts(period_from="2026-03")["items"] == []
    superseded = query.find_facts(kind="payroll_profile", status="superseded")
    assert [item["revision"] for item in superseded["items"]] == [1]
    assert superseded["items"][0]["superseded"] is True
    first = query.find_facts(kind="payroll_profile", status="history", limit=1)
    second = query.find_facts(
        kind="payroll_profile", status="history", limit=1, cursor=first["next_cursor"]
    )
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["fact_id"] != second["items"][0]["fact_id"]
    assert second["next_cursor"] is None
    matched = query.find_facts(entity_id=employee, role="employee", status="history")
    assert len(matched["items"]) == 2
    assert all(item["identity_matches"] for item in matched["items"])
    assert all(
        match["entity_id"] == employee and match["role"] == "employee"
        for item in matched["items"]
        for match in item["identity_matches"]
    )
    assert len(query.find_facts(role="employee", status="history")["items"]) == 2


def test_find_sources_distinguishes_saved_published_and_deleted(company):
    proof = company.register_evidence(b"cost", "text/plain", "proof", request_id="proof")["digest"]
    vendor = Entities(company).register_entity(
        "organization", {"display_name": "Vendor"}, source="test", request_id="vendor"
    )["entity_id"]
    company.save_fact(
        "expense",
        "cost",
        {
            "period": "2026-01",
            "counterparty_id": vendor,
            "amount_fen": 100,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="cost",
    )
    query = Discovery(company)
    assert len(query.find_facts(status="pending")["items"]) == 1
    assert query.find_facts(status="published")["items"] == []
    preview = company.preview(["cost"])
    company.confirm(
        ["cost"], preview_digest=preview["digest"], epochs=preview["epochs"], request_id="publish"
    )
    published = query.find_facts(status="published")["items"]
    assert len(published) == 1
    assert published[0]["adoption"]["basis"] == "direct_publication"
    assert published[0]["adoption"]["calculation_id"] == published[0]["calculation_id"]
    assert published[0]["adoption"]["current"] is True
    preview = company.preview_delete("cost")
    company.delete(
        "cost", preview_digest=preview["digest"], epochs=preview["epochs"], request_id="delete"
    )
    assert query.find_facts()["items"] == []
    deleted = query.find_facts(status="deleted")["items"]
    assert len(deleted) == 1
    assert deleted[0]["adoption"]["current"] is False


def test_find_facts_cursor_binds_filters_and_versions(company):
    vendor = Entities(company).register_entity(
        "organization", {"display_name": "Vendor"}, source="test", request_id="vendor"
    )["entity_id"]
    proof = company.register_evidence(b"cost", "text/plain", "proof", request_id="proof")["digest"]
    for index, period in enumerate(("2026-03", "2026-02", "2026-01")):
        company.save_fact(
            "expense",
            f"cost-{index}",
            {
                "period": period,
                "counterparty_id": vendor,
                "amount_fen": 100 + index,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            evidence=(proof,),
            expected_revision=0,
            request_id=f"cost-{index}",
        )
    query = Discovery(company)
    first = query.find_facts(kind="expense", limit=1)
    assert [item["period"] for item in first["items"]] == ["2026-03"]
    second = query.find_facts(kind="expense", limit=1, cursor=first["next_cursor"])
    assert [item["period"] for item in second["items"]] == ["2026-02"]
    with pytest.raises(KernelError) as changed_filter:
        query.find_facts(status="history", limit=1, cursor=first["next_cursor"])
    assert changed_filter.value.code == "fact_cursor_stale"
    company.save_fact(
        "expense",
        "later",
        {
            "period": "2026-04",
            "counterparty_id": vendor,
            "amount_fen": 999,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="later",
    )
    with pytest.raises(KernelError) as changed_version:
        query.find_facts(kind="expense", limit=1, cursor=first["next_cursor"])
    assert changed_version.value.code == "fact_cursor_stale"


def test_find_facts_batches_raw_hydration(company, monkeypatch):
    vendor = Entities(company).register_entity(
        "organization", {"display_name": "Vendor"}, source="test", request_id="vendor"
    )["entity_id"]
    proof = company.register_evidence(b"cost", "text/plain", "proof", request_id="proof")["digest"]
    for index in range(5):
        company.save_fact(
            "expense",
            f"cost-{index}",
            {
                "period": "2026-01",
                "counterparty_id": vendor,
                "amount_fen": 100 + index,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            evidence=(proof,),
            expected_revision=0,
            request_id=f"cost-{index}",
        )
    calls = []
    original = company.store.fact_data_many

    def counted(connection, identifiers):
        calls.append(tuple(identifiers))
        return original(connection, identifiers)

    monkeypatch.setattr(company.store, "fact_data_many", counted)
    monkeypatch.setattr(
        company.store,
        "fact",
        lambda *_: (_ for _ in ()).throw(AssertionError("scalar fact load")),
    )
    result = Discovery(company).find_facts(kind="expense", limit=5)
    assert len(result["items"]) == 5
    assert len(calls) == 1 and len(calls[0]) == 5
    seen, cursor = [], None
    while True:
        page = Discovery(company).find_facts(kind="expense", limit=2, cursor=cursor)
        seen.extend(item["fact_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 5


def test_discovery_index_rebuild_changes_only_a_damaged_projection(company):
    vendor = Entities(company).register_entity(
        "organization", {"display_name": "Vendor"}, source="test", request_id="vendor"
    )["entity_id"]
    proof = company.register_evidence(b"cost", "text/plain", "proof", request_id="proof")["digest"]
    company.save_fact(
        "expense",
        "cost",
        {
            "period": "2026-01",
            "counterparty_id": vendor,
            "amount_fen": 100,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="cost",
    )
    with company.store.connection() as connection:
        connection.execute("BEGIN")
        assert rebuild_discovery_indexes(connection) is False
        connection.execute("DELETE FROM discovery_fact_current")
        assert rebuild_discovery_indexes(connection) is True
        assert rebuild_discovery_indexes(connection) is False


def test_find_facts_rejects_a_damaged_hit_without_scanning_unrelated_rows(company):
    vendor = Entities(company).register_entity(
        "organization", {"display_name": "Vendor"}, source="test", request_id="vendor"
    )["entity_id"]
    proof = company.register_evidence(b"cost", "text/plain", "proof", request_id="proof")["digest"]
    company.save_fact(
        "expense",
        "cost",
        {
            "period": "2026-01",
            "counterparty_id": vendor,
            "amount_fen": 100,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="cost",
    )
    with company.store.connection() as connection:
        connection.execute("UPDATE discovery_fact_current SET period=24240 WHERE subject_id='cost'")
    with pytest.raises(KernelError) as error:
        Discovery(company).find_facts(kind="expense")
    assert error.value.code == "content_integrity_failed"
    assert error.value.details["reason"] == "discovery_source_mismatch"


def test_context_cannot_cross_company_binding(company, tmp_path):
    different = Engine(
        Store.create(
            tmp_path / "other.sqlite",
            production_bundle(),
            "other",
            "911100000000000002",
            "other-db",
        )
    )
    Discovery(company).update_company_note(
        "第一家公司私有背景", expected_revision=0, request_id="note"
    )
    assert Discovery(different).company_context()["company_note"]["revision"] == 0
    assert Discovery(different).find_facts()["company_id"] == "other"
