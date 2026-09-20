"""Real public registration paths: no auto-registration fixture or free object IDs."""

import sqlite3

import pytest

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities, verify_entities
from ai_accounting.kernel.entity_references import verify_entity_references
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


@pytest.fixture
def engine(tmp_path):
    return Engine(
        Store.create(
            tmp_path / "company.sqlite",
            production_bundle(),
            "company",
            "911100000000000001",
            "database",
        )
    )


def test_same_name_multi_role_unnamed_inactive_and_isolation(engine, tmp_path):
    entities = Entities(engine)
    first = entities.register_entity(
        "person", {"display_name": "张三"}, source="合同", request_id="first"
    )
    second = entities.register_entity(
        "person", {"display_name": "张三", "active": False}, source="合同", request_id="second"
    )
    unnamed = entities.register_entity(
        "organization", {}, source="原件只给了编号", request_id="unnamed"
    )
    assert first["entity_id"] != second["entity_id"]
    assert len(entities.find_entities(query="张三")["items"]) == 2
    assert len(entities.find_entities()["items"]) == 3
    assert unnamed["entity_id"].startswith("entity_")
    assert first == entities.register_entity(
        "person", {"display_name": "张三"}, source="合同", request_id="first"
    )
    proof = engine.register_evidence(b"invoice", "text/plain", "invoice", request_id="proof")[
        "digest"
    ]
    engine.save_fact(
        "expense",
        "expense-1",
        {
            "period": "2026-01",
            "counterparty_id": first["entity_id"],
            "amount_fen": 100,
            "expense_class": "administration",
            "creditor_kind": "employee",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="expense",
    )
    other = Engine(
        Store.create(
            tmp_path / "other.sqlite", production_bundle(), "other", "911100000000000002", "db2"
        )
    )
    assert Entities(other).find_entities()["items"] == []
    with other.store.connection(read_only=True) as connection, pytest.raises(NeedsInformation):
        from ai_accounting.kernel.entities import require_entity

        require_entity(connection, first["entity_id"])
    with engine.store.connection(read_only=True) as connection:
        verify_entities(connection)
        verify_entity_references(connection)


def test_unknown_object_rejected_and_profile_changes_only_management(engine):
    proof = engine.register_evidence(b"invoice", "text/plain", "invoice", request_id="proof")[
        "digest"
    ]
    fact = {
        "period": "2026-01",
        "counterparty_id": "not-registered",
        "amount_fen": 100,
        "expense_class": "administration",
        "creditor_kind": "supplier",
    }
    with pytest.raises(NeedsInformation):
        engine.save_fact(
            "expense",
            "expense-1",
            fact,
            evidence=(proof,),
            expected_revision=0,
            request_id="expense",
        )
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == 0
    entity = Entities(engine).register_entity("person", {}, source="明确资料", request_id="entity")
    with engine.store.connection(read_only=True) as connection:
        before = engine.store.epochs(connection)
    result = Entities(engine).update_entity_profile(
        entity["entity_id"],
        {"display_name": "李四"},
        source="本人确认",
        expected_revision=1,
        request_id="profile",
    )
    assert result["revision"] == 2
    with engine.store.connection() as connection:
        after = engine.store.epochs(connection)
        assert before["accounting"] == after["accounting"]
        assert before["material"] == after["material"]
        assert before["management"] + 1 == after["management"]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM entity_profile_revision")
    with pytest.raises(KernelError, match="档案已变化"):
        Entities(engine).update_entity_profile(
            entity["entity_id"], {}, source="确认", expected_revision=1, request_id="stale"
        )


def test_account_kind_is_not_payee_or_another_fund_type(engine):
    entity = Entities(engine).register_entity(
        "fund_account", {}, account_type="cash", source="现金账户", request_id="account"
    )
    from ai_accounting.kernel.entities import require_entity

    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as error:
            require_entity(
                connection, entity["entity_id"], kinds=("fund_account",), account_type="bank"
            )
        assert error.value.code == "entity_kind_mismatch"
        with pytest.raises(KernelError):
            require_entity(connection, entity["entity_id"], kinds=("person", "organization"))


@pytest.mark.parametrize("nested", [False, True])
def test_ordinary_amend_cannot_bypass_atomic_identity_correction(engine, nested):
    entities = Entities(engine)
    first = entities.register_entity("person", {}, source="原件", request_id="first")["entity_id"]
    second = entities.register_entity("person", {}, source="原件", request_id="second")["entity_id"]
    proof = engine.register_evidence(b"confirmed cost", "text/plain", "basis", request_id="proof")[
        "digest"
    ]
    kind = "expense"
    data = dict(
        period="2026-01",
        counterparty_id=first,
        amount_fen=100,
        expense_class="administration",
        creditor_kind="employee",
    )
    if nested:
        account = entities.register_entity(
            "fund_account", {}, account_type="bank", source="账户原件", request_id="bank"
        )["entity_id"]
        kind = "payment"
        data = dict(
            period="2026-01",
            actual_date="2026-01-15",
            direction="outflow",
            bank_account_id=account,
            counterparty_id=first,
            amount_fen=100,
            allocations=[
                dict(
                    source_kind="expense",
                    source_id="cost",
                    obligation="primary",
                    amount_fen=100,
                    recipient_id=first,
                )
            ],
        )
    engine.save_fact(
        kind, "business", data, evidence=[proof], expected_revision=0, request_id="save"
    )
    if nested:
        data["allocations"][0]["recipient_id"] = second
    else:
        data["counterparty_id"] = second
    with engine.store.connection(read_only=True) as connection:
        before = list(connection.iterdump())
    with pytest.raises(KernelError) as error:
        engine.amend_fact(
            kind,
            "business",
            data,
            evidence=[proof],
            expected_revision=1,
            recording_error_confirmed=True,
            request_id="amend",
        )
    assert error.value.code == "identity_correction_required"
    assert error.value.details["fields"] == [
        "allocations.0.recipient_id" if nested else "counterparty_id"
    ]
    with engine.store.connection(read_only=True) as connection:
        assert list(connection.iterdump()) == before


def test_reference_damage_rejected_and_repair_preserves_authority(engine):
    from ai_accounting.kernel.discovery import Discovery
    from ai_accounting.kernel.maintenance import Maintenance

    person = Entities(engine).register_entity("person", {}, source="本人", request_id="person")[
        "entity_id"
    ]
    proof = engine.register_evidence(b"cost source", "text/plain", "source", request_id="proof")[
        "digest"
    ]
    registered = engine.save_fact(
        "expense",
        "expense",
        {
            "period": "2026-01",
            "counterparty_id": person,
            "amount_fen": 123,
            "expense_class": "administration",
            "creditor_kind": "employee",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="expense",
    )
    check = Maintenance(engine)
    assert check.verify_integrity()["status"] == "verified"
    with engine.store.connection() as connection:
        before = list(connection.execute("SELECT * FROM fact_revision"))
        connection.execute(
            "DELETE FROM entity_reference_current WHERE fact_id=?", (registered["fact_id"],)
        )
        connection.commit()
    # A missing row cannot be disproved by a page that never encounters it.
    assert Discovery(engine).find_facts(entity_id=person)["items"] == []
    with pytest.raises(KernelError):
        check.verify_integrity()
    repaired = check.repair_read_indexes(request_id="repair")
    assert repaired["changed"] is True
    again = check.repair_read_indexes(request_id="repair-again")
    assert again["changed"] is False
    assert again["read_repair_revision"] == repaired["read_repair_revision"]
    with engine.store.connection(read_only=True) as connection:
        assert [tuple(row) for row in before] == [
            tuple(row) for row in connection.execute("SELECT * FROM fact_revision")
        ]
    assert (
        Discovery(engine).find_facts(entity_id=person)["items"][0]["fact_id"]
        == registered["fact_id"]
    )


def test_frozen_employee_membership_does_not_acquire_later_roles(engine):
    from ai_accounting.kernel.dashboard import _Snapshot
    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods

    directory = Entities(engine)
    employee = directory.register_entity(
        "person",
        {"display_name": "原姓名", "employment_status": "active"},
        source="synthetic employment",
        request_id="employee",
    )["entity_id"]
    contractor = directory.register_entity(
        "person", {"display_name": "交易方"}, source="synthetic contract", request_id="contractor"
    )["entity_id"]
    proof = engine.register_evidence(
        b"no January business", "text/plain", "confirmation", request_id="proof"
    )["digest"]
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-01",
            category,
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=proof,
            request_id="inventory-" + category,
        )
    preview = periods.preview_close("2026-01", owner_confirmation=proof)
    periods.close(
        "2026-01",
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="close",
    )
    directory.update_entity_profile(
        employee,
        {"display_name": "现姓名", "employment_status": "active"},
        source="synthetic rename",
        expected_revision=1,
        request_id="rename",
    )
    directory.update_entity_profile(
        contractor,
        {"display_name": "交易方", "employment_status": "active"},
        source="synthetic new employment",
        expected_revision=1,
        request_id="new-employment",
    )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        closed = _Snapshot(engine, connection, "2026-01")
        assert set(closed.profiles["employee"]) == {employee}
        assert closed.profiles["employee"][employee]["display_name"] == "原姓名"
        current = _Snapshot(engine, connection, "2026-02")
        assert set(current.profiles["employee"]) == {employee, contractor}
