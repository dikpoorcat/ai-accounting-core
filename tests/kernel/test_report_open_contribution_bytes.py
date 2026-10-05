"""A saved open-report body may authenticate source bytes without decoding them."""

import pytest
from test_integrity_content import damage
from test_payroll_corrections import company as _payroll_company
from test_payroll_corrections import payment as payroll_payment
from test_reports import book as book  # noqa: F401
from test_reports import scenario
from test_requested_cache_work import RequestedCache

from ai_accounting.kernel import integrity, storage
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_open_contribution import (
    _SOURCE_PROOF_SEAL,
    _AnchoredSourceProof,
    read_open_contributions,
)
from ai_accounting.kernel.reports import Reports

payroll_company = _payroll_company


def test_selected_contribution_cache_work_ignores_unrelated_saved_bodies(book):
    from collections import Counter

    engine = book[0]
    scenario(book)
    with QueryReads.snapshot(engine) as reads:
        ids = _publication_ids(reads.connection)
        expected = read_open_contributions(engine, reads.connection, ids, reads=reads)
        assert expected
        selected = tuple(expected)[:2]
        assert len(selected) == 2
        requested = set(selected)
        visits = []
        for size in (12, 48, 120, 2000):
            calls = Counter()
            reads._report_snapshot_cache["report_open_contributions"] = RequestedCache(
                {**{f"unrelated-{i}": object() for i in range(size)},
                 selected[0]: expected[selected[0]]},
                calls, "contributions",
            )
            actual = read_open_contributions(engine, reads.connection, requested, reads=reads)
            assert actual == {ident: expected[ident] for ident in requested}
            visits.append(calls)
        assert all(value == visits[0] for value in visits)


def _publication_ids(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT calculation_id FROM calculation_publication WHERE calculation_id IS NOT NULL"
        )
    }


def _payment_id(connection):
    return connection.execute(
        "SELECT calculation_id FROM calculation_publication WHERE subject_id='payment'"
    ).fetchone()[0]


def test_anchored_canonical_bytes_skip_unused_result_and_fact_decoding(book, monkeypatch):
    engine = book[0]
    scenario(book)
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(ValueError, match="proof"):
            reads._verify_anchored_source_bytes({"claimed_verified": True})
        ids = _publication_ids(reads.connection)
        fact_ids = {
            row[0]
            for row in reads.connection.execute(
                "SELECT fact_id FROM calculation WHERE id IN (SELECT value FROM json_each(?))",
                (storage.canonical(sorted(ids)),),
            )
        }
        reads.fact_versions(fact_ids)

        def unused(*_args, **_kwargs):
            raise AssertionError("canonical saved bytes should not be decoded a second time")

        monkeypatch.setattr(integrity, "_object", unused)
        monkeypatch.setattr(storage, "_snapshot_fact_raws", unused)
        assert ids <= read_open_contributions(engine, reads.connection, ids, reads=reads).keys()


def test_anchored_scalar_fact_uses_saved_json_without_python_hydration(book, monkeypatch):
    engine = book[0]
    scenario(book)
    with QueryReads.snapshot(engine) as reads:
        payment = _payment_id(reads.connection)

        def unnecessary(*_args, **_kwargs):
            raise AssertionError("a matching scalar fact should not be decoded in Python")

        monkeypatch.setattr(engine.store, "fact_data_many", unnecessary)
        assert payment in read_open_contributions(
            engine, reads.connection, {payment}, reads=reads
        )
        assert reads._verified_source_contents == {}


def test_two_publication_roots_reuse_exact_payroll_byte_proof(payroll_company, monkeypatch):
    company = payroll_company
    company.save(payroll_payment(), "payment")
    company.publish("january", "february", "payment")
    engine = company.engine
    january = company.current("january").id
    february = company.current("february").id
    payment = company.current("payment", "payment").id
    hashed_facts = []
    original_hashes = storage._scalar_fact_hashes

    def recorded_hashes(store, connection, kinds):
        hashed_facts.append({ident for identifiers in kinds.values() for ident in identifiers})
        return original_hashes(store, connection, kinds)

    monkeypatch.setattr(storage, "_scalar_fact_hashes", recorded_hashes)
    with QueryReads.snapshot(engine) as reads:
        january_fact = reads.connection.execute(
            "SELECT fact_id FROM calculation WHERE id=?", (january,)
        ).fetchone()[0]
        february_fact = reads.connection.execute(
            "SELECT fact_id FROM calculation WHERE id=?", (february,)
        ).fetchone()[0]
        first = read_open_contributions(engine, reads.connection, {payment}, reads=reads)
        assert january in {item[0] for item in first[payment]["source_bindings"]}
        recorded = dict(reads._anchored_source_bytes)
        assert january in recorded
        assert reads._verified_source_contents == {}
        before_second = len(hashed_facts)
        second = read_open_contributions(
            engine, reads.connection, {january, february}, reads=reads
        )
        second_hashes = set().union(*hashed_facts[before_second:])
        assert january_fact not in second_hashes
        assert february_fact in second_hashes
        assert january in {item[0] for item in second[january]["source_bindings"]}
        assert reads._anchored_source_bytes[january] == recorded[january]
        assert february in reads._anchored_source_bytes
        assert reads._verified_source_contents == {}
    assert reads._anchored_source_bytes == {}
    with QueryReads.snapshot(engine) as reads:
        assert read_open_contributions(
            engine, reads.connection, {january, february}, reads=reads
        ) == second
        assert january in reads._anchored_source_bytes


def test_open_report_response_matches_without_anchored_byte_reuse(book, monkeypatch):
    engine = book[0]
    scenario(book)
    report = Reports(engine)
    with QueryReads.snapshot(engine) as reads:
        cached = report._report(2026, 1, source="open", connection=reads.connection, reads=reads)

    original = QueryReads._verify_anchored_source_bytes

    def without_reuse(reads, proof):
        reads._anchored_source_bytes.clear()
        return original(reads, proof)

    with monkeypatch.context() as patcher:
        patcher.setattr(QueryReads, "_verify_anchored_source_bytes", without_reuse)
        with QueryReads.snapshot(engine) as reads:
            uncached = report._report(
                2026, 1, source="open", connection=reads.connection, reads=reads
            )
    assert cached == uncached


def test_unrelated_fact_revisions_do_not_expand_anchored_source_load(book, monkeypatch):
    engine, save, _, _ = book
    scenario(book)
    hashed = []
    original = storage._scalar_fact_hashes

    def recorded_hashes(store, connection, kinds):
        hashed.append({ident for values in kinds.values() for ident in values})
        return original(store, connection, kinds)

    monkeypatch.setattr(storage, "_scalar_fact_hashes", recorded_hashes)

    def selected_work():
        hashed.clear()
        with QueryReads.snapshot(engine) as reads:
            payment = _payment_id(reads.connection)
            assert payment in read_open_contributions(
                engine, reads.connection, {payment}, reads=reads
            )
            return set().union(*hashed), set(reads._anchored_source_bytes)

    before = selected_work()
    for index in range(40):
        save(
            "report_profile",
            f"unrelated-profile-{index}",
            {
                "period": "2026-01",
                "company_name": f"无关企业-{index}",
                "accounting_standard": "small_enterprise",
                "bookkeeping_start": "2026-01",
                "newly_established_zero_opening_confirmed": True,
            },
        )
    assert selected_work() == before


def test_conflicting_anchored_binding_is_rejected_without_caching(book):
    engine = book[0]
    scenario(book)
    with QueryReads.snapshot(engine) as reads:
        payment = _payment_id(reads.connection)
        read_open_contributions(engine, reads.connection, {payment}, reads=reads)
        original = reads._anchored_source_bytes[payment]
        proof = _AnchoredSourceProof(
            _SOURCE_PROOF_SEAL,
            reads.connection,
            reads,
            ((payment, "0" * 64, original[2], original[3]),),
        )
        before = dict(reads._anchored_source_bytes)
        with pytest.raises(KernelError, match="绑定"):
            reads._verify_anchored_source_bytes(proof)
        assert reads._anchored_source_bytes == before


def test_failed_multi_source_batch_does_not_cache_partial_byte_proof(book):
    engine = book[0]
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        payment = _payment_id(connection)
        expense = connection.execute(
            "SELECT calculation_id FROM calculation_publication WHERE subject_id='cost'"
        ).fetchone()[0]
    damage(engine, "calculation", "UPDATE calculation SET outcome='{}' WHERE id=?", (expense,))
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            read_open_contributions(engine, reads.connection, {payment, expense}, reads=reads)
        assert reads._anchored_source_bytes == {}
        assert not reads._report_snapshot_cache.get("report_open_contributions")


def test_anchored_scalar_hash_mismatch_rechecks_original_fact(book, monkeypatch):
    engine = book[0]
    scenario(book)
    with QueryReads.snapshot(engine) as reads:
        payment = _payment_id(reads.connection)
        fact_id = reads.connection.execute(
            "SELECT fact_id FROM calculation WHERE id=?", (payment,)
        ).fetchone()[0]
        calls = []
        original = engine.store.fact_data_many

        def counted(connection, identifiers):
            calls.extend(identifiers)
            return original(connection, identifiers)

        monkeypatch.setattr(engine.store, "fact_data_many", counted)
        monkeypatch.setattr(
            storage,
            "_scalar_fact_hashes",
            lambda _store, _connection, _kinds: {fact_id: bytes(32)},
        )
        assert payment in read_open_contributions(
            engine, reads.connection, {payment}, reads=reads
        )
        assert fact_id in calls


def test_equivalent_noncanonical_outcome_uses_original_decoder(book, monkeypatch):
    engine = book[0]
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        payment = _payment_id(connection)
    damage(
        engine, "calculation", "UPDATE calculation SET outcome=' '||outcome WHERE id=?", (payment,)
    )
    decoded = []
    original = integrity._object

    def counted(*args, **kwargs):
        decoded.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(integrity, "_object", counted)
    with QueryReads.snapshot(engine) as reads:
        result = read_open_contributions(engine, reads.connection, {payment}, reads=reads)
        assert payment in result
    assert payment in decoded


def test_equivalent_noncanonical_fact_bytes_use_fallback_without_false_rejection(book):
    engine = book[0]
    scenario(book)
    with QueryReads.snapshot(engine) as reads:
        payment = _payment_id(reads.connection)
        fact_id = reads.connection.execute(
            "SELECT fact_id FROM calculation WHERE id=?", (payment,)
        ).fetchone()[0]
        reads.fact_versions({fact_id})
        reads._raw_fact_data[fact_id] = b" " + reads._raw_fact_data[fact_id]
        assert payment in read_open_contributions(
            engine, reads.connection, {payment}, reads=reads
        )


@pytest.mark.parametrize("case", ["outcome", "header", "fact", "seal", "dependency"])
def test_anchored_source_rejects_changed_authority(book, case):
    engine = book[0]
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        payment = _payment_id(connection)
    if case == "outcome":
        damage(engine, "calculation", "UPDATE calculation SET outcome='{}' WHERE id=?", (payment,))
    elif case == "header":
        damage(
            engine, "calculation", "UPDATE calculation SET period=period+1 WHERE id=?", (payment,)
        )
    elif case == "fact":
        damage(
            engine,
            "fact_cash_payment",
            "UPDATE fact_cash_payment SET amount_fen=amount_fen+1 WHERE revision_id="
            "(SELECT fact_id FROM calculation WHERE id=?)",
            (payment,),
        )
    elif case == "seal":
        damage(
            engine,
            "calculation_seal",
            "DELETE FROM calculation_seal WHERE calculation_id=?",
            (payment,),
            foreign_keys=False,
        )
    else:
        damage(
            engine,
            "dependency_calculation",
            "DELETE FROM dependency_calculation WHERE calculation_id=?",
            (payment,),
        )
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            read_open_contributions(engine, reads.connection, {payment}, reads=reads)
        assert not reads._report_snapshot_cache.get("report_open_contributions")


def test_success_and_failure_never_cross_read_snapshots(book):
    engine = book[0]
    scenario(book)
    with QueryReads.snapshot(engine) as reads:
        payment = _payment_id(reads.connection)
        assert payment in read_open_contributions(engine, reads.connection, {payment}, reads=reads)
        assert payment in reads._anchored_source_bytes
    assert reads._anchored_source_bytes == {}
    with engine.store.connection(read_only=True) as connection:
        original = connection.execute(
            "SELECT outcome FROM calculation WHERE id=?", (payment,)
        ).fetchone()[0]
    damage(engine, "calculation", "UPDATE calculation SET outcome='{}' WHERE id=?", (payment,))
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            read_open_contributions(engine, reads.connection, {payment}, reads=reads)
        assert not reads._report_snapshot_cache.get("report_open_contributions")
        assert reads._anchored_source_bytes == {}
    damage(
        engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?", (original, payment)
    )
    with QueryReads.snapshot(engine) as reads:
        assert payment in read_open_contributions(engine, reads.connection, {payment}, reads=reads)
        assert payment in reads._anchored_source_bytes
