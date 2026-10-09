"""Positive role and opening-account index hits must prove their typed source."""

import json

import pytest
from test_identity_corrections import confirm, opening_package
from test_opening_continuation import _close_without_current_business

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities, employee_entities
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


@pytest.fixture
def book(tmp_path):
    engine = Engine(Store.create(
        tmp_path / "positive-witness.sqlite", production_bundle(), "company",
        "911100000000000001", "database",
    ))
    return engine, Entities(engine)


def test_forged_employee_role_rejected_by_page_and_close_snapshot_then_repaired(book):
    engine, directory = book
    person = directory.register_entity(
        "person", {"display_name": "Source Person"}, source="synthetic", request_id="person"
    )["entity_id"]
    proof = engine.register_evidence(
        b"synthetic expense", "text/plain", "invoice", request_id="proof"
    )["digest"]
    saved = engine.save_fact(
        "expense", "expense",
        {"period": "2026-01", "counterparty_id": person, "amount_fen": 100,
         "expense_class": "administration", "creditor_kind": "employee"},
        evidence=(proof,), expected_revision=0, request_id="expense",
    )
    dashboard = Dashboard(engine)

    def people():
        return dashboard.employees(
            "2026-01", preparation="deferred"
        , employee_filter="all")["data"]["collections"]["employees"]["items"]

    assert people() == []
    with engine.store.connection() as connection:
        connection.execute(
            "UPDATE entity_reference_current SET role='employee' "
            "WHERE fact_id=? AND path='counterparty_id'", (saved["fact_id"],)
        )
    with pytest.raises(KernelError) as page_error:
        people()
    assert page_error.value.code == "entity_reference_corrupt"
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as close_error:
            Display.snapshot(connection, "2026-01", registry=engine.store.registry)
    assert close_error.value.code == "entity_reference_corrupt"
    with pytest.raises(KernelError) as integrity_error:
        Maintenance(engine).verify_integrity()
    assert integrity_error.value.code == "entity_reference_corrupt"
    repaired = Maintenance(engine).repair_read_indexes(request_id="repair-role")
    assert repaired["changed"] is True
    assert people() == []


def test_profile_only_employee_candidate_is_authenticated_before_membership(book):
    engine, directory = book
    person = directory.register_entity(
        "person", {"display_name": "Profile Person", "employment_status": "active"},
        source="synthetic employment", request_id="profile-person",
    )["entity_id"]
    with engine.store.connection(read_only=True) as connection:
        assert employee_entities(connection, "2026-01", registry=engine.store.registry) == [person]
    with engine.store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM entity_profile_revision WHERE entity_id=?", (person,)
        ).fetchone()
        content = json.loads(row["content"])
        content["display_name"] = "Forged Person"
        connection.execute("DROP TRIGGER immutable_entity_profile_revision_UPDATE")
        connection.execute(
            "UPDATE entity_profile_revision SET content=? WHERE id=?",
            (json.dumps(content), row["id"]),
        )
        with pytest.raises(KernelError) as error:
            employee_entities(connection, "2026-01", registry=engine.store.registry)
    assert error.value.code == "entity_profile_corrupt"


def test_forged_retired_opening_account_hit_rejected_by_funds_and_repair(book):
    engine, directory = book
    proof = engine.register_evidence(
        b"synthetic opening confirmation", "text/plain", "confirmation", request_id="proof"
    )["digest"]
    owner = directory.register_entity(
        "person", {}, source="synthetic", request_id="owner"
    )["entity_id"]
    old, new = [
        directory.register_entity(
            "fund_account", {}, account_type="cash", source="synthetic", request_id=request
        )["entity_id"]
        for request in ("old", "new")
    ]
    opening_package(engine, proof, [
        ("opening_cash", "cash", {"cash_account_id": old, "balance_fen": 10000}),
        ("opening_equity", "equity", {
            "equity_kind": "paid_in_capital", "balance_fen": 10000,
            "holder_or_basis_id": owner,
        }),
    ])
    _close_without_current_business(engine, "2026-01", proof)
    confirm(engine, dict(
        changes=[{"subject_id": "cash", "expected_revision": 1, "action": "reassign",
                  "data": {"period": "2026-01", "package_id": "opening",
                           "cash_account_id": new, "balance_fen": 10000}}],
        evidence=[proof], reason="confirmed corrected account", posting_period="2026-03",
    ))
    for category in MATERIAL_CATEGORIES:
        Periods(engine).inventory(
            "2026-03", category, evidence=[], expected=0, no_business=True,
            confirmation_evidence=proof, request_id=f"march-{category}",
        )
    dashboard = Dashboard(engine)

    def page():
        return dashboard.funds("2026-03", preparation="deferred")["data"]

    baseline = page()
    assert baseline["account_count"] == 1
    assert baseline["total_fen"] == 10000
    with engine.store.connection() as connection:
        row = connection.execute(
            "SELECT r.fact_id,r.path FROM fact_current c "
            "JOIN fact_revision f ON f.id=c.fact_id "
            "JOIN subject s ON s.id=f.subject_id "
            "JOIN entity_reference_current r ON r.fact_id=f.id "
            "WHERE s.kind='opening_identity_binding' AND r.entity_id=?", (new,),
        ).fetchone()
        assert row is not None
        connection.execute(
            "UPDATE entity_reference_current SET entity_id=? WHERE fact_id=? AND path=?",
            (old, row["fact_id"], row["path"]),
        )
    with pytest.raises(KernelError) as page_error:
        page()
    assert page_error.value.code == "entity_reference_corrupt"
    with pytest.raises(KernelError) as integrity_error:
        Maintenance(engine).verify_integrity()
    assert integrity_error.value.code == "entity_reference_corrupt"
    repaired = Maintenance(engine).repair_read_indexes(request_id="repair-funds")
    assert repaired["changed"] is True
    restored = page()
    assert restored["account_count"] == 1
    assert restored["total_fen"] == 10000
