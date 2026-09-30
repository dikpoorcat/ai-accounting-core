"""Real close-rooted, as-of period balance proof on synthetic companies."""

from dataclasses import replace

import pytest
from schema_fixture import test_bundle
from test_publication_periods import Position, calculate, close, publish, save

from ai_accounting.kernel.close_storage import derived_root, verified_header
from ai_accounting.kernel.contracts import KernelError, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.period_balance_freeze import (
    _require_prepared_consistency,
    compare_balance_freeze,
    prepare_balance_freeze,
    read_frozen_balances,
)
from ai_accounting.kernel.period_balances import balance_movements, balance_totals
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth


@pytest.fixture
def company(tmp_path):
    registry = Registry()
    registry.register(Position, calculate)
    engine = Engine(
        Store.create(tmp_path / "company.sqlite", test_bundle(registry), "company", "tax", "db")
    )
    proof = engine.register_evidence(b"synthetic", "text/plain", "proof", request_id="proof")[
        "digest"
    ]
    return engine, proof


def _prepared(connection, month, *, highwater=None, check_projection=True):
    period = YearMonth(month).ordinal
    close_row = connection.execute(
        "SELECT digest FROM period_close WHERE period=?", (period,)
    ).fetchone()
    if highwater is None:
        highwater = connection.execute(
            "SELECT max(sequence) FROM calculation_publication"
        ).fetchone()[0]
    return prepare_balance_freeze(
        connection, period, close_row["digest"], highwater,
        check_projection=check_projection,
    )


def _header(connection, prepared):
    row = connection.execute(
        "SELECT * FROM period_close WHERE period=?", (prepared.period,)
    ).fetchone()
    header = verified_header(connection, row)
    assert derived_root(header, "period_balance") == prepared.root_digest
    return header


def test_asof_close_root_preserves_activity_and_zero_key_after_later_correction(company):
    save(company, 10000)
    publish(company, "initial")
    close(company, "2026-01")
    with company[0].store.connection(read_only=True) as connection:
        january = _prepared(connection, "2026-01")
    save(company, 14000, 1, account="bank-b")
    preview, _ = publish(company, "correction", "2026-03")
    assert preview["results"][0]["mode"] == "closed_correction"
    close(company, "2026-03")
    with company[0].store.connection(read_only=True) as connection:
        march = _prepared(connection, "2026-03")
    with company[0].store.connection(read_only=True) as connection:
        assert read_frozen_balances(connection, _header(connection, january), "test_position") == [
            {"category": "test_position", "key": "bank-a", "ending": 10000,
             "activity": 10000, "ending_present": True, "activity_present": True}
        ]
        assert read_frozen_balances(connection, _header(connection, march), "test_position") == [
            {"category": "test_position", "key": "bank-a", "ending": 0,
             "activity": -10000, "ending_present": True, "activity_present": True},
            {"category": "test_position", "key": "bank-b", "ending": 14000,
             "activity": 14000, "ending_present": True, "activity_present": True},
        ]
        assert read_frozen_balances(
            connection, _header(connection, march), "test_position", keys={"bank-b"}
        ) == [
            {"category": "test_position", "key": "bank-b", "ending": 14000,
             "activity": 14000, "ending_present": True, "activity_present": True}
        ]
        replay = _prepared(
            connection, "2026-01", highwater=january.publication_highwater,
            check_projection=False,
        )
        assert replay.root_digest == january.root_digest


def test_bucket_tamper_and_missing_row_reject_without_scanning_other_bucket(company):
    save(company, 100, account="bank-a")
    publish(company, "initial")
    close(company, "2026-01")
    with company[0].store.connection() as connection:
        prepared = _prepared(connection, "2026-01")
        connection.execute(
            "UPDATE period_balance_freeze_row SET ending=999 WHERE period=?",
            (prepared.period,),
        )
        connection.commit()
    with company[0].store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as error:
            read_frozen_balances(
                connection, _header(connection, prepared), "test_position", {"bank-a"}
            )
        assert error.value.details["reason"] == "bucket_content_mismatch"
    with company[0].store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert compare_balance_freeze(connection, repair=True)["changed"]
        connection.commit()
    with company[0].store.connection(read_only=True) as connection:
        assert read_frozen_balances(
            connection, _header(connection, prepared), "test_position", {"bank-a"}
        )[0]["ending"] == 100


def test_later_no_impact_review_keeps_original_highwater_proof(company):
    save(company, 100, account="bank-a")
    publish(company, "initial")
    close(company, "2026-01")
    with company[0].store.connection(read_only=True) as connection:
        original = _prepared(connection, "2026-01")
    save(company, 100, revision=1, account="bank-a")
    preview, _ = publish(company, "review")
    assert preview["results"][0]["mode"] == "review_no_impact"
    with company[0].store.connection(read_only=True) as connection:
        reconstructed = _prepared(
            connection, "2026-01", highwater=original.publication_highwater,
            check_projection=False,
        )
        assert reconstructed.root_digest == original.root_digest
        assert reconstructed.rows == original.rows
        assert compare_balance_freeze(connection) == {"periods": 1, "changed": False}
        assert balance_totals(connection, original.period, "test_position") == [
            {"category": "test_position", "key": "bank-a", "amount": 100}
        ]


def test_prepared_rows_cannot_disagree_with_committed_bucket_directory(company):
    save(company, 100)
    publish(company, "initial")
    close(company, "2026-01")
    with company[0].store.connection(read_only=True) as connection:
        prepared = _prepared(connection, "2026-01")
    _require_prepared_consistency(prepared)
    wrong = replace(
        prepared,
        rows=tuple(
            (*row[:4], row[4] + 1, *row[5:]) for row in prepared.rows
        ),
    )
    with pytest.raises(KernelError) as error:
        _require_prepared_consistency(wrong)
    assert error.value.details["reason"] == "prepared_bucket_mismatch"


def test_public_balance_reads_reuse_frozen_baseline_and_check_only_open_tail(company, monkeypatch):
    from ai_accounting.kernel import period_balances

    save(company, 10000)
    publish(company, "initial")
    close(company, "2026-01")
    save(company, 14000, 1, account="bank-b")
    publish(company, "correction", "2026-03")
    called = []
    original = period_balances.verify_selected_balances

    def observe(connection, through, category=None, *, periods=None, reads=None):
        called.append(periods)
        assert periods is not None
        return original(connection, through, category, periods=periods, reads=reads)

    monkeypatch.setattr(period_balances, "verify_selected_balances", observe)
    march = YearMonth("2026-03").ordinal
    with company[0].store.connection(read_only=True) as connection:
        assert balance_totals(connection, march, "test_position") == [
            {"category": "test_position", "key": "bank-a", "amount": 0},
            {"category": "test_position", "key": "bank-b", "amount": 14000},
        ]
        assert balance_movements(connection, march, "test_position") == [
            {"category": "test_position", "key": "bank-a", "amount": -10000},
            {"category": "test_position", "key": "bank-b", "amount": 14000},
        ]
        assert balance_totals(connection, march, "test_position", keys={"bank-b"}) == [
            {"category": "test_position", "key": "bank-b", "amount": 14000}
        ]
    assert called and all(periods == {march} for periods in called)


def test_open_tail_balance_proof_reuses_only_exact_successful_snapshot_scope(
    company, monkeypatch
):
    from ai_accounting.kernel import period_balances

    save(company, 10000)
    publish(company, "initial")
    close(company, "2026-01")
    save(company, 14000, 1, account="bank-b")
    publish(company, "correction", "2026-03")
    engine = company[0]
    march = YearMonth("2026-03").ordinal
    calls = []
    original = period_balances.verify_selected_balances

    def counted(connection, through, category=None, *, periods=None, reads=None):
        calls.append((through, category, frozenset(periods) if periods is not None else None))
        return original(connection, through, category, periods=periods, reads=reads)

    monkeypatch.setattr(period_balances, "verify_selected_balances", counted)
    with QueryReads.snapshot(engine) as reads:
        assert balance_totals(reads.connection, march, "test_position", reads=reads) == [
            {"category": "test_position", "key": "bank-a", "amount": 0},
            {"category": "test_position", "key": "bank-b", "amount": 14000},
        ]
        assert balance_movements(reads.connection, march, "test_position", reads=reads) == [
            {"category": "test_position", "key": "bank-a", "amount": -10000},
            {"category": "test_position", "key": "bank-b", "amount": 14000},
        ]
        assert balance_totals(
            reads.connection, march, "test_position", keys={"bank-b"}, reads=reads
        ) == [{"category": "test_position", "key": "bank-b", "amount": 14000}]
        assert calls == [(march, "test_position", frozenset({march}))]
        balance_movements(reads.connection, march, "bank", reads=reads)
        assert calls[-1] == (march, "bank", frozenset({march}))
        with engine.store.connection(read_only=True) as other_connection:
            balance_movements(other_connection, march, "test_position", reads=reads)
            balance_movements(other_connection, march, "test_position", reads=reads)
        assert calls.count((march, "test_position", frozenset({march}))) == 3
    with QueryReads.snapshot(engine) as reads:
        balance_totals(reads.connection, march, "test_position", reads=reads)
    assert calls.count((march, "test_position", frozenset({march}))) == 4
    with engine.store.connection(read_only=True) as connection:
        balance_totals(connection, march, "test_position")
        balance_movements(connection, march, "test_position")
    assert calls.count((march, "test_position", frozenset({march}))) == 6


def test_bad_open_tail_balance_never_enters_successful_scope_cache(company, monkeypatch):
    from ai_accounting.kernel import period_balances

    save(company, 10000)
    publish(company, "initial")
    close(company, "2026-01")
    save(company, 14000, 1, account="bank-b")
    publish(company, "correction", "2026-03")
    engine = company[0]
    march = YearMonth("2026-03").ordinal
    with engine.store.connection() as connection:
        connection.execute(
            "UPDATE period_balance SET amount=amount+1 WHERE posting_period=? "
            "AND balance_key='bank-b'",
            (march,),
        )
    calls = []
    original = period_balances.verify_selected_balances

    def counted(connection, through, category=None, *, periods=None, reads=None):
        calls.append(frozenset(periods) if periods is not None else None)
        return original(connection, through, category, periods=periods, reads=reads)

    monkeypatch.setattr(period_balances, "verify_selected_balances", counted)
    with QueryReads.snapshot(engine) as reads:
        for read in (balance_totals, balance_movements):
            with pytest.raises(KernelError) as error:
                read(reads.connection, march, "test_position", reads=reads)
            assert error.value.details["reason"] == "content_checksum"
    assert calls == [frozenset({march}), frozenset({march})]


def test_full_compare_replays_publications_once_and_decodes_each_source_once(company):
    save(company, 10000)
    publish(company, "initial")
    close(company, "2026-01")
    save(company, 14000, 1, account="bank-b")
    publish(company, "correction", "2026-03")
    close(company, "2026-03")
    traced = []
    with company[0].store.connection(read_only=True) as connection:
        connection.set_trace_callback(traced.append)
        try:
            assert compare_balance_freeze(connection) == {"periods": 2, "changed": False}
        finally:
            connection.set_trace_callback(None)
    publication_reads = [
        sql for sql in traced
        if sql.startswith("SELECT * FROM calculation_publication")
    ]
    calculation_reads = [
        sql for sql in traced
        if sql.startswith("SELECT id,outcome,digest FROM calculation WHERE id IN")
    ]
    assert publication_reads == ["SELECT * FROM calculation_publication ORDER BY sequence"]
    assert len(calculation_reads) == 2
    assert all("WHERE sequence<=" not in sql for sql in traced)


def test_reused_full_close_set_rejects_missing_and_duplicate_months(company, monkeypatch):
    from ai_accounting.kernel import close_storage
    from ai_accounting.kernel.report_projection import compare_report_projection
    from ai_accounting.kernel.report_semantics import compare_report_semantics

    save(company, 100)
    publish(company, "initial")
    close(company, "2026-01")
    with company[0].store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT * FROM period_close").fetchone()
        verified = (row, close_storage.decode_close(connection, row))
        for compare in (compare_report_projection, compare_report_semantics):
            with pytest.raises(KernelError):
                compare(company[0], connection, _verified_closes=())
            with pytest.raises(KernelError):
                compare(company[0], connection, _verified_closes=(verified, verified))
        def forbidden(*_args, **_kwargs):
            raise AssertionError("same-snapshot report verification reread a full close")

        monkeypatch.setattr(close_storage, "decode_close", forbidden)
        for compare in (compare_report_projection, compare_report_semantics):
            assert not compare(company[0], connection, _verified_closes=(verified,))["changed"]
