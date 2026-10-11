"""Evidence INSERTs may re-anchor authenticated pages, never stale business inputs."""

import pytest
from test_banking import book as book  # noqa: F401
from test_banking import funding, opening
from test_dashboard_model_transactions import state
from test_dashboard_write_growth_audit import native_pages, owner_pages
from test_integrity_content import damage

from ai_accounting.kernel import business_queries, dashboard_model_materializer
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_runtime import model_scope
from ai_accounting.kernel.dashboard_read_model import (
    load_current_model,
    prepare_evidence_registration_model,
)
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.stored_json import loads_unique

PERIOD = "2026-01"


@pytest.fixture
def model_book(book):
    engine, save, publish, _ = book
    opening(save, publish, month=PERIOD)
    funding(save, publish, amount=17, day=PERIOD + "-01")
    return book


def current(engine):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT c.descriptor,b.payload FROM dashboard_read_model_current x "
            "JOIN dashboard_read_model_commit c ON c.id=x.commit_id "
            "JOIN dashboard_read_model_block b ON b.digest=c.root_digest WHERE x.period=?",
            (PERIOD,),
        ).fetchone()
        return loads_unique(row[0]), loads_unique(row[1])


def register(engine, request="independent-proof"):
    return engine.register_evidence(
        b"synthetic independent evidence", "text/plain", "unattached", request_id=request,
    )


def test_rebound_scope_preserves_all_native_values_and_independent_verifier_rebuilds(
    model_book, monkeypatch,
):
    engine = model_book[0]
    before_descriptor, before_root = current(engine)
    before_native = native_pages(engine, PERIOD)
    before_page = Dashboard(engine).brief(PERIOD)
    build = dashboard_model_materializer.build_model
    calls = []

    def observed(dashboard, period, **kwargs):
        calls.append(period)
        return build(dashboard, period, **kwargs)

    monkeypatch.setattr(dashboard_model_materializer, "build_model", observed)
    register(engine)
    assert calls == []
    after_descriptor, after_root = current(engine)
    assert after_descriptor["parent_commit_id"] == before_descriptor["commit_id"]
    assert after_descriptor["scope"]["epochs"]["material"] == (
        before_descriptor["scope"]["epochs"]["material"] + 1
    )
    assert after_root["blocks"] == before_root["blocks"]
    assert after_descriptor["root_digest"] != before_descriptor["root_digest"]
    after_page = Dashboard(engine).brief(PERIOD)
    assert after_page["read_context"]["read_version"] != before_page["read_context"]["read_version"]
    assert after_page["snapshot_version"] != before_page["snapshot_version"]
    assert native_pages(engine, PERIOD) == before_native
    assert owner_pages(engine, PERIOD) == before_native
    with engine.store.connection(read_only=True) as connection:
        old = connection.execute(
            "SELECT descriptor FROM dashboard_read_model_commit WHERE id=?",
            (bytes.fromhex(before_descriptor["commit_id"]),),
        ).fetchone()
        assert loads_unique(old[0]) == before_descriptor
    before_verify = state(engine)
    result = Maintenance(engine).verify_integrity()
    assert result["status"] == "verified"
    assert calls == [PERIOD]
    assert state(engine) == before_verify


def test_evidence_epoch_still_expires_a_business_preview(model_book):
    engine, save, publish, _ = model_book
    funding(save, publish, subject="pending-funding", amount=5, day=PERIOD + "-02", posted=False)
    preview = engine.preview(["pending-funding"])
    register(engine)
    before = state(engine)
    with pytest.raises(KernelError) as error:
        engine.confirm(
            ["pending-funding"], preview_digest=preview["digest"], epochs=preview["epochs"],
            request_id="expired-preview",
        )
    assert error.value.code == "preview_expired"
    assert state(engine) == before


@pytest.mark.parametrize("changed", [
    "company_id", "database_id", "period", "accounting", "management", "material",
    "read_repair_revision", "rule_build", "as_of",
])
def test_rebinding_requires_exact_other_inputs_and_one_material_increment(model_book, changed):
    engine = model_book[0]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        old = model_scope(engine, connection, PERIOD)
        model = load_current_model(connection, old)
        scope = old.model_copy(update={
            "epochs": old.epochs.model_copy(update={"material": old.epochs.material + 1}),
        })
        if changed in ("accounting", "management", "material"):
            scope = scope.model_copy(update={"epochs": scope.epochs.model_copy(update={
                changed: getattr(scope.epochs, changed) + 1,
            })})
        else:
            alternate = {"company_id": "other-company", "database_id": "other-database",
                         "period": "2026-02", "read_repair_revision": 1,
                         "rule_build": "other-build", "as_of": "2099-01-01"}
            scope = scope.model_copy(update={changed: alternate[changed]})
        assert prepare_evidence_registration_model(model, scope, request_id="internal-test") is None


@pytest.mark.parametrize("changed", ["as_of", "rule_build", "read_repair_revision"])
def test_evidence_registration_rebuilds_when_authentic_inputs_are_stale(
    model_book, monkeypatch, changed,
):
    import ai_accounting.kernel.engine as engine_module

    engine = model_book[0]
    before_descriptor, _ = current(engine)
    if changed == "as_of":
        monkeypatch.setattr(business_queries, "_today_china", lambda: "2099-01-01")
    elif changed == "rule_build":
        monkeypatch.setattr(engine_module, "PROGRAM_VERSION", "synthetic-new-build")
    else:
        with engine.store.connection() as connection:
            connection.execute("UPDATE state SET read_repair_revision=read_repair_revision+1")
            connection.commit()
    build = dashboard_model_materializer.build_model
    calls = []

    def observed(dashboard, period, **kwargs):
        calls.append(period)
        return build(dashboard, period, **kwargs)

    monkeypatch.setattr(dashboard_model_materializer, "build_model", observed)
    register(engine)
    assert calls == [PERIOD]
    after_descriptor, _ = current(engine)
    assert after_descriptor["scope"][changed] != before_descriptor["scope"][changed]
    assert after_descriptor["parent_commit_id"] == before_descriptor["commit_id"]


def test_damaged_current_bytes_cannot_be_hidden_by_evidence_rebinding(model_book, monkeypatch):
    engine = model_book[0]
    _, root = current(engine)
    damage(engine, "dashboard_read_model_block",
           "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?",
           (bytes.fromhex(root["blocks"][0]["digest"]),))
    before = state(engine)

    def unexpected(*args, **kwargs):
        raise AssertionError("damaged current content was silently rebuilt")

    monkeypatch.setattr(dashboard_model_materializer, "build_model", unexpected)
    with pytest.raises(KernelError) as error:
        register(engine)
    assert error.value.code == "content_integrity_failed"
    assert state(engine) == before
