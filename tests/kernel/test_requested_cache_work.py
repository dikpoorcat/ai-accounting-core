"""Requested cache lookups retain real source proofs without scanning other keys."""

from collections import Counter

import pytest
from test_dashboard_metadata import detached_snapshot, profile
from test_engine import engine as engine_fixture
from test_engine import publish, save
from test_full_accounting_period_scopes import series

from ai_accounting.kernel import read_indexes
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads, verify_current_voucher_publications

engine = engine_fixture


class RequestedCache(dict):
    """Count actual visits and reject copying/iterating an accumulated cache."""

    def __init__(self, values, calls, name):
        super().__init__(values)
        self.calls, self.name = calls, name

    def keys(self):
        raise AssertionError("requested lookup scanned cache keys")

    def __iter__(self):
        raise AssertionError("requested lookup iterated cache")

    def __or__(self, other):
        raise AssertionError("requested lookup copied cache")

    def __contains__(self, key):
        self.calls[self.name, "contains"] += 1
        return super().__contains__(key)

    def __getitem__(self, key):
        self.calls[self.name, "getitem"] += 1
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.calls[self.name, "get"] += 1
        return super().get(key, default)


def reference(reads, month):
    return dict(reads.connection.execute(
        "SELECT * FROM close_reference WHERE close_period=? AND path=? LIMIT 1",
        (month, read_indexes.CLOSE_VOUCHERS),
    ).fetchone())


def test_requested_close_work_is_independent_of_unrelated_cache_growth(engine):
    months = tuple(series(engine, months=2))
    visits = []
    for size in (12, 48, 120, 2000):
        with QueryReads.snapshot(engine) as reads:
            rows = reads.authoritative_close_rows(periods=months)
            reads.close_rows(periods=(months[0],))
            leaf = reference(reads, months[0])
            padding = {months[0] - 1000 - i: object() for i in range(size)}
            calls = Counter()
            reads._closes = RequestedCache({**padding, **reads._closes}, calls, "full")
            reads._authoritative_closes = RequestedCache(
                {**padding, **reads._authoritative_closes}, calls, "authority"
            )
            reads._close_manifests = RequestedCache(
                {**padding, **reads._close_manifests}, calls, "manifest"
            )
            actual = reads.authoritative_close_rows(periods=months)
            reads.verify_close_references([leaf])
            assert all(
                observed is expected for observed, expected in zip(actual, rows, strict=True)
            )
            visits.append(calls)
    assert all(value == visits[0] for value in visits)
    assert visits[0]["manifest", "getitem"] == 1


def test_requested_close_proofs_preserve_failure_type_and_authority_boundaries(engine, monkeypatch):
    months = tuple(series(engine, months=2))
    with QueryReads.snapshot(engine) as reads:
        authority = reads.authoritative_close_rows(periods=months)
        assert not reads._closes and not reads._close_manifests
        leaves = [reference(reads, month) for month in months]
        calls = []
        original = read_indexes.verify_close_references

        def capture(connection, values, **options):
            calls.append((len(values), set(options["_verified_manifests"])))
            return original(connection, values, **options)

        monkeypatch.setattr(read_indexes, "verify_close_references", capture)
        reads.verify_close_references([leaves[0], leaves[0]])
        successful = set(reads._verified_close_references)
        parts = dict(reads._verified_close_storage_parts)
        reads.verify_close_references([leaves[0]])
        assert calls == [(1, set())]
        bad = {**leaves[1], "reference_id": "wrong-immutable-leaf"}
        for _ in range(2):
            with pytest.raises(KernelError):
                reads.verify_close_references([leaves[1], bad])
            assert reads._verified_close_references == successful
            assert reads._verified_close_storage_parts == parts
        assert len(calls) == 3
        with pytest.raises(KernelError):
            reads.verify_close_references([{**leaves[0], "position": False}])
        assert reads._verified_close_references == successful
        assert not reads._closes and not reads._close_manifests
        source_calls = []
        original_sources = read_indexes.verify_sources

        def sources(connection, kind, rows):
            source_calls.append((kind, len(rows)))
            return original_sources(connection, kind, rows)

        monkeypatch.setattr(read_indexes, "verify_sources", sources)
        reads.close_rows(periods=(months[0],))
        assert source_calls == [("close", 1)]
        reads.verify_close_references([leaves[1]])
        assert calls[-1] == (1, set())  # A different month's full proof is irrelevant.
        weak = dict(authority[0])
        reads._authoritative_closes[months[0]] = weak
        assert reads.authoritative_close_rows(periods=(months[0],))[0] is weak
        assert reads.authoritative_close_rows(periods=months, through_period=months[0]) == [weak]
        assert reads.authoritative_close_rows(periods=()) == []
        before = dict(reads._authoritative_closes), dict(reads._close_headers)

        def fault(*args, **kwargs):
            raise RuntimeError("authority fault")

        monkeypatch.setattr(read_indexes, "authoritative_close_rows", fault)
        with pytest.raises(RuntimeError, match="authority fault"):
            reads.authoritative_close_rows(periods=(months[-1] + 1,))
        assert (reads._authoritative_closes, reads._close_headers) == before
    assert not reads._authoritative_closes and not reads._verified_close_references


def test_profile_cache_positive_and_negative_reads_stay_bounded(engine):
    profile(engine, "asset", "requested", display_name="Requested asset")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        snapshot = detached_snapshot(engine, connection)
        records = snapshot.profiles["asset"]
        records.prime(("requested", "absent"))
        expected = records.cache["requested"]
        visits = []
        for size in (12, 48, 120, 2000):
            calls = Counter()
            records.cache = RequestedCache(
                {"requested": expected, **{f"other-{i}": object() for i in range(size)}},
                calls, "profiles",
            )
            records.prime(("requested", "absent"))
            records.prime((f"new-absent-{size}",))
            assert f"new-absent-{size}" in records.missing
            assert records["requested"] == expected
            with pytest.raises(KeyError):
                records["absent"]
            visits.append(calls)
        assert all(value == visits[0] for value in visits)


def test_publication_cache_hits_keep_exact_results_with_growing_unrelated_cache(engine):
    save(engine)
    publish(engine)
    visits = []
    for size in (12, 48, 120, 2000):
        with QueryReads.snapshot(engine) as reads:
            voucher = reads.connection.execute(
                "SELECT id FROM voucher_version LIMIT 1"
            ).fetchone()[0]
            period = reads.connection.execute(
                "SELECT posting_period FROM calculation_publication LIMIT 1"
            ).fetchone()[0]
            expected = verify_current_voucher_publications(reads.connection, {voucher})
            reads.verify_publication_periods({period})
            calls = Counter()
            reads._verified_current_voucher_publications = RequestedCache(
                {**{f"unrelated-{i}": object() for i in range(size)},
                 **reads._verified_current_voucher_publications}, calls, "vouchers"
            )
            reads._verified_publications = RequestedCache(
                {**{period - 1000 - i: object() for i in range(size)},
                 **reads._verified_publications}, calls, "publications"
            )
            assert verify_current_voucher_publications(reads.connection, {voucher}) == expected
            reads.verify_publication_periods({period})
            visits.append(calls)
    assert all(value == visits[0] for value in visits)
