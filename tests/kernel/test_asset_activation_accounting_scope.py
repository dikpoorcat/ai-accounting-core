"""Activation counts consume exact activation leaves after all-owner source proof."""

from unittest.mock import patch

import pytest
from close_storage_fixture import replace_stored_manifest, stored_manifest
from stage9_metrics import measure_work
from test_asset_batch_reads import prepare_batch_assets
from test_asset_batches import month
from test_asset_owner_frozen_scope import amend_activation
from test_asset_owner_frozen_scope import owner_book as owner_book  # noqa: F401
from test_asset_owner_kind_sources import change_owner_kind
from test_dashboard_model_transactions import raw_read
from test_integrity_content import damage
from test_payroll_corrections import Company

from ai_accounting.kernel import close_storage, dashboard_model_runtime
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _active_asset_counts
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth


def counts(engine, period):
    with Dashboard(engine)._snapshot(period) as snap:
        return _active_asset_counts(snap)


def identities(snap, *, narrow=True):
    return snap.queries._selected_asset_activation_identities(
        snap.connection, snap.period,
        **({"outcome_kinds": {"asset_activation_batch"}} if narrow else {}),
    )


def full_scope(original):
    def consume(self, connection, period, **_options):
        return original(self, connection, period)
    return consume


def physical(work):
    statements = [item for item in work["sql"] if any(table in item["statement"] for table in (
        "close_storage_subroot s", "close_storage_directory d", "close_storage_block b",
    ))]
    return {field: sum(item[field] for item in statements)
            for field in ("returned_rows", "returned_value_bytes")}


def test_counts_leave_consumption_accounting_unread_with_120_frozen_months(
    tmp_path, record_property,
):
    # This fixture exercises native frozen-leaf reads. Keep every typed write,
    # publication and real close, without constructing unrelated UI models.
    with patch.object(dashboard_model_runtime, "supports_native_production", return_value=False):
        company = Company(tmp_path / "activation-accounting-growth.sqlite")
        prepare_batch_assets(company)
        company.close("2026-02")
    engine = company.engine
    original = BusinessQueries._selected_asset_activation_identities
    decoded = []
    family = close_storage._decode_family

    def counted(header, kind, row, **options):
        value = family(header, kind, row, **options)
        if kind == "accounting":
            decoded.append(header.period)
        return value

    first = YearMonth("2026-02").ordinal
    prepared = 0
    samples = []
    for length in (0, 48, 120):
        with patch.object(
            dashboard_model_runtime, "supports_native_production", return_value=False,
        ):
            while prepared < length:
                prepared += 1
                period = str(YearMonth.from_ordinal(first + prepared))
                month(engine, company.owner_confirmation, period, f"activation-growth-{prepared}")
                company.close(period)
        period = str(YearMonth.from_ordinal(first + length))
        with patch.object(BusinessQueries, "_selected_asset_activation_identities",
                          full_scope(original)):
            before, expected = measure_work(engine, lambda period=period: counts(engine, period))
        decoded.clear()
        with patch.object(close_storage, "_decode_family", counted):
            after, actual = measure_work(engine, lambda period=period: counts(engine, period))
        assert actual == expected
        assert actual == {"fixed": 2}
        assert decoded == [first]
        with Dashboard(engine)._snapshot(period) as snap:
            events = identities(snap)
            assert {event["asset_id"] for event in events} == {"computer", "chair"}
            assert all(event["frozen_identity"] for event in events)
            # Every consumption owner retains a real physical typed-fact proof.
            required = {row[0] for row in snap.connection.execute(
                "SELECT fact_id FROM calculation WHERE kind='asset_consumption_month'",
            )}
            assert len(required) == length
            assert required <= snap.reads._fact_versions.keys()
        before_physical, after_physical = physical(before), physical(after)
        if length:
            assert after_physical["returned_rows"] < before_physical["returned_rows"]
            assert after_physical["returned_value_bytes"] < before_physical["returned_value_bytes"]
        samples.append(after_physical)
        record_property(f"full_{length}", before["counters"])
        record_property(f"activation_{length}", after["counters"])
        record_property(f"full_leaf_{length}", before_physical)
        record_property(f"activation_leaf_{length}", after_physical)
    # Root headers, declarations and typed owner facts necessarily grow. Only
    # accounting-family/directory/block consumption stays at the activation leaf.
    # A consumption posting creates one necessary immutable reference block
    # used by the common adopted-asset heads. Its first appearance is bounded;
    # the additional 72 consumption months must add no accounting payload.
    assert samples[1] == samples[2]
    assert samples[1]["returned_rows"] <= samples[0]["returned_rows"] + 1


@pytest.mark.parametrize("selected", [False, True])
def test_leaf_damage_is_rejected_by_its_actual_consumer(owner_book, selected):
    company, _ = owner_book
    engine = company.engine
    period = YearMonth("2026-02" if selected else "2026-03").ordinal
    damage(engine, "close_storage_block",
           "UPDATE close_storage_block SET content=content||' ' "
           "WHERE period=? AND field='adopted_results'", (period,))
    if selected:
        with pytest.raises(KernelError):
            counts(engine, "2026-03")
    else:
        assert counts(engine, "2026-03") == {"fixed": 2}
    # Exercise the complete native asset reader, which consumes both owner kinds
    # in an independent scope; the count cannot authenticate its damaged leaf.
    with raw_read(), pytest.raises(KernelError):
        Dashboard(engine).assets("2026-03", preparation="deferred")
    if not selected:
        with Dashboard(engine)._snapshot("2026-03") as snap:
            assert identities(snap)
            with pytest.raises(KernelError):
                identities(snap, narrow=False)


@pytest.mark.parametrize("broken", [
    "kind", "fact", "publication", "seal", "root", "head_redirect",
])
def test_excluded_consumption_still_requires_all_owner_source_proof(owner_book, broken):
    company, _ = owner_book
    engine = company.engine
    with engine.store.connection(read_only=True) as connection:
        owner = connection.execute(
            "SELECT id,fact_id FROM calculation WHERE kind='asset_consumption_month'",
        ).fetchone()
    if broken == "kind":
        change_owner_kind(engine, "asset_consumption_month", "asset_activation_batch")
    elif broken == "fact":
        damage(engine, "fact_revision", "UPDATE fact_revision SET digest=? WHERE id=?",
               (bytes(32), owner["fact_id"]))
    elif broken == "publication":
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE calculation_id=?", (owner["id"],),
               foreign_keys=False)
    elif broken == "seal":
        damage(engine, "fact_seal", "DELETE FROM fact_seal WHERE fact_id=?", (owner["fact_id"],),
               foreign_keys=False)
    elif broken == "head_redirect":
        damage(engine, "calculation_current",
               "DELETE FROM calculation_current WHERE calculation_id=?", (owner["id"],))
        damage(engine, "calculation_current",
               "UPDATE calculation_current SET calculation_id=? "
               "WHERE subject_id='activation-batch'", (owner["id"],))
    else:
        damage(engine, "close_storage_root",
               "UPDATE close_storage_root SET storage_digest=zeroblob(32) "
               "WHERE period=?", (YearMonth("2026-03").ordinal,))
        with pytest.raises(KernelError):
            counts(engine, "2026-03")
        return
    with Dashboard(engine)._snapshot("2026-03") as snap:
        with pytest.raises(KernelError):
            identities(snap)
        assert ("asset_activation_identity_events", snap.month,
                frozenset({"asset_activation_batch"})) not in snap.reads._report_snapshot_cache


@pytest.mark.parametrize("review", [False, True])
def test_selected_owner_corrections_and_noimpact_keep_exact_adoption(owner_book, review):
    company, evidence = owner_book
    if review:
        evidence = company.engine.register_evidence(
            b"Additional unchanged activation confirmation", "text/plain", "owner confirmation",
            request_id="activation-scope-review-proof",
        )["digest"]
    result = amend_activation(
        company, evidence, revision=1, posting_period="2026-02" if review else "2026-04",
        benefit_area="administration" if review else "sales",
    )
    if review:
        assert any(item["impact"] == "review_no_impact" for item in result["results"])
    period = "2026-03"
    if not review:
        month(company.engine, evidence, "2026-04", "activation-scope-correction-april")
        company.close("2026-04")
        amend_activation(company, evidence, revision=2, posting_period="2026-05",
                         benefit_area="administration")
        month(company.engine, evidence, "2026-05", "activation-scope-correction-may")
        company.close("2026-05")
        period = "2026-05"
    with Dashboard(company.engine)._snapshot(period) as snap:
        narrow = identities(snap)
        full = identities(snap, narrow=False)
        assert narrow == full
        assert {event["asset_id"] for event in narrow} == {"computer", "chair"}
    assert counts(company.engine, period) == {"fixed": 2}


def test_fixed_and_unowned_identity_preserve_complete_fallback(owner_book):
    company, _ = owner_book
    engine = company.engine
    with historical_content(1), Dashboard(engine)._snapshot("2026-03") as snap:
        assert identities(snap) is None
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        queries = BusinessQueries(engine)
        assert queries._selected_asset_activation_identities(
            connection, "2026-03", outcome_kinds={"asset_activation_batch"},
        ) is None
        reads = QueryReads(engine, connection)
        rows = reads.authoritative_close_rows(
            periods=(YearMonth("2026-02").ordinal, YearMonth("2026-03").ordinal),
            through_period=YearMonth("2026-03").ordinal,
        )
        subjects = {row[0] for row in connection.execute(
            "SELECT DISTINCT subject_id FROM calculation WHERE kind IN "
            "('asset_activation_batch','asset_consumption_month')",
        )}
        activation = {"activation-batch"}
        narrowed_request = reads.close_asset_owner_accounting_many(
            rows, subjects=activation, owner_subjects=subjects,
        )
        complete = reads.close_accounting_many(rows, subjects=subjects)
        assert narrowed_request == complete


@pytest.mark.parametrize("broken", ["missing_declaration", "wrong_role"])
def test_selected_activation_remains_visible_outside_damaged_declaration(owner_book, broken):
    company, _ = owner_book
    engine = company.engine
    with engine.store.connection(read_only=True) as connection:
        manifest = stored_manifest(connection, "2026-02")
    if broken == "missing_declaration":
        manifest["asset_batch_adoptions"] = []
    else:
        owner = next(item for item in manifest["adopted_results"]
                     if item["subject_id"] == "activation-batch")
        owner["role"] = "journal_basis"
    # This synthetic semantic damage deliberately preserves physical encoding;
    # the actual activation publication still expands the selected authority.
    replace_stored_manifest(engine, manifest)
    with pytest.raises(KernelError):
        counts(engine, "2026-03")
