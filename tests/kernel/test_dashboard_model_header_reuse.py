"""Actual model hits reuse headers without reusing raw-content success flags."""

from contextlib import contextmanager

import pytest
from stage9_metrics import measure_work
from test_banking import book as book
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_model_source_identity_work import published_sources as published_sources
from test_dashboard_model_transactions import source_body_work
from test_engine import close, evidence, publish, save
from test_engine import engine as _header_engine
from test_integrity_content import damage

from ai_accounting.kernel import dashboard_model_sources as sources
from ai_accounting.kernel import query_reads
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_runtime import stable_payload
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth, canonical

header_engine = _header_engine


@contextmanager
def independent_headers(monkeypatch, kind):
    """Execute the previous independent reads, preserving all current body checks."""
    with monkeypatch.context() as patch:
        if kind == "publication":
            patch.setattr(query_reads, "_verify_model_current_publication_voucher_heads",
                          query_reads.verify_current_publication_voucher_heads)
        elif kind == "physical":
            original = sources._verify_model_voucher_rows

            def physical(connection, headers, outcomes, *, normalized_lines=None):
                selected = {row["id"]: dict(row) for row in connection.execute(
                    "SELECT v.* FROM json_each(?) ids JOIN voucher_version v ON v.id=ids.value",
                    (canonical(sorted(headers)),),
                )}
                assert selected.keys() == headers.keys()
                return original(
                    connection, selected, outcomes, normalized_lines=normalized_lines,
                )

            patch.setattr(sources, "_verify_model_voucher_rows", physical)
        else:
            original = sources.verify_model_sources

            def bindings(engine, connection, **arguments):
                identifiers = set(arguments["calculation_ids"])
                rows = connection.execute(
                    "SELECT c.id,c.digest,c.fact_id,f.digest FROM json_each(?) ids "
                    "JOIN calculation c ON c.id=ids.value "
                    "JOIN fact_revision f ON f.id=c.fact_id",
                    (canonical(sorted(identifiers)),),
                )
                assert {(row[0], row[1].hex(), row[2], row[3].hex()) for row in rows} == (
                    set(arguments["expected_sources"])
                )
                fact_ids = set(arguments["fact_ids"])
                if fact_ids:
                    rows = connection.execute(
                        "SELECT f.id,f.digest FROM json_each(?) ids "
                        "JOIN fact_revision f ON f.id=ids.value",
                        (canonical(sorted(fact_ids)),),
                    )
                    assert {(row[0], row[1].hex()) for row in rows} == (
                        set(arguments["expected_facts"])
                    )
                return original(engine, connection, **arguments)

            patch.setattr(sources, "verify_model_sources", bindings)
        yield


def test_500_real_hits_reduce_each_header_read_without_reducing_body_work(book, monkeypatch):
    engine = book[0]
    _publish_filter_funding(engine, ["bank-a"] * 500)
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="header-work-model")

    def read():
        with source_body_work(monkeypatch) as bodies:
            response = Dashboard(engine).brief("2026-09", section="vouchers", limit=500)
        items = response["data"]["collections"]["vouchers"]["items"]
        assert len(items) == 500 and sum(item["amount_fen"] for item in items) == 500
        assert len(bodies["outcomes"]) == len(set(bodies["outcomes"])) == 500
        assert len(bodies["facts"]) == len(set(bodies["facts"])) == 500
        assert bodies["lineages"] == []
        return stable_payload(response), bodies

    reused_work, reused = measure_work(engine, read)
    assert reused_work["counters"]["calculation_result_rows_loaded"] == 500, [
        (row["statement"], row.get("returned_rows", 0), row["calls"])
        for row in reused_work["sql"]
        if "outcome" in row["statement"] or "SELECT * FROM calculation " in row["statement"]
    ]
    results = {}
    for kind in ("bindings", "physical", "publication"):
        with independent_headers(monkeypatch, kind):
            legacy_work, legacy = measure_work(engine, read)
        assert legacy == reused
        assert legacy_work["counters"]["calculation_result_rows_loaded"] == 500
        assert legacy_work["counters"]["returned_rows"] > reused_work["counters"]["returned_rows"]
        assert legacy_work["counters"]["returned_value_bytes"] > (
            reused_work["counters"]["returned_value_bytes"]
        )
        results[kind] = {
            key: legacy_work["counters"][key] - reused_work["counters"][key]
            for key in ("sql_calls", "returned_rows", "returned_value_bytes", "sqlite_vm_steps")
        }
    _publish_filter_funding(engine, ["bank-a"] * 23, period="2026-08")
    grown_work, grown = measure_work(engine, read)
    assert grown == reused
    assert grown_work["counters"]["calculation_result_rows_loaded"] == 500
    # The unrelated month changes only the certified root, not hit-header work.
    assert grown_work["counters"]["returned_rows"] == reused_work["counters"]["returned_rows"]
    print("actual_header_work", results, "unique_outcomes=500 unique_facts=500 history_growth=23")


@pytest.mark.parametrize("kind", ["digest", "fact_id", "fact_digest", "extra_fact", "missing_fact"])
def test_model_binding_compares_actual_checked_headers(published_sources, kind):
    engine, rows = published_sources
    selected = rows["first-funding"]
    with engine.store.connection(read_only=True) as connection:
        fact = connection.execute("SELECT * FROM fact_revision WHERE id=?",
                                  (selected["fact_id"],)).fetchone()
        expected = [selected["id"], selected["digest"].hex(), fact["id"], fact["digest"].hex()]
        explicit = {(fact["id"], fact["digest"].hex())}
        if kind == "extra_fact":
            explicit.add(("f_" + "0" * 64, "0" * 64))
        elif kind == "missing_fact":
            explicit.clear()
        else:
            expected[{"digest": 1, "fact_id": 2, "fact_digest": 3}[kind]] = "0" * 64
        normalized = {}
        with pytest.raises(KernelError) as failure:
            sources.verify_model_sources(
                engine, connection, calculation_ids=[selected["id"]], fact_ids=[fact["id"]],
                expected_sources={tuple(expected)}, expected_facts=explicit,
                normalized_lines=normalized,
            )
        assert failure.value.code == "content_integrity_failed"
        assert normalized == {}


def test_correct_binding_cannot_replace_actual_raw_fact_proof(published_sources):
    engine, rows = published_sources
    selected = rows["first-funding"]
    with engine.store.connection(read_only=True) as connection:
        fact = connection.execute("SELECT * FROM fact_revision WHERE id=?",
                                  (selected["fact_id"],)).fetchone()
        expected = {(selected["id"], selected["digest"].hex(), fact["id"], fact["digest"].hex())}
    damage(engine, "fact_funding", "UPDATE fact_funding SET amount_fen=999 WHERE revision_id=?",
           (selected["fact_id"],))
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            sources.verify_model_sources(
                engine, reads.connection, calculation_ids=[selected["id"]],
                expected_sources=expected,
            )
        assert not reads._verified_publication_ids
        assert not reads._verified_current_voucher_publications


@pytest.mark.parametrize("target", ["current", "publication", "voucher_current", "version"])
def test_failed_actual_header_batch_does_not_publish_memo(published_sources, target):
    engine, rows = published_sources
    selected = rows["second-funding"]
    table, statement = {
        "current": ("calculation_current", "DELETE FROM calculation_current "
                    "WHERE calculation_id=?"),
        "publication": ("calculation_publication", "DELETE FROM calculation_publication "
                        "WHERE calculation_id=?"),
        "voucher_current": ("voucher_current", "DELETE FROM voucher_current WHERE version_id IN("
                            "SELECT id FROM voucher_version WHERE calculation_id=?)"),
        "version": ("voucher_version", "UPDATE voucher_version SET period=period+1 "
                    "WHERE calculation_id=?"),
    }[target]
    damage(engine, table, statement, (selected["id"],), foreign_keys=False)
    ids = [rows["first-funding"]["id"], selected["id"]]
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            fund_sources = []
            with pytest.raises(KernelError):
                sources.verify_model_sources(engine, reads.connection, calculation_ids=ids,
                                             through_period=YearMonth("2026-09").ordinal,
                                             _fund_sources=fund_sources, _reads=reads)
            assert fund_sources == []
            assert not reads._verified_publication_ids
            assert not reads._verified_current_voucher_publications
            assert not reads._verified_source_contents


@pytest.mark.parametrize("mode", ["unowned", "fixed"])
def test_unowned_and_fixed_model_heads_keep_independent_lookup(
    published_sources, monkeypatch, mode,
):
    engine, rows = published_sources
    observed = []
    original = query_reads.verify_current_publication_voucher_heads

    def counted(connection, publications):
        observed.append(tuple(row["id"] for row in publications))
        return original(connection, publications)

    monkeypatch.setattr(query_reads, "verify_current_publication_voucher_heads", counted)
    ident = rows["first-funding"]["id"]
    if mode == "unowned":
        with engine.store.connection(read_only=True) as connection:
            fund_sources = []
            sources.verify_model_sources(
                engine, connection, calculation_ids=[ident], _fund_sources=fund_sources,
                _reads=QueryReads(engine, connection),
            )
            assert fund_sources == []
    else:
        # Source identity encoding is historical too: exercise the exact-head
        # fallback directly against actual SQLite rows, not a current source ID.
        with QueryReads.snapshot(engine) as reads, historical_content(1):
            publications = list(reads.connection.execute(
                "SELECT * FROM calculation_publication WHERE calculation_id=?", (ident,),
            ))
            original(reads.connection, publications)
            independent_records = dict(reads._verified_publication_ids)
        with QueryReads.snapshot(engine) as reads, historical_content(1):
            publications = list(reads.connection.execute(
                "SELECT * FROM calculation_publication WHERE calculation_id=?", (ident,),
            ))
            query_reads._verify_model_current_publication_voucher_heads(
                reads.connection, publications,
            )
            assert not reads._verified_current_voucher_publications
            assert reads._verified_publication_ids == independent_records
    assert len(observed) == 1 and len(observed[0]) == 1


@pytest.mark.parametrize("boundary", ["expired", "historical", "replaced_token"])
def test_checked_model_fund_sources_cannot_cross_snapshot_boundary(published_sources, boundary):
    engine, rows = published_sources
    ident = rows["first-funding"]["id"]
    with QueryReads.snapshot(engine) as reads:
        fund_sources = []
        sources.verify_model_sources(
            engine, reads.connection, calculation_ids=[ident], _fund_sources=fund_sources,
            _reads=reads,
        )
        assert len(fund_sources) == 1
        assert fund_sources[0].raw_calculations([ident])[ident]["id"] == ident
        if boundary == "historical":
            with historical_content(1), pytest.raises(KernelError):
                fund_sources[0].raw_calculations([ident])
        elif boundary == "replaced_token":
            reads._snapshot_token = object()
            with pytest.raises(KernelError):
                fund_sources[0].raw_calculations([ident])
    if boundary == "expired":
        with pytest.raises(KernelError):
            fund_sources[0].raw_calculations([ident])


def test_later_close_coverage_never_creates_open_normal_relationship(header_engine, monkeypatch):
    engine = header_engine
    save(engine)
    publish(engine)
    close(engine, "2026-01")
    periods, proof = Periods(engine), evidence(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-02", category, evidence=[], expected=0, no_business=True,
            confirmation_evidence=proof, request_id="later-inventory-" + category,
        )
    preview = periods.preview_close("2026-02", owner_confirmation=proof)
    periods.close(
        "2026-02", owner_confirmation=proof, preview_digest=preview["digest"],
        epochs=preview["epochs"], request_id="later-close",
    )
    # Removing the individual close must not reinterpret the covered source
    # as open. The later authentic close remains; full integrity independently
    # rejects this missing frozen record, and is not replaced by model hits.
    damage(engine, "period_close", "DELETE FROM period_close WHERE period=?",
           (YearMonth("2026-01").ordinal,), foreign_keys=False)
    with engine.store.connection(read_only=True) as connection:
        ident = connection.execute("SELECT id FROM calculation").fetchone()[0]

    def read():
        with QueryReads.snapshot(engine) as reads:
            assert set(sources.verify_model_sources(
                engine, reads.connection, calculation_ids=[ident],
                through_period=YearMonth("2026-01").ordinal,
            )) == {ident}
            assert not reads._verified_open_voucher_scopes
            assert all(signature is None for signature, _ in
                       reads._verified_current_voucher_publications.values())
            return dict(reads._verified_current_voucher_publications)

    reused = read()
    with independent_headers(monkeypatch, "publication"):
        assert read() == reused
