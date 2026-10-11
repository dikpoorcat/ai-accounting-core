"""Narrow dashboard reads bind frozen adoption independently of voucher ownership."""

import pytest
from close_storage_fixture import replace_stored_manifest, stored_manifest
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage

from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _Snapshot
from ai_accounting.kernel.dashboard_reads import Journal
from ai_accounting.kernel.query_reads import QueryReads


@pytest.fixture
def reviewed_book(engine):
    subjects = ("charge-a", "charge-b")
    for subject in subjects:
        save(engine, subject=subject, request="save-" + subject)
    publish(engine, subjects=list(subjects))
    evidence = engine.register_evidence(
        b"Explicit unchanged charge confirmation", "text/plain", "review confirmation",
        request_id="review-evidence",
    )["digest"]
    for subject in subjects:
        engine.save_fact(
            "test_charge", subject, {"period": "2026-01", "amount": 100},
            evidence=(evidence,), expected_revision=1, request_id="review-" + subject,
        )
    _, result = publish(engine, subjects=list(subjects), request="publish-review")
    assert all(item["impact"] == "review_no_impact" for item in result["results"])
    close(engine)
    with engine.store.connection(read_only=True) as connection:
        manifest = stored_manifest(connection, "2026-01")
        by_subject = {row["id"]: row["subject_id"] for row in connection.execute(
            "SELECT id,subject_id FROM calculation"
        )}
    leaves = {by_subject[item["calculation_id"]]: item for item in manifest["vouchers"]}
    assert set(leaves) == set(subjects)
    assert all(item["calculation_id"] != item["adopted_calculation_id"]
               for item in leaves.values())
    return engine, leaves


def frozen_proofs(reads):
    return {key: value for key, value in reads._report_snapshot_cache.items()
            if isinstance(key, tuple) and key[0] == "authenticated_frozen_voucher_adoption"}


def damage_adopted(engine, leaf, field):
    ident = leaf["adopted_calculation_id"]
    if field == "outcome":
        damage(engine, "calculation",
               "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount',101) "
               "WHERE id=?", (ident,))
    elif field == "lines":
        damage(engine, "calculation",
               "UPDATE calculation SET outcome=json_set(outcome,'$.lines[0].account','5601') "
               "WHERE id=?", (ident,))
    elif field == "digest":
        damage(engine, "calculation", "UPDATE calculation SET digest=? WHERE id=?",
               (bytes(32), ident))
    elif field == "publication":
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE calculation_id=?", (ident,),
               foreign_keys=False)
    else:
        with engine.store.connection(read_only=True) as connection:
            fact_id = connection.execute("SELECT fact_id FROM calculation WHERE id=?",
                                         (ident,)).fetchone()[0]
        damage(engine, "fact_test_charge", "UPDATE fact_test_charge SET amount=101 "
               "WHERE revision_id=?", (fact_id,))


@pytest.mark.parametrize("section", ("activity", "vouchers"))
def test_reviewed_adoption_is_legal_without_overview_money_proof(
    reviewed_book, monkeypatch, section,
):
    engine, leaves = reviewed_book

    def overview_proof(*_, **__):
        raise AssertionError("partial scope invoked complete overview money proof")

    monkeypatch.setattr(Journal, "account_amounts", overview_proof)
    result = Dashboard(engine).brief("2026-01", section=section, limit=1)
    assert result["scope"] == section
    collection = result["data"]["collections"][section]
    assert collection["page"]["total_count"] == 2
    assert collection["page"]["returned_count"] == 1
    if section == "vouchers":
        item = collection["items"][0]
        assert item["voucher_version_id"] in {leaf["id"] for leaf in leaves.values()}
        assert sum(line["debit_fen"] for line in item["lines"]) == 100
        assert sum(line["credit_fen"] for line in item["lines"]) == 100


@pytest.mark.parametrize("field", ("outcome", "lines", "digest", "publication", "fact"))
@pytest.mark.parametrize("section", ("activity", "vouchers"))
def test_partial_rejects_damage_to_reviewed_adopted_not_owner(reviewed_book, field, section):
    engine, leaves = reviewed_book
    damage_adopted(engine, leaves["charge-a"], field)
    with pytest.raises(KernelError):
        Dashboard(engine).brief("2026-01", section=section)


@pytest.mark.parametrize("field", ("publication_id", "fact_id", "source_period", "result_digest"))
def test_frozen_adopted_metadata_must_match_actual_selected_result(reviewed_book, field):
    engine, leaves = reviewed_book
    with engine.store.connection(read_only=True) as connection:
        manifest = stored_manifest(connection, "2026-01")
    entry = next(item for item in manifest["adopted_results"]
                 if item["calculation_id"] == leaves["charge-a"]["adopted_calculation_id"])
    entry[field] = {"publication_id": "different-publication", "fact_id": "different-fact",
                    "source_period": "2025-12", "result_digest": bytes(32).hex()}[field]
    replace_stored_manifest(engine, manifest)
    with Dashboard(engine)._snapshot("2026-01") as snap:
        with pytest.raises(KernelError) as failure:
            snap.month_journal.authenticated_headers()
        assert failure.value.code == "content_integrity_failed"
        assert failure.value.details["reason"] == (
            "storage_adoption_publication_mismatch" if field == "publication_id"
            else "frozen_adopted_result_mismatch"
        )
        assert not frozen_proofs(snap.reads)


def test_activity_checks_complete_partition_but_vouchers_only_returned_page(reviewed_book):
    engine, leaves = reviewed_book
    dashboard = Dashboard(engine)
    first = dashboard.brief("2026-01", section="vouchers", limit=1)
    first_id = first["data"]["collections"]["vouchers"]["items"][0]["voucher_version_id"]
    unreturned = next(leaf for leaf in leaves.values() if leaf["id"] != first_id)
    damage_adopted(engine, unreturned, "outcome")
    retained = dashboard.brief("2026-01", section="vouchers", limit=1)
    assert retained["data"]["collections"]["vouchers"]["items"] == (
        first["data"]["collections"]["vouchers"]["items"]
    )
    with pytest.raises(KernelError):
        dashboard.brief("2026-01", section="activity", limit=1)
    with pytest.raises(KernelError):
        dashboard.brief("2026-01", section="vouchers", limit=2)


def test_failed_batch_does_not_publish_any_frozen_adoption_proof(reviewed_book):
    engine, leaves = reviewed_book
    damage_adopted(engine, leaves["charge-b"], "outcome")
    with Dashboard(engine)._snapshot("2026-01") as snap:
        for _ in range(2):
            with pytest.raises(KernelError):
                snap.month_journal.authenticated_headers()
            assert not frozen_proofs(snap.reads)


def test_successful_adoption_proof_reused_only_in_its_snapshot(reviewed_book, monkeypatch):
    engine, leaves = reviewed_book
    calls = []
    original = QueryReads.close_accounting_many

    def counted(reads, rows, **options):
        calls.append(options["subjects_by_period"])
        return original(reads, rows, **options)

    monkeypatch.setattr(QueryReads, "close_accounting_many", counted)
    with Dashboard(engine)._snapshot("2026-01") as snap:
        rows = snap.month_journal.authenticated_headers()
        snap.month_journal.authenticated_frozen_adoptions(rows)
        snap.month_journal.authenticated_headers()
        assert len(calls) == 1
        assert len(frozen_proofs(snap.reads)) == 2
    damage_adopted(engine, leaves["charge-a"], "outcome")
    with Dashboard(engine)._snapshot("2026-01") as snap:
        with pytest.raises(KernelError):
            snap.month_journal.authenticated_headers()
    assert len(calls) == 2


@pytest.mark.parametrize("reader", ("fixed", "unowned"))
def test_fallback_does_not_consume_native_adoption_cache_or_anchors(
    reviewed_book, monkeypatch, reader,
):
    from ai_accounting.kernel import report_open_contribution

    engine, leaves = reviewed_book
    with Dashboard(engine)._snapshot("2026-01") as snap:
        snap.month_journal.authenticated_headers()
        cached = frozen_proofs(snap.reads)
    damage_adopted(engine, leaves["charge-a"], "outcome")

    def native_anchor(*_, **__):
        raise AssertionError("fallback consumed current contribution anchors")

    monkeypatch.setattr(report_open_contribution, "verify_published_source_bindings", native_anchor)
    if reader == "fixed":
        with historical_content(1), Dashboard(engine)._snapshot("2026-01") as snap:
            snap.reads._report_snapshot_cache.update(cached)
            with pytest.raises(KernelError):
                snap.month_journal.authenticated_headers()
            assert frozen_proofs(snap.reads) == cached
    else:
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            reads = QueryReads(engine, connection)
            reads._report_snapshot_cache.update(cached)
            snap = _Snapshot(engine, connection, "2026-01", reads)
            try:
                with pytest.raises(KernelError):
                    snap.month_journal.authenticated_headers()
                assert frozen_proofs(reads) == cached
            finally:
                snap.release()


@pytest.mark.parametrize("consume", ("activity", "vouchers", "detail"))
def test_reversal_checks_original_reviewed_frozen_adoption(reviewed_book, consume):
    engine, leaves = reviewed_book
    save(engine, subject="charge-a", amount=150, revision=2, request="correction")
    publish(engine, subjects=["charge-a"], request="publish-correction", posting_period="2026-02")
    dashboard = Dashboard(engine)
    if consume != "detail":
        assert dashboard.brief("2026-02", section=consume)["scope"] == consume
    with engine.store.connection(read_only=True) as connection:
        reversal_id = connection.execute(
            "SELECT id FROM voucher_version WHERE reverses_id=?", (leaves["charge-a"]["id"],),
        ).fetchone()[0]
    damage_adopted(engine, leaves["charge-a"], "outcome")
    with pytest.raises(KernelError):
        if consume == "detail":
            with dashboard._snapshot("2026-02") as snap:
                snap.month_journal.page(0, 1, voucher_version_id=reversal_id, include_lines=False)
        else:
            dashboard.brief("2026-02", section=consume)


@pytest.mark.parametrize("reader", ("fixed", "unowned"))
def test_fallback_exact_page_keeps_complete_header_proof_and_narrow_adoption(reviewed_book, reader):
    engine, leaves = reviewed_book
    target = leaves["charge-a"]

    def consume(snap):
        records, page = snap.month_journal.page(
            0, 1, voucher_version_id=target["id"], include_lines=False,
        )
        assert page["total_count"] == 2 and len(records) == 1
        assert records[0]["id"] == target["id"]
        actual = snap.month_journal.authenticated_selected_lines(records)[target["id"]]
        assert sum(line["debit"] for line in actual) == 100
        assert sum(line["credit"] for line in actual) == 100
        assert not frozen_proofs(snap.reads)

    def run():
        if reader == "fixed":
            with historical_content(1), Dashboard(engine)._snapshot("2026-01") as snap:
                consume(snap)
        else:
            with engine.store.connection(read_only=True) as connection:
                connection.execute("BEGIN")
                reads = QueryReads(engine, connection)
                snap = _Snapshot(engine, connection, "2026-01", reads)
                try:
                    consume(snap)
                finally:
                    snap.release()

    run()
    damage_adopted(engine, leaves["charge-b"], "outcome")
    run()
    damage_adopted(engine, target, "outcome")
    with pytest.raises(KernelError):
        run()


def test_precise_detail_checks_returned_frozen_lines_without_rendering_them(reviewed_book):
    engine, leaves = reviewed_book
    target = leaves["charge-a"]
    damage(engine, "voucher_line", "UPDATE voucher_line SET account='5601' "
           "WHERE version_id=? AND debit>0", (target["id"],))
    with Dashboard(engine)._snapshot("2026-01") as snap:
        with pytest.raises(KernelError) as failure:
            snap.month_journal.page(0, 1, voucher_version_id=target["id"], include_lines=False)
        assert failure.value.details["reason"] == "voucher_lines_mismatch"
