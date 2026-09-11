import sqlite3

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.discovery import Discovery
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store


@pytest.fixture
def company(tmp_path):
    store = Store.create(
        tmp_path / "company.sqlite", default_registry(), "co", "911100000000000001", "db"
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
    profile = {
        "period": "2026-02",
        "employee_id": "employee-1",
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
    current = query.find_facts(person_id="employee-1", period_from="2026-02", period_to="2026-02")
    assert len(current["items"]) == 1
    assert current["items"][0]["revision"] == 2
    assert current["items"][0]["evidence"] == [proof]
    assert query.find_facts(person_id="someone-else")["items"] == []
    assert query.find_facts(period_from="2026-03")["items"] == []
    first = query.find_facts(kind="payroll_profile", status="history", limit=1)
    second = query.find_facts(
        kind="payroll_profile", status="history", limit=1, after_id=first["next_after_id"]
    )
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["fact_id"] != second["items"][0]["fact_id"]
    assert second["next_after_id"] is None


def test_find_sources_distinguishes_saved_published_and_deleted(company):
    proof = company.register_evidence(b"cost", "text/plain", "proof", request_id="proof")["digest"]
    company.save_fact(
        "expense",
        "cost",
        {
            "period": "2026-01",
            "counterparty_id": "vendor",
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
    assert len(query.find_facts(status="published")["items"]) == 1
    preview = company.preview_delete("cost")
    company.delete(
        "cost", preview_digest=preview["digest"], epochs=preview["epochs"], request_id="delete"
    )
    assert query.find_facts()["items"] == []
    assert len(query.find_facts(status="deleted")["items"]) == 1


def test_context_cannot_cross_company_binding(company, tmp_path):
    different = Engine(
        Store.create(
            tmp_path / "other.sqlite", default_registry(), "other", "911100000000000002", "other-db"
        )
    )
    Discovery(company).update_company_note(
        "第一家公司私有背景", expected_revision=0, request_id="note"
    )
    assert Discovery(different).company_context()["company_note"]["revision"] == 0
    assert Discovery(different).find_facts()["company_id"] == "other"
