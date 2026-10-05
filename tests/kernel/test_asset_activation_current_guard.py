"""Frozen card identity never replaces current owner adoption or body consumers."""

import json
from unittest.mock import patch

import pytest
from stage9_metrics import measure_work
from test_asset_batch_reads import prepare_batch_assets
from test_asset_owner_frozen_scope import amend_activation
from test_integrity_content import damage, verify
from test_owner_asset_activation_identity import closed_cards
from test_payroll_corrections import Company

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard


def complete_current_guard_page(engine, period, **options):
    original = BusinessQueries._current_accounting_heads

    def complete_guard(queries, connection, *args, **kwargs):
        reads = queries._reads(connection)
        key = ("asset_activation_identity_events", kwargs.get("cutoff"))
        cached = reads._report_snapshot_cache.pop(key, None)
        try:
            return original(queries, connection, *args, **kwargs)
        finally:
            if cached is not None:
                reads._report_snapshot_cache[key] = cached

    # The control constructs the same independent typed/directory proof first.
    # Only its current-member guard consumes the former complete-body path.
    with patch.object(BusinessQueries, "_current_accounting_heads", complete_guard):
        return Dashboard(engine).assets(period, preparation="deferred", **options)


@pytest.mark.parametrize("count", [2, 8])
def test_complete_assets_equal_with_less_actual_activation_body_work(tmp_path, count):
    company, identities = closed_cards(tmp_path, count)
    engine = company.engine
    before_work, before = measure_work(
        engine, lambda: complete_current_guard_page(engine, "2026-04"),
    )
    after_work, after = measure_work(
        engine, lambda: Dashboard(engine).assets("2026-04", preparation="deferred"),
    )
    assert after == before
    before_counts, after_counts = before_work["counters"], after_work["counters"]
    # Exactly the closed activation owner plus its entire member directory
    # leave this duplicate guard. The open consumption body lane is unchanged.
    assert before_counts["calculation_result_rows_loaded"] - after_counts[
        "calculation_result_rows_loaded"
    ] == count + 1
    assert before_counts["calculation_result_json_decodes"] - after_counts[
        "calculation_result_json_decodes"
    ] == count + 1
    assert after_counts["calculation_result_bytes_loaded"] < before_counts[
        "calculation_result_bytes_loaded"
    ]
    assert after_counts["typed_fact_json_decodes"] == before_counts["typed_fact_json_decodes"]
    print(json.dumps({"cards": count, "full_guard": before_counts, "identity_guard": after_counts}))
    for options in ({"asset_filter": "fixed"}, {"asset_id": identities[-1]}):
        assert Dashboard(engine).assets("2026-04", preparation="deferred", **options) == (
            complete_current_guard_page(engine, "2026-04", **options)
        )
    # Successful identity-only reuse never publishes a complete body proof.
    for _ in range(2):
        with Dashboard(engine)._snapshot("2026-04") as snap:
            events = snap.queries._selected_asset_activation_identities(
                snap.connection, snap.period,
            )
            subjects = {event["subject_id"] for event in events}
            snap.queries._current_accounting_heads(snap.connection, subjects, cutoff=snap.month)
            assert not snap.reads._asset_members
            assert not {event["calculation_id"] for event in events} & snap.reads._metadata.keys()


@pytest.fixture
def closed_activation(tmp_path):
    company = Company(tmp_path / "current-activation.sqlite")
    prepare_batch_assets(company)
    company.close("2026-02")
    return company


def activation_sources(engine):
    with engine.store.connection(read_only=True) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT m.*,p.id publication_id,p.voucher_id FROM asset_batch_member m "
            "JOIN calculation_publication p ON p.calculation_id=m.owner_calculation_id "
            "JOIN calculation c ON c.id=m.owner_calculation_id "
            "WHERE c.kind='asset_activation_batch' ORDER BY m.position",
        )]


def test_current_member_redirect_survives_complete_frozen_identity_proof(closed_activation):
    engine = closed_activation.engine
    first, second = activation_sources(engine)
    damage(engine, "calculation_current",
           "DELETE FROM calculation_current WHERE subject_id=?", (second["member_subject_id"],))
    damage(engine, "calculation_current",
           "UPDATE calculation_current SET calculation_id=? WHERE subject_id=?",
           (second["member_calculation_id"], first["member_subject_id"]))
    for _ in range(2):
        with Dashboard(engine)._snapshot("2026-02") as snap:
            events = snap.queries._selected_asset_activation_identities(
                snap.connection, snap.period,
            )
            assert len(events) == 2
            assert not snap.reads._asset_members
            with pytest.raises(KernelError, match="当前核算头"):
                snap.queries._current_accounting_heads(
                    snap.connection, {first["member_subject_id"]}, cutoff=snap.month,
                )
            assert not snap.reads._asset_members
    for read in (
        lambda: Dashboard(engine).assets("2026-02", preparation="deferred"),
        lambda: complete_current_guard_page(engine, "2026-02"),
    ):
        with pytest.raises(KernelError):
            read()


def test_same_subject_unadopted_current_member_is_not_a_proved_member(closed_activation):
    engine = closed_activation.engine
    source = activation_sources(engine)[0]
    new_id = "c_synthetic_unadopted_activation"
    damage(engine, "calculation",
           "INSERT INTO calculation(id,subject_id,fact_id,kind,period,outcome,digest,"
           "program_version) "
           "SELECT ?,subject_id,fact_id,kind,period,outcome,digest,program_version "
           "FROM calculation WHERE id=?", (new_id, source["member_calculation_id"]),
           foreign_keys=False)
    damage(engine, "calculation_current",
           "UPDATE calculation_current SET calculation_id=? WHERE subject_id=?",
           (new_id, source["member_subject_id"]))
    with Dashboard(engine)._snapshot("2026-02") as snap:
        events = snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
        assert new_id not in {event["calculation_id"] for event in events}
        with pytest.raises(KernelError, match="当前资产成员缺少精确所有者采用"):
            snap.queries._current_accounting_heads(
                snap.connection, {source["member_subject_id"]}, cutoff=snap.month,
            )
        assert not snap.reads._asset_members


@pytest.mark.parametrize("missing", ["owner_current", "owner_publication", "voucher_current"])
def test_missing_current_adoption_is_not_hidden_by_frozen_identity(closed_activation, missing):
    engine = closed_activation.engine
    source = activation_sources(engine)[0]
    table, column, ident = {
        "owner_current": ("calculation_current", "calculation_id", source["owner_calculation_id"]),
        "owner_publication": ("calculation_publication", "id", source["publication_id"]),
        "voucher_current": ("voucher_current", "voucher_id", source["voucher_id"]),
    }[missing]
    damage(engine, table, f"DELETE FROM {table} WHERE {column}=?", (ident,), foreign_keys=False)
    for read in (
        lambda: Dashboard(engine).assets("2026-02", preparation="deferred"),
        lambda: complete_current_guard_page(engine, "2026-02"),
    ):
        with pytest.raises(KernelError):
            read()


def test_review_new_current_owner_keeps_complete_guard_and_body_scope(closed_activation):
    company = closed_activation
    engine = company.engine
    proof = engine.register_evidence(
        b"Explicit unchanged activation confirmation", "text/plain", "review guard proof",
        request_id="current-guard-review",
    )["digest"]
    result = amend_activation(
        company, proof, revision=1, posting_period="2026-02", benefit_area="administration",
    )
    assert any(item["impact"] == "review_no_impact" for item in result["results"])
    with Dashboard(engine)._snapshot("2026-02") as snap:
        events = snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
        current = snap.connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='activation-batch'",
        ).fetchone()[0]
        assert current not in {event["owner_calculation_id"] for event in events}
        assert not snap.reads._asset_members
        snap.queries._current_accounting_heads(
            snap.connection, {event["subject_id"] for event in events}, cutoff=snap.month,
        )
        assert current in snap.reads._asset_members
    assert Dashboard(engine).assets("2026-02", preparation="deferred") == (
        complete_current_guard_page(engine, "2026-02")
    )


@pytest.mark.parametrize("scope", ["include_members", "posting_period", "fixed_v1", "unowned"])
def test_other_read_scopes_keep_complete_current_guard(closed_activation, scope):
    engine = closed_activation.engine
    with Dashboard(engine)._snapshot("2026-02") as snap:
        events = snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
        subjects = {event["subject_id"] for event in events}
        owner_ids = {event["owner_calculation_id"] for event in events}
        options = {"cutoff": snap.month}
        queries = snap.queries
        if scope == "include_members":
            options["include_members"] = True
        elif scope == "posting_period":
            options["posting_period"] = snap.month
        elif scope == "unowned":
            queries = BusinessQueries(engine)
        if scope == "fixed_v1":
            with historical_content(1):
                queries._current_accounting_heads(snap.connection, subjects, **options)
        else:
            queries._current_accounting_heads(snap.connection, subjects, **options)
        assert owner_ids <= queries._reads(snap.connection)._asset_members.keys()


@pytest.mark.parametrize("body", ["owner", "member"])
def test_identity_reuse_does_not_certify_unconsumed_body_bytes(closed_activation, body):
    engine = closed_activation.engine
    source = activation_sources(engine)[0]
    ident = source[f"{body}_calculation_id"]
    with engine.store.connection(read_only=True) as connection:
        outcome = json.loads(connection.execute(
            "SELECT outcome FROM calculation WHERE id=?", (ident,),
        ).fetchone()[0])
    outcome["unused_historical_field"] = True
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
           (json.dumps(outcome), ident))
    with Dashboard(engine)._snapshot("2026-02") as snap:
        events = snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
        snap.queries._current_accounting_heads(
            snap.connection, {event["subject_id"] for event in events}, cutoff=snap.month,
        )
        assert not snap.reads._asset_members
        assert ident not in snap.reads._verified_sql_outcomes
        assert ident not in snap.reads._verified_source_contents
        with pytest.raises(KernelError):
            snap.reads.asset_members_many({source["owner_calculation_id"]})
        assert not snap.reads._asset_members
    with pytest.raises(KernelError):
        complete_current_guard_page(engine, "2026-02")
    # This fixture has no consumption owner: the page's amount consumer still
    # needs the activation body and must perform its own complete validation.
    with pytest.raises(KernelError):
        Dashboard(engine).assets("2026-02", preparation="deferred")
    with pytest.raises(KernelError):
        verify(engine)
