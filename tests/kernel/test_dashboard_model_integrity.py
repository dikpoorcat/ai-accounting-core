"""Certified summaries, exact hit proofs and independent repair have distinct duties."""

import copy
from contextlib import contextmanager
from dataclasses import replace

import pytest
from test_banking import book as book
from test_banking import funding, opening
from test_dashboard_bank_source_proof import closed_banks
from test_development_upgrade import create_old_company, old_business
from test_integrity_content import damage, verify
from test_v1_stored_json_isolation import released_book as released_book

from ai_accounting.kernel.backup import BackupError, verify_file
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_runtime import _producing
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.offline_development_upgrade import READ_MODEL_SOURCE_FINGERPRINT
from ai_accounting.kernel.read_state import repair_revision
from ai_accounting.kernel.stored_json import loads_unique

MONTH = "2026-09"


@contextmanager
def raw_read():
    token = _producing.set(True)
    try:
        yield
    finally:
        _producing.reset(token)


def comparable(value):
    value = copy.deepcopy(value)
    if value.get("scope") == "overview" and value.get("data"):
        value["data"].pop("generated_at")
    return value


def database_state(engine):
    with engine.store.connection(read_only=True) as connection:
        return tuple(connection.iterdump())


def revision(engine):
    with engine.store.connection(read_only=True) as connection:
        return repair_revision(connection)


def current_content(engine):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT c.id,c.descriptor FROM dashboard_read_model_current p "
            "JOIN dashboard_read_model_commit c ON c.id=p.commit_id WHERE p.period=?",
            (MONTH,),
        ).fetchone()
        descriptor = loads_unique(row["descriptor"])
        root_digest = bytes.fromhex(descriptor["root_digest"])
        root = loads_unique(
            connection.execute(
                "SELECT payload FROM dashboard_read_model_block WHERE digest=?",
                (root_digest,),
            ).fetchone()[0]
        )
        return (
            bytes(row["id"]),
            root_digest,
            {block["key"]: bytes.fromhex(block["digest"]) for block in root["blocks"]},
        )


def retained_records(engine):
    with engine.store.connection(read_only=True) as connection:
        return {
            table: {row[0]: tuple(row) for row in connection.execute(f"SELECT * FROM {table}")}
            for table in ("audit", "request", "dashboard_read_model_commit", "period_close")
        }


def assert_original_records_unchanged(before, engine):
    after = retained_records(engine)
    for table, rows in before.items():
        assert all(after[table].get(ident) == row for ident, row in rows.items())


def rejected(operation):
    with pytest.raises(KernelError) as failure:
        operation()
    assert failure.value.code == "content_integrity_failed"


@pytest.fixture
def model_book(book):
    engine, save, publish, _ = book
    opening(save, publish, month="2026-08")
    funding(save, publish, subject="history-funding", amount=9, day="2026-08-01")
    funding(save, publish, subject="shown-funding", amount=17)
    Maintenance(engine).rebuild_dashboard_read_model(MONTH, request_id="bootstrap-model")
    assert verify(engine)["status"] == "verified"
    return book


@pytest.mark.parametrize("kind", ("fact", "outcome"))
def test_unshown_history_damage_is_left_to_complete_source_verification(model_book, kind):
    engine = model_book[0]
    dashboard = Dashboard(engine)
    before = dashboard.brief(MONTH)
    voucher_page = dashboard.brief(MONTH, section="vouchers")
    vouchers = voucher_page["data"]["collections"]["vouchers"]["items"]
    assert {item["subject_id"] for item in vouchers} == {"shown-funding"}
    if kind == "fact":
        damage(
            engine,
            "fact_funding",
            "UPDATE fact_funding SET amount_fen=10 WHERE revision_id=("
            "SELECT fact_id FROM fact_current WHERE subject_id='history-funding')",
        )
    else:
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount',10) "
            "WHERE subject_id='history-funding'",
        )
    assert comparable(dashboard.brief(MONTH)) == comparable(before)
    rejected(lambda: verify(engine))
    rejected(
        lambda: Maintenance(engine).rebuild_dashboard_read_model(
            MONTH, request_id="cannot-hide-history"
        )
    )


@pytest.mark.parametrize("kind", ("fact", "outcome", "voucher"))
def test_exact_returned_source_damage_refuses_the_returned_page(model_book, kind):
    engine = model_book[0]
    if kind == "fact":
        damage(
            engine,
            "fact_funding",
            "UPDATE fact_funding SET amount_fen=18 WHERE revision_id=("
            "SELECT fact_id FROM fact_current WHERE subject_id='shown-funding')",
        )
    elif kind == "outcome":
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount',18) "
            "WHERE subject_id='shown-funding'",
        )
    else:
        damage(
            engine,
            "voucher_line",
            "UPDATE voucher_line SET credit=credit+1 WHERE credit>0 AND version_id IN("
            "SELECT id FROM voucher_version WHERE calculation_id IN("
            "SELECT id FROM calculation WHERE subject_id='shown-funding'))",
        )
    rejected(lambda: Dashboard(engine).brief(MONTH, section="vouchers", limit=1))
    rejected(lambda: verify(engine))


@pytest.mark.parametrize("target", ("summary", "page_block", "root", "locator"))
def test_damaged_derived_content_is_refused_then_independently_repaired(model_book, target):
    engine = model_book[0]
    dashboard = Dashboard(engine)
    expected = dashboard.brief(MONTH)
    _, root_digest, blocks = current_content(engine)
    immutable = retained_records(engine)
    initial_revision = revision(engine)
    if target == "locator":
        damage(
            engine,
            "dashboard_read_model_current",
            "DELETE FROM dashboard_read_model_current WHERE period=?",
            (MONTH,),
        )
    else:
        target_digest = (
            root_digest
            if target == "root"
            else blocks[
                "brief/default/overview/0" if target == "summary" else "brief/default/vouchers/0"
            ]
        )
        damage(
            engine,
            "dashboard_read_model_block",
            "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?",
            (target_digest,),
        )
    operation = (
        (lambda: dashboard.brief(MONTH, section="vouchers", limit=1))
        if target == "page_block"
        else (lambda: dashboard.brief(MONTH))
    )
    rejected(operation)
    rejected(lambda: verify(engine))
    result = Maintenance(engine).rebuild_dashboard_read_model(MONTH, request_id="repair-" + target)
    assert result["changed"] is True
    assert result["read_repair_revision"] == initial_revision + 1
    repaired = dashboard.brief(MONTH)
    with raw_read():
        authority = dashboard.brief(MONTH)
    assert comparable(repaired) == comparable(authority)
    # Only refreshed snapshot metadata changes; the financial business content
    # is restored to the complete independently verified original result.
    assert repaired["data"]["financial_position"] == expected["data"]["financial_position"]
    assert_original_records_unchanged(immutable, engine)
    assert verify(engine)["status"] == "verified"
    unchanged = Maintenance(engine).rebuild_dashboard_read_model(
        MONTH, request_id="unchanged-" + target
    )
    assert unchanged["changed"] is False
    assert unchanged["read_repair_revision"] == initial_revision + 1


@pytest.mark.parametrize("stage", ("dashboard_materialized", "dashboard_persisted", "commit"))
def test_failed_derived_repair_preserves_corruption_revision_and_every_receipt(model_book, stage):
    engine = model_book[0]
    _, _, blocks = current_content(engine)
    damage(
        engine,
        "dashboard_read_model_block",
        "DELETE FROM dashboard_read_model_block WHERE digest=?",
        (blocks["brief/default/overview/0"],),
    )
    before = database_state(engine)
    initial_revision = revision(engine)

    def interrupt(point, _connection):
        if point == stage:
            raise RuntimeError("synthetic model repair interruption")

    failed = Engine(engine.store, fault=interrupt)
    with pytest.raises(RuntimeError, match="synthetic model repair interruption"):
        Maintenance(failed).rebuild_dashboard_read_model(MONTH, request_id="retry-repair")
    assert database_state(engine) == before
    assert revision(engine) == initial_revision
    result = Maintenance(engine).rebuild_dashboard_read_model(MONTH, request_id="retry-repair")
    assert result["changed"] is True
    assert verify(engine)["status"] == "verified"


@pytest.mark.parametrize("target", ("receipt", "audit"))
def test_repair_refuses_to_rewrite_original_production_authority(model_book, target):
    engine = model_book[0]
    if target == "receipt":
        damage(
            engine,
            "dashboard_read_model_commit",
            "UPDATE dashboard_read_model_commit SET descriptor="
            "json_set(descriptor,'$.scope.rule_build','invented')",
        )
    else:
        damage(
            engine,
            "audit",
            "UPDATE audit SET payload="
            "json_set(payload,'$.dashboard_models[0].scope.rule_build','invented') "
            "WHERE action='dashboard_read_model'",
        )
    before = database_state(engine)
    rejected(lambda: Dashboard(engine).brief(MONTH))
    rejected(lambda: verify(engine))
    rejected(
        lambda: Maintenance(engine).rebuild_dashboard_read_model(
            MONTH, request_id="forbidden-authority-repair"
        )
    )
    assert database_state(engine) == before


def test_audit_coverage_finds_removed_receipt_even_if_its_locator_was_also_removed(model_book):
    engine = model_book[0]
    damage(engine, "dashboard_read_model_current", "DELETE FROM dashboard_read_model_current")
    damage(engine, "dashboard_read_model_commit", "DELETE FROM dashboard_read_model_commit")
    rejected(lambda: verify(engine))
    before = database_state(engine)
    rejected(
        lambda: Maintenance(engine).rebuild_dashboard_read_model(
            MONTH, request_id="cannot-forge-missing-receipt"
        )
    )
    assert database_state(engine) == before


def test_authenticated_but_wrong_summary_is_compared_with_independent_domain_algorithms(
    book, monkeypatch
):
    import ai_accounting.kernel.dashboard_model_materializer as materializer

    engine, save, publish, _ = book
    funding(save, publish, subject="shown-funding", amount=17)
    expected = Dashboard(engine).brief(MONTH)["data"]["voucher_count"]
    build = materializer.build_model

    def faulty_producer(*args, **kwargs):
        model = build(*args, **kwargs)
        blocks = []
        for block in model.blocks:
            if block.key == "brief/default/overview/0":
                payload = copy.deepcopy(block.payload)
                payload["data"]["voucher_count"] += 1
                block = replace(block, payload=payload)
            blocks.append(block)
        return replace(model, blocks=tuple(blocks))

    with monkeypatch.context() as producer:
        producer.setattr(materializer, "build_model", faulty_producer)
        # A display profile changes native UI content and must rebuild it.
        # Unattached evidence registration legitimately reuses those bytes.
        Entities(engine).update_entity_profile(
            "bank-a", {"display_name": "synthetic faulty producer update"},
            source="synthetic owner update", expected_revision=1,
            request_id="faulty-production",
        )
    assert Dashboard(engine).brief(MONTH)["data"]["voucher_count"] == expected + 1
    rejected(lambda: verify(engine))
    before = database_state(engine)
    rejected(
        lambda: Maintenance(engine).rebuild_dashboard_read_model(
            MONTH, request_id="cannot-recertify-false-summary"
        )
    )
    assert database_state(engine) == before


def test_obsolete_ui_bytes_are_reclaimed_without_rewriting_production_receipts(model_book):
    engine = model_book[0]
    old_commit, _, _ = current_content(engine)
    # This legitimate material write produces a new scope. The immutable receipt
    # remains, while unreferenced UI bytes no longer own a repair obligation.
    engine.register_evidence(
        b"synthetic later source", "text/plain", "later", request_id="later-material"
    )
    new_commit, _, _ = current_content(engine)
    assert new_commit != old_commit
    # The old root is always unique to its scope, even where business block
    # content happens to be shared by both production receipts.
    with engine.store.connection(read_only=True) as connection:
        descriptor = loads_unique(
            connection.execute(
                "SELECT descriptor FROM dashboard_read_model_commit WHERE id=?",
                (old_commit,),
            ).fetchone()[0]
        )
    old_root = bytes.fromhex(descriptor["root_digest"])
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT 1 FROM dashboard_read_model_block WHERE digest=?",
                                  (old_root,)).fetchone() is None
    assert Dashboard(engine).brief(MONTH)["data"] is not None
    assert verify(engine)["status"] == "verified"
    result = Maintenance(engine).rebuild_dashboard_read_model(
        MONTH, request_id="no-old-ui-reconstruction",
    )
    assert result["changed"] is False
    with engine.store.connection(read_only=True) as connection:
        assert loads_unique(connection.execute(
            "SELECT descriptor FROM dashboard_read_model_commit WHERE id=?", (old_commit,),
        ).fetchone()[0]) == descriptor


def test_frozen_accounting_content_is_never_a_derived_model_repair_target(book):
    engine, _, _ = closed_banks(book)
    Maintenance(engine).rebuild_dashboard_read_model(MONTH, request_id="bootstrap-frozen-model")
    damage(
        engine,
        "period_close",
        "UPDATE period_close SET manifest=json_set(manifest,'$.period','2026-08')",
    )
    before = database_state(engine)
    rejected(
        lambda: Maintenance(engine).rebuild_dashboard_read_model(
            MONTH, request_id="cannot-rewrite-frozen-adoption"
        )
    )
    assert database_state(engine) == before


def test_declared_old_draft_without_model_tables_keeps_the_complete_source_path(
    tmp_path, monkeypatch
):
    import ai_accounting.kernel.dashboard_model_materializer as materializer

    path, source, _ = create_old_company(
        tmp_path / "old.sqlite", source_fingerprint=READ_MODEL_SOURCE_FINGERPRINT
    )
    engine, _, _, _ = old_business(path, source)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("old exact draft attempted current dashboard materialization")

    monkeypatch.setattr(materializer, "build_model", forbidden)
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='dashboard_read_model_commit'"
            ).fetchone()
            is None
        )
    assert Dashboard(engine).brief("2026-02")["data"] is not None
    assert verify_file(path, _bundle=source)["verification"]["status"] == "verified"
    with engine.store.connection(read_only=True) as connection:
        fact = connection.execute("SELECT revision_id FROM fact_funding LIMIT 1").fetchone()[0]
    damage(
        engine,
        "fact_funding",
        "UPDATE fact_funding SET amount_fen=amount_fen+1 WHERE revision_id=?",
        (fact,),
    )
    with pytest.raises(BackupError) as failure:
        verify_file(path, _bundle=source)
    assert failure.value.code == "backup_content_invalid"


def test_fixed_v1_fixture_keeps_its_registered_source_verifier(released_book, monkeypatch):
    from test_v1_stored_json_isolation import _block_current_decoders

    import ai_accounting.kernel.dashboard_model_materializer as materializer

    legacy, bundle = released_book
    # Preserve the registered content-v1 fixture unchanged. It verifies frozen
    # source interpretation; the exact old draft above proves table absence.

    def forbidden(*_args, **_kwargs):
        raise AssertionError("fixed v1 called current dashboard materializer")

    monkeypatch.setattr(materializer, "build_model", forbidden)
    with monkeypatch.context() as future:
        _block_current_decoders(future)
        result = verify_file(legacy.engine.store.path, _bundle=bundle)
    assert result["database_format"]["version"] == 1
    assert result["verification"]["status"] == "verified"
    assert result["verification"]["counts"]["facts"] > 0
