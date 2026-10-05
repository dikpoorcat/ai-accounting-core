"""Resident owner briefs preserve coherent snapshots and isolate each company."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from material_fixture import supporting_text
from pydantic import SecretStr

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.service import LocalService

PERIOD = "2026-09"


def _without_generated(value):
    if isinstance(value, dict):
        return {
            key: _without_generated(item) for key, item in value.items() if key != "generated_at"
        }
    if isinstance(value, list):
        return [_without_generated(item) for item in value]
    return value



def _fund(engine, label, amount, *, period=PERIOD):
    entities = Entities(engine)
    owner = entities.register_entity(
        "person",
        {"display_name": label},
        source="synthetic explicit owner",
        request_id=label + "-owner",
    )["entity_id"]
    bank = entities.register_entity(
        "fund_account",
        {},
        account_type="bank",
        source="synthetic bank account",
        request_id=label + "-bank",
    )["entity_id"]
    proof = engine.register_evidence(
        ("Synthetic funding confirmation " + label).encode(),
        "text/plain",
        label,
        request_id=label + "-evidence",
    )["digest"]
    supporting_text(engine, proof, period=period)
    engine.save_fact(
        "funding",
        label,
        {
            "period": period,
            "owner_id": owner,
            "bank_account_id": bank,
            "amount_fen": amount,
            "funding_kind": "capital",
            "actual_date": period + "-02",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id=label + "-save",
    )
    preview = engine.preview([label])
    engine.confirm(
        [label],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=label + "-publish",
    )
    return bank



@pytest.fixture
def resident_service(tmp_path):
    root = tmp_path / "synthetic-owner-brief"
    service = LocalService(root, enable_read_pool=True)
    password = SecretStr("Synthetic-parallel-owner-123")
    service.security.provision("owner", password)
    token = service.security.login("owner", password).session_token
    first = service.catalog.create_company("91310000123456789A", "合成甲")
    second = service.catalog.create_company("91310000123456789B", "合成乙")
    _fund(service.engine(first["id"]), "first-funding", 123456)
    _fund(service.engine(second["id"]), "second-funding", 987654)
    try:
        yield service, token, first, second
    finally:
        service.close()



def _brief(service, token, company, **options):
    return service.dispatch(
        "dashboard_brief",
        {"company_id": company["id"], "period": PERIOD, **options},
        session_token=token,
    )



def test_read_failure_discards_connection_and_recovers(resident_service, monkeypatch):
    from ai_accounting.kernel import dashboard

    service, token, first, _ = resident_service
    original = dashboard._open_items
    failed = []

    def fail_once(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise KernelError("synthetic_open_items_failure", "synthetic open item failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(dashboard, "_open_items", fail_once)
    with pytest.raises(KernelError) as rejected:
        _brief(service, token, first)
    assert rejected.value.code == "synthetic_open_items_failure"
    assert service.read_pool._total == 0
    assert _brief(service, token, first)["data"]["funds_overview"]["bank_fen"] == 123456



def test_client_cannot_supply_parallel_values(resident_service):
    service, token, first, _ = resident_service
    properties = service.command_models["dashboard_brief"].json_schema()["properties"]
    assert not any(key.startswith("_") for key in properties)
    with pytest.raises(KernelError) as rejected:
        _brief(service, token, first, _parallel_attempt={"already_verified": True})
    assert rejected.value.code == "invalid_command"



def test_resident_brief_matches_snapshot_company_and_http_amounts(resident_service):
    service, token, first, second = resident_service
    for company, amount in ((first, 123456), (second, 987654), (first, 123456)):
        expected = Dashboard(service.engine(company["id"], dashboard_read=True)).brief(PERIOD)
        actual = _brief(service, token, company, preparation="complete")
        assert _without_generated(actual) == _without_generated(expected)
        assert actual["read_context"]["company_id"] == company["id"]
        assert actual["selected_period"]["key"] == PERIOD
        assert actual["data"]["funds_overview"]["bank_fen"] == amount
        assert actual["data"]["activity_count"] == 1
    http = service.dispatch(
        "dashboard_brief", {"company_id": first["id"], "period": PERIOD},
        session_token=token, response_format="http_json",
    )
    assert json.loads(http)["data"]["funds_overview"]["bank_fen"] == "123456"
    assert service.read_pool._total == 2


def test_month_scope_and_empty_company_are_independent(resident_service):
    service, token, first, _ = resident_service
    _fund(service.engine(first["id"]), "october-funding", 333, period="2026-10")
    september = _brief(service, token, first)
    october = _brief(service, token, first, period="2026-10")
    assert september["selected_period"]["key"] == PERIOD
    assert october["selected_period"]["key"] == "2026-10"
    assert september["data"]["funds_overview"]["bank_fen"] == 123456
    assert october["data"]["funds_overview"]["bank_fen"] == 123789
    assert september["data"]["funds_overview"]["inflow_fen"] == 123456
    assert october["data"]["funds_overview"]["inflow_fen"] == 333
    assert september["data"]["activity_count"] == october["data"]["activity_count"] == 1
    empty = service.catalog.create_company("91310000123456789C", "合成空库")
    result = service.dispatch("dashboard_brief", {"company_id": empty["id"]}, session_token=token)
    assert result["data"] is result["selected_period"] is None
    assert result["read_context"]["company_id"] == empty["id"]


def test_concurrent_commit_keeps_snapshot_and_next_read_sees_commit(resident_service, monkeypatch):
    from ai_accounting.kernel import dashboard

    service, token, first, _ = resident_service
    expected = _brief(service, token, first)
    reached, committed = Event(), Event()
    original = dashboard._position

    def pause_after_position(snap):
        result = original(snap)
        reached.set()
        assert committed.wait(10)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(dashboard, "_position", pause_after_position)
        with ThreadPoolExecutor(max_workers=1) as executor:
            reading = executor.submit(_brief, service, token, first)
            try:
                assert reached.wait(10)
                _fund(service.engine(first["id"]), "during-read", 333)
            finally:
                committed.set()
            actual = reading.result(timeout=10)
    assert _without_generated(actual) == _without_generated(expected)
    fresh = _brief(service, token, first)
    assert fresh["data"]["funds_overview"]["bank_fen"] == 123789
    assert fresh["data"]["activity_count"] == 2
    assert fresh["snapshot_version"] != actual["snapshot_version"]


@pytest.mark.parametrize("marker", ["_parallel_checks", "_brief_parallel_checks"])
@pytest.mark.parametrize("command", ["dashboard_brief", "period_readiness"])
def test_client_cannot_supply_internal_checks(resident_service, marker, command):
    service, token, first, _ = resident_service
    with pytest.raises(KernelError) as rejected:
        service.dispatch(command, {
            "company_id": first["id"], "period": PERIOD, marker: {"verified": True},
        }, session_token=token)
    assert rejected.value.code == "invalid_command"


def test_company_identity_error_is_rejected(resident_service):
    service, token, first, _ = resident_service
    with service.catalog.connection() as connection:
        connection.execute("UPDATE company SET taxpayer_id=? WHERE id=?",
                           ("91310000123456789C", first["id"]))
    with pytest.raises(KernelError) as rejected:
        _brief(service, token, first)
    assert rejected.value.code == "company_mismatch"
    assert rejected.value.response()["status"] == "rejected"
    assert "fact_issues" not in rejected.value.response()


def test_owner_brief_rejects_damaged_source(resident_service):
    service, token, first, _ = resident_service
    with service.engine(first["id"]).store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='calculation'"
        ).fetchall()
        for name, _ in triggers:
            connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        connection.execute("UPDATE calculation SET digest=zeroblob(32)")
        for _, sql in triggers:
            connection.execute(sql)
        connection.commit()
    with pytest.raises(KernelError) as rejected:
        _brief(service, token, first)
    assert rejected.value.code == "content_integrity_failed"
    assert service.read_pool._total == 0
