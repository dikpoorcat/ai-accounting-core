"""Certified summaries, exact hit proofs and independent repair have distinct duties."""

import copy
from dataclasses import replace

import pytest
from archived_draft_fixture import FINGERPRINTS, archived_company
from test_banking import book as current_book_factory
from test_banking import funding, opening
from test_dashboard_bank_source_proof import closed_banks
from test_dashboard_model_transactions import raw_read
from test_integrity_content import damage, verify
from test_v1_stored_json_isolation import released_book as released_book

from ai_accounting.kernel.backup import BackupError, verify_file
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.read_state import repair_revision
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.stored_json import loads_unique

MONTH = "2026-09"


@pytest.fixture
def book(tmp_path, monkeypatch):
    """Construct saved company models on the exact retained 0c contract."""
    import entity_fixture
    import material_fixture
    import test_banking

    from ai_accounting.kernel.entity_profile_history import PreEventProfile
    from ai_accounting.kernel.materials import Materials
    from ai_accounting.kernel.runtime import initialize_file
    from ai_accounting.kernel.versions import install_metadata, verify_schema
    from scripts.stage9_history_adjustment import source_bundle

    bundle = source_bundle()

    class SourceStore:
        @staticmethod
        def create(path, selected, company_id, taxpayer_id, database_id):
            assert selected is bundle

            def initialize(connection):
                connection.execute("BEGIN IMMEDIATE")
                for kind in ("table", "index", "trigger"):
                    for item in bundle.current()["objects"]:
                        if item["type"] == kind:
                            connection.execute(item["sql"])
                connection.execute("INSERT INTO state VALUES(1,0,0,0,1,0)")
                connection.execute("INSERT INTO source_change_head VALUES(1,0)")
                connection.execute("INSERT INTO identity VALUES(1,?,?,?)",
                                   (company_id, taxpayer_id, database_id))
                install_metadata(connection, bundle, "company")
                connection.commit()

            initialize_file(path, initialize, lambda connection: verify_schema(
                connection, bundle=bundle,
            ))
            return Store(path, bundle, company_id, database_id)

    class StoredModelHistoryFixtureEngine(Engine):
        """Test construction bridge for retained 0c, not current Engine behavior.

        Construct stored content with the real build/prepare/persist functions
        in a writer transaction. This is separate from the current async
        producer; no production capability or verifier is mocked.
        """

        def _write(self, *args, **kwargs):
            from contextlib import contextmanager
            from uuid import uuid4

            from ai_accounting.kernel.dashboard import _Snapshot
            from ai_accounting.kernel.dashboard_model_materializer import build_model
            from ai_accounting.kernel.dashboard_model_runtime import (
                INTERNAL_REQUEST_PREFIX,
                model_scope,
                production_reads,
                stable_payload,
            )
            from ai_accounting.kernel.dashboard_read_model import (
                PageBlock,
                authenticate_current_storage,
                collect_obsolete_blocks,
                persist_model,
                prepare_model,
            )
            from ai_accounting.kernel.read_indexes import sync_audit
            from ai_accounting.kernel.types import canonical, digest

            original_fault = self.fault

            def historical_construction(point, connection):
                if point == "commit":
                    periods = {row[0] for row in connection.execute(
                        "SELECT period FROM dashboard_read_model_current",
                    )} | set(kwargs.get("_dashboard_periods", ()))
                    if periods:
                        authenticate_current_storage(connection)
                        prepared = []
                        request_id = INTERNAL_REQUEST_PREFIX + uuid4().hex
                        with production_reads(self, connection) as reads:
                            dashboard = Dashboard(self)

                            @contextmanager
                            def snapshot(period):
                                snap = _Snapshot(self, connection, period, reads)
                                try:
                                    yield snap
                                finally:
                                    snap.release()

                            dashboard._snapshot = snapshot
                            for period in sorted(periods):
                                scope = model_scope(self, connection, period)
                                current = connection.execute(
                                    "SELECT commit_id FROM dashboard_read_model_current "
                                    "WHERE period=?", (period,),
                                ).fetchone()
                                content = build_model(dashboard, period)
                                blocks = [PageBlock(
                                    block.key, block.kind, stable_payload(block.payload),
                                    keys=block.keys, sources=block.sources,
                                    voucher_version_ids=block.voucher_version_ids,
                                    total_count=block.total_count,
                                    filtered_count=block.filtered_count,
                                    item_sources=block.item_sources,
                                    item_voucher_version_ids=block.item_voucher_version_ids,
                                    fact_sources=block.fact_sources,
                                    item_fact_sources=block.item_fact_sources,
                                ) for block in content.blocks]
                                prepared.append(prepare_model(
                                    scope, blocks, request_id=request_id,
                                    parent_commit_id=bytes(current[0]).hex() if current else None,
                                    date_basis=content.date_basis,
                                ))
                        self.fault("dashboard_materialized", connection)
                        result = {"status": "materialized", "dashboard_models": [
                            model.descriptor for model in prepared
                        ]}
                        audit = connection.execute(
                            "INSERT INTO audit(request_id,action,payload) VALUES(?,?,?)",
                            (request_id, "dashboard_read_model", canonical(result)),
                        )
                        connection.execute("INSERT INTO request VALUES(?,?,?)", (
                            request_id, digest(["dashboard_read_model", result]), canonical(result),
                        ))
                        sync_audit(connection, audit.lastrowid)
                        for model in prepared:
                            persist_model(connection, model, audit_id=audit.lastrowid)
                        collect_obsolete_blocks(connection)
                        self.fault("dashboard_persisted", connection)
                original_fault(point, connection)

            self.fault = historical_construction
            try:
                return super()._write(*args, **kwargs)
            finally:
                self.fault = original_fault

    monkeypatch.setattr(test_banking, "production_bundle", lambda: bundle)
    monkeypatch.setattr(test_banking, "Store", SourceStore)
    monkeypatch.setattr(test_banking, "Engine", StoredModelHistoryFixtureEngine)
    # The source used one shared profile grammar. Initial synthetic rows use
    # that exact declared body; current typed writes are not relaxed.
    profiles = entity_fixture.entity_profile_models()
    profiles["person"] = PreEventProfile
    monkeypatch.setattr(entity_fixture, "entity_profile_models",
                        lambda: profiles)

    class SourceMaterialsSeed(Materials):
        def resolve(self, subject_id, data, *, evidence, expected_revision, request_id):
            # Bootstrap the original supporting-only declaration through the
            # exact stored model. Production's historical input gate stays on.
            assert data["treatment"] == "supporting" and not data.get("links")
            kind = "material_resolution_v2"
            constructor_registry = copy.copy(bundle.registry)
            constructor_registry.historical_only = bundle.registry.historical_only - {kind}
            constructor = Engine(Store(
                self.engine.store.path, replace(bundle, registry=constructor_registry),
                self.engine.store.company_id, self.engine.store.database_id,
            ))
            request_hash, operation = constructor._registration(
                False, kind, subject_id, data, evidence=evidence,
                expected_revision=expected_revision,
            )
            return constructor._write(
                request_id, request_hash, None, (bundle.registry.models[kind].lane,),
                "confirm_fact", operation,
            )

    monkeypatch.setattr(material_fixture, "Materials", SourceMaterialsSeed)
    result = current_book_factory.__wrapped__(tmp_path)
    with result[0].store.connection(read_only=True) as connection:
        from ai_accounting.kernel.dashboard_async import supported

        assert not supported(connection)
    return result


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
def async_book(tmp_path):
    """Current 041 uses its real queued sidecar lifecycle, never the old fixture."""
    result = current_book_factory.__wrapped__(tmp_path)
    _, save, publish, _ = result
    opening(save, publish, month="2026-08")
    funding(save, publish, subject="history-funding", amount=9, day="2026-08-01")
    funding(save, publish, subject="shown-funding", amount=17)
    return result


def sidecar_state(engine):
    import sqlite3

    from ai_accounting.kernel.dashboard_async import _store_path

    path = _store_path(engine)
    if not path.exists():
        return None
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        return tuple(connection.iterdump())


def damage_sidecar(engine):
    import sqlite3

    from ai_accounting.kernel.dashboard_async import _store_path

    with sqlite3.connect(_store_path(engine)) as connection:
        connection.execute("UPDATE dashboard_read_model_block SET payload='{}'")


@pytest.mark.parametrize("kind", ("fact", "outcome"))
@pytest.mark.parametrize("state", ("preparing", "ready", "corrupt"))
def test_async_explicit_repair_rejects_bad_authority_without_any_queue_or_receipt(
    async_book, kind, state,
):
    from ai_accounting.kernel.dashboard_async import _produce

    engine = async_book[0]
    if state != "preparing":
        assert _produce(engine, MONTH)["status"] == "ready"
    if state == "corrupt":
        damage_sidecar(engine)
    if kind == "fact":
        damage(engine, "fact_funding", "UPDATE fact_funding SET amount_fen=10 "
               "WHERE revision_id=(SELECT fact_id FROM fact_current "
               "WHERE subject_id='history-funding')")
    else:
        damage(engine, "calculation", "UPDATE calculation SET outcome="
               "json_set(outcome,'$.values.amount',10) WHERE subject_id='history-funding'")
    before, before_sidecar = database_state(engine), sidecar_state(engine)
    before_revision = revision(engine)
    rejected(lambda: Maintenance(engine).rebuild_dashboard_read_model(
        MONTH, request_id="bad-authority-repair",
    ))
    assert database_state(engine) == before
    assert sidecar_state(engine) == before_sidecar
    assert revision(engine) == before_revision


def test_current_sidecar_repair_is_pending_until_real_production_and_keeps_old_authority(
    async_book, monkeypatch,
):
    import ai_accounting.kernel.dashboard_async as asynchronous

    engine = async_book[0]
    assert asynchronous._produce(engine, MONTH)["status"] == "ready"
    expected = Dashboard(engine).brief(MONTH)["data"]["financial_position"]
    original_path = asynchronous._store_path(engine)
    immutable, initial_revision = retained_records(engine), revision(engine)
    damage_sidecar(engine)
    before_sidecar = sidecar_state(engine)
    # Company integrity authenticates books and in-company historical models,
    # not an external sidecar. Its separate anchored read must reject damage.
    assert verify(engine)["status"] == "verified"
    rejected(lambda: Dashboard(engine).brief(MONTH))
    before = database_state(engine)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        assert asynchronous._delivery_state_in_snapshot(engine, connection, MONTH)["state"] == (
            "failed"
        )
    assert database_state(engine) == before
    calls, queue = [], asynchronous._queue

    def counted_queue(*args, **kwargs):
        calls.append(args[1])
        return queue(*args, **kwargs)

    monkeypatch.setattr(asynchronous, "_queue", counted_queue)
    result = Maintenance(engine).rebuild_dashboard_read_model(MONTH, request_id="sidecar-repair")
    assert result["status"] == "pending" and result["changed"] is True
    assert result["read_repair_revision"] == initial_revision + 1
    assert calls == [MONTH]
    assert asynchronous._store_path(engine) != original_path
    assert sidecar_state(engine) is None
    assert_original_records_unchanged(immutable, engine)
    assert asynchronous._produce(engine, MONTH)["status"] == "ready"
    assert asynchronous.delivery_state(engine, MONTH)["state"] == "ready"
    assert Dashboard(engine).brief(MONTH)["data"]["financial_position"] == expected
    import sqlite3

    with sqlite3.connect(original_path.as_uri() + "?mode=ro", uri=True) as connection:
        assert tuple(connection.iterdump()) == before_sidecar
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM request WHERE id='sidecar-repair'",
        ).fetchone()[0] == 1


@pytest.mark.parametrize("stage", ("published", "commit"))
def test_async_failed_schedule_rolls_back_reset_revision_queue_and_audit(async_book, stage):
    from ai_accounting.kernel.dashboard_async import _produce

    engine = async_book[0]
    assert _produce(engine, MONTH)["status"] == "ready"
    damage_sidecar(engine)
    before, before_sidecar = database_state(engine), sidecar_state(engine)
    before_revision = revision(engine)

    def interrupt(point, _connection):
        if point == stage:
            raise RuntimeError("synthetic scheduling interruption")

    failed = type(engine)(engine.store, fault=interrupt)
    with pytest.raises(RuntimeError, match="synthetic scheduling interruption"):
        Maintenance(failed).rebuild_dashboard_read_model(MONTH, request_id="retry-async-repair")
    assert database_state(engine) == before
    assert sidecar_state(engine) == before_sidecar
    assert revision(engine) == before_revision
    result = Maintenance(engine).rebuild_dashboard_read_model(
        MONTH, request_id="retry-async-repair",
    )
    assert result["status"] == "pending" and result["changed"] is True


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
                "brief/default/overview/0" if target == "summary" else "funds/default/overview/0"
            ]
        )
        damage(
            engine,
            "dashboard_read_model_block",
            "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?",
            (target_digest,),
        )
    operation = (
        # Current content deliberately falls back to native reads for voucher
        # continuation pages. Damage a collection block the default page uses.
        (lambda: dashboard.funds(MONTH, movement_account_selection="first"))
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

    failed = type(engine)(engine.store, fault=interrupt)
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
    Maintenance(engine).rebuild_dashboard_read_model(MONTH, request_id="false-summary-bootstrap")
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
            "bank-a",
            {"display_name": "synthetic faulty producer update"},
            source="synthetic owner update",
            expected_revision=1,
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
        assert (
            connection.execute(
                "SELECT 1 FROM dashboard_read_model_block WHERE digest=?", (old_root,)
            ).fetchone()
            is None
        )
    assert Dashboard(engine).brief(MONTH)["data"] is not None
    assert verify(engine)["status"] == "verified"
    result = Maintenance(engine).rebuild_dashboard_read_model(
        MONTH,
        request_id="no-old-ui-reconstruction",
    )
    assert result["changed"] is False
    with engine.store.connection(read_only=True) as connection:
        assert (
            loads_unique(
                connection.execute(
                    "SELECT descriptor FROM dashboard_read_model_commit WHERE id=?",
                    (old_commit,),
                ).fetchone()[0]
            )
            == descriptor
        )


def test_frozen_accounting_content_is_never_a_derived_model_repair_target(tmp_path):
    engine, _, _ = closed_banks(current_book_factory.__wrapped__(tmp_path))
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


def test_archived_draft_without_model_tables_is_rejected_and_preserved(tmp_path):
    path, _ = archived_company(tmp_path / "archived.sqlite", FINGERPRINTS[2])
    original = path.read_bytes()
    engine = Engine(
        Store(
            path, production_bundle(), "archived-synthetic-company", "archived-synthetic-database"
        )
    )
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief("2026-02")
    assert failure.value.code == "schema_fingerprint_mismatch"
    with pytest.raises(BackupError) as failure:
        verify_file(path, _bundle=production_bundle())
    assert failure.value.code == "backup_schema_unsupported"
    assert path.read_bytes() == original


def test_fixed_v1_fixture_keeps_its_registered_source_verifier(released_book, monkeypatch):
    from test_v1_stored_json_isolation import _block_current_decoders

    import ai_accounting.kernel.dashboard_model_materializer as materializer

    legacy, bundle = released_book
    # Preserve the registered content-v1 fixture unchanged. It verifies frozen
    # source interpretation; archived draft shape support is deliberately absent.

    def forbidden(*_args, **_kwargs):
        raise AssertionError("fixed v1 called current dashboard materializer")

    monkeypatch.setattr(materializer, "build_model", forbidden)
    with monkeypatch.context() as future:
        _block_current_decoders(future)
        result = verify_file(legacy.engine.store.path, _bundle=bundle)
    assert result["database_format"]["version"] == 1
    assert result["verification"]["status"] == "verified"
    assert result["verification"]["counts"]["facts"] > 0
