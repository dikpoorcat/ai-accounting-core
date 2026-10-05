"""Cross-period physical reads preserve the existing leaf and authority proofs."""

import hashlib
import sqlite3
import weakref
from collections import ChainMap, Counter
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from stage9_metrics import measure_work
from test_close_accounting_filter import _closed_charge
from test_employee_adopted_period_scopes import prepare
from test_engine import engine as engine_fixture
from test_integrity_content import damage

from ai_accounting.kernel import close_storage
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import canonical

engine = engine_fixture


class PhysicalStore:
    def __init__(self, connection):
        self.raw = connection

    @contextmanager
    def connection(self, *, read_only=False):
        yield self.raw


def _sha(value):
    return hashlib.sha256(value.encode()).digest()


@pytest.fixture
def physical():
    """Physical-only synthetic roots, independent of business adoption claims."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE period_close(period INTEGER PRIMARY KEY)")
    connection.executescript(close_storage.CLOSE_STORAGE_DDL)
    headers = []
    for period in range(120):
        connection.execute("INSERT INTO period_close VALUES(?)", (period,))
        directories = {}
        for field in (*close_storage.ACCOUNTING_FIELDS, "readiness:financial_reports",
                      "readiness:unrelated", "management_snapshot"):
            descriptors = []
            buckets = (0,) if field.startswith("readiness:") or field == "management_snapshot" \
                else (3, 91, 203)
            for bucket in buckets:
                # Different block parts must remain visible to the count and
                # exact part checks, including a later requested period.
                payloads = (
                    [[[0, {"period": period, "field": field}]]]
                    if buckets == (0,) else
                    [[[bucket * 4 + part, {"period": period, "part": part}]]
                     for part in range(2)]
                )
                block_descriptors = []
                for part, payload in enumerate(payloads):
                    body = canonical(payload)
                    block_descriptors.append([part, _sha(body).hex(), len(payload)])
                    connection.execute(
                        "INSERT INTO close_storage_block VALUES(?,?,?,?,?,?)",
                        (period, field, bucket, part, body, _sha(body)),
                    )
                body = canonical(block_descriptors)
                connection.execute(
                    "INSERT INTO close_storage_directory VALUES(?,?,?,?,?)",
                    (period, field, bucket, body, _sha(body)),
                )
                descriptors.append([bucket, _sha(body).hex(), sum(len(p) for p in payloads)])
            directories[field] = descriptors
        roots = {}
        for family, fields in (
            ("accounting", close_storage.ACCOUNTING_FIELDS),
            ("management", ("readiness:financial_reports", "readiness:unrelated",
                            "management_snapshot")),
        ):
            body = canonical({"directories": {field: directories[field] for field in fields}})
            connection.execute(
                "INSERT INTO close_storage_subroot VALUES(?,?,?,?)",
                (period, family, body, _sha(body)),
            )
            roots[family] = _sha(body).hex()
        headers.append(close_storage.CloseHeader(period, b"l" * 32, b"s" * 32,
                                                {"subroots": roots}))
    yield SimpleNamespace(store=PhysicalStore(connection)), headers
    connection.close()


def _physical_accounting(connection, headers, *, batch):
    parts = {}
    subroots = (
        close_storage._families_many(connection, headers, "accounting") if batch else
        tuple(close_storage._family(connection, header, "accounting") for header in headers)
    )
    groups = [
        (header, subroot, field, {3, 91, 203, 250})
        for header, subroot in zip(headers, subroots, strict=True)
        for field in close_storage.ACCOUNTING_FIELDS
    ]
    if batch:
        close_storage._prime_buckets_many(connection, groups, parts)
    else:
        for header, subroot, field, buckets in groups:
            close_storage._prime_buckets(connection, header, subroot, field, buckets, parts)
    return parts


@pytest.mark.parametrize("months", [12, 48])
def test_cross_period_work_returns_same_selected_content(physical, months, record_property):
    synthetic, headers = physical

    def read(batch):
        with synthetic.store.connection(read_only=True) as connection:
            return _physical_accounting(connection, headers[:months], batch=batch)

    old_work, expected = measure_work(synthetic, lambda: read(False))
    new_work, actual = measure_work(synthetic, lambda: read(True))
    assert actual == expected
    old, new = old_work["counters"], new_work["counters"]
    record_property("physical_work", {"months": months, "old": old, "batch": new})
    assert old["sql_calls"] == 5 * months
    assert new["sql_calls"] == 3
    assert new["returned_rows"] == old["returned_rows"]
    assert new["returned_value_bytes"] <= old["returned_value_bytes"]
    assert new["stdlib_json_input_bytes"] == old["stdlib_json_input_bytes"]
    assert new["stdlib_json_loads"] == old["stdlib_json_loads"]
    # Record actual VM counts; no unrequested month may turn this into a scan.
    assert new["sqlite_vm_steps"] <= old["sqlite_vm_steps"] * 2


@pytest.mark.parametrize("adopted_only", [False, True])
def test_owned_batch_matches_original_with_exact_and_empty_scopes(
    engine, monkeypatch, adopted_only
):
    scopes = prepare(engine)
    subjects = set().union(*scopes.values())

    def read():
        with QueryReads.snapshot(engine) as reads:
            rows = reads.authoritative_close_rows(periods=scopes)
            method = (
                reads.close_adopted_results_many if adopted_only else reads.close_accounting_many
            )
            result = method(rows, subjects=subjects, subjects_by_period=scopes)
            assert not any(key[2] == "accounting_subject_buckets"
                           for key in reads._verified_close_storage_parts)
            assert method(rows, subjects=subjects, subjects_by_period=scopes) == result
            return result

    actual = read()
    with monkeypatch.context() as patch:
        patch.setattr(close_storage, "_owned_header_snapshot", lambda connection: False)
        assert read() == actual
    assert [len(part.adopted_results) for part in actual] == [1, 1, 0]


@pytest.mark.parametrize("table", [
    "close_storage_subroot", "close_storage_directory", "close_storage_block",
])
def test_failed_group_does_not_publish_physical_or_filter_prefix(engine, table):
    scopes = prepare(engine)
    period = sorted(scopes)[1]
    where = "family='accounting'" if table == "close_storage_subroot" else "field='adopted_results'"
    damage(engine, table, f"UPDATE {table} SET content='[]' WHERE period=? AND {where}", (period,))
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=scopes)
        known = dict(reads._verified_close_storage_parts)
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                reads.close_accounting_many(rows, subjects=set().union(*scopes.values()))
            assert failure.value.code == "content_integrity_failed"
            assert reads._verified_close_storage_parts == known
            assert not reads._close_accounting_positions
            assert not reads._close_accounting_slices


def test_bucket_validator_fault_remains_in_group_and_no_prefix(engine, monkeypatch):
    scopes = prepare(engine)
    original = close_storage._bucket_rows

    def fail_last(connection, header, subroot, field, bucket, **options):
        if header.period == sorted(scopes)[1] and field == "vouchers":
            raise RuntimeError("synthetic later voucher block failure")
        return original(connection, header, subroot, field, bucket, **options)

    monkeypatch.setattr(close_storage, "_bucket_rows", fail_last)
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=scopes)
        with pytest.raises(RuntimeError, match="synthetic later voucher block failure"):
            reads.close_accounting_many(rows, subjects=set().union(*scopes.values()))
        assert not reads._verified_close_storage_parts
        assert not reads._close_accounting_slices
        assert not reads._close_accounting_positions


@pytest.mark.parametrize("mode", ["single", "unowned", "v1"])
def test_only_owned_current_multiple_headers_prime(engine, monkeypatch, mode):
    scopes = prepare(engine)

    def forbidden(*args, **kwargs):
        raise AssertionError("noneligible physical batch")

    monkeypatch.setattr(close_storage, "_prime_accounting_many", forbidden)
    monkeypatch.setattr(close_storage, "_families_many", forbidden)
    if mode == "unowned":
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            reads = QueryReads(engine, connection)
            rows = reads.authoritative_close_rows(periods=scopes)
            assert reads.close_accounting_many(rows, subjects={"included"})
            headers = tuple(reads.close_header(row) for row in rows)
            assert close_storage.read_readiness_checks_many(connection, headers, "absent") == (
                {}, {}, {}
            )
        return
    with QueryReads.snapshot(engine) as reads:
        if mode == "v1":
            with historical_content(1):
                rows = reads.authoritative_close_rows(periods=scopes)
                assert reads.close_accounting_many(rows, subjects={"included"})
                headers = tuple(reads.close_header(row) for row in rows)
                assert close_storage.read_readiness_checks_many(
                    reads.connection, headers, "absent"
                ) == ({}, {}, {})
        else:
            rows = reads.authoritative_close_rows(periods=[min(scopes)])
            assert reads.close_accounting_many(rows, subjects={"included"})
            assert close_storage.read_readiness_checks_many(
                reads.connection, [reads.close_header(rows[0])], "absent"
            ) == ({},)


def test_readiness_batch_only_loads_chosen_checker(physical, monkeypatch):
    synthetic, headers = physical
    connection = synthetic.store.raw
    # Physical-only roots test structure/leaf equivalence. Actual ownership is
    # checked separately above with the managed QueryReads snapshot.
    monkeypatch.setattr(close_storage, "_owned_header_snapshot", lambda connection: True)
    wanted = headers[:3]
    expected = tuple(close_storage.read_readiness_check(connection, header, "financial_reports")
                     for header in wanted)
    statements = []
    connection.set_trace_callback(statements.append)
    assert close_storage.read_readiness_checks_many(
        connection, wanted, "financial_reports"
    ) == expected
    assert len(statements) == 3
    assert not any(
        "readiness:unrelated" in sql or "management_snapshot" in sql for sql in statements
    )
    statements.clear()
    assert close_storage.read_readiness_checks_many(connection, wanted, "absent") == ({}, {}, {})
    assert len(statements) == 1
    connection.set_trace_callback(None)
    for _ in range(2):
        connection.execute(
            "UPDATE close_storage_block SET content='[]' "
            "WHERE period=2 AND field='readiness:financial_reports'"
        )
        with pytest.raises(KernelError) as failure:
            close_storage.read_readiness_checks_many(connection, wanted, "financial_reports")
        assert failure.value.code == "content_integrity_failed"


def test_header_success_reuses_only_the_exact_row_and_snapshot(engine):
    period = _closed_charge(engine)
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[period])[0]
        cached = reads._close_headers[period]
        statements = []
        reads.connection.set_trace_callback(statements.append)
        assert close_storage.verified_header(reads.connection, row) is cached
        assert not statements
        assert close_storage.verified_header(
            reads.connection, row, require_marker=False
        ) == cached
        assert len(statements) == 2
        assert not any("read_index_source" in sql for sql in statements)
        statements.clear()
        with historical_content(1):
            assert close_storage.verified_header(reads.connection, row) == cached
        assert len(statements) == 3
        with engine.store.connection(read_only=True) as other:
            other.execute("BEGIN")
            other_statements = []
            other.set_trace_callback(other_statements.append)
            assert close_storage.verified_header(other, row) == cached
            assert len(other_statements) == 3
    with QueryReads.snapshot(engine) as reads:
        assert not reads._close_headers
        statements = []
        reads.connection.set_trace_callback(statements.append)
        assert close_storage.verified_header(reads.connection, row) == cached
        assert len(statements) == 3
        # This helper consumes an existing proof but never publishes one.
        assert not reads._close_headers


@pytest.mark.parametrize("change", ["manifest", "digest", "period"])
def test_different_row_cannot_borrow_cached_header(engine, change):
    period = _closed_charge(engine)
    with QueryReads.snapshot(engine) as reads:
        row = dict(reads.authoritative_close_rows(periods=[period])[0])
        previous = dict(reads._close_headers)
        row[change] = {"manifest": row["manifest"] + " ",
                       "digest": b"x" * 32, "period": period + 1}[change]
        statements = []
        reads.connection.set_trace_callback(statements.append)
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                close_storage.verified_header(reads.connection, row)
            assert failure.value.code == "content_integrity_failed"
            assert reads._close_headers == previous
        assert len(statements) == 6


@pytest.mark.parametrize("change", ["wrong_type", "wrong_period", "wrong_digest"])
def test_inexact_cached_header_does_not_replace_original_validation(engine, change):
    period = _closed_charge(engine)
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[period])[0]
        expected = reads._close_headers[period]
        if change == "wrong_type":
            reads._close_headers[period] = object()
        else:
            reads._close_headers[period] = close_storage.CloseHeader(
                period + (change == "wrong_period"), expected.logical_digest,
                b"x" * 32 if change == "wrong_digest" else expected.storage_digest,
                expected.root,
            )
        previous = reads._close_headers[period]
        statements = []
        reads.connection.set_trace_callback(statements.append)
        assert close_storage.verified_header(reads.connection, row) == expected
        assert len(statements) == 3
        assert reads._close_headers[period] is previous


def test_header_new_snapshot_rejects_missing_marker_without_success_cache(engine):
    period = _closed_charge(engine)
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[period])[0]
        assert close_storage.verified_header(reads.connection, row) is reads._close_headers[period]
    damage(engine, "read_index_source",
           "DELETE FROM read_index_source WHERE source_kind='close' AND source_id=?",
           (str(period),), foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                close_storage.verified_header(reads.connection, row)
            assert failure.value.code == "content_integrity_failed"
            assert failure.value.details["reason"] == "source_digest_or_marker_mismatch"
            assert not reads._close_headers


@pytest.mark.parametrize("adopted_only", [False, True])
def test_private_overlay_reduces_actual_lookups_without_changing_frozen_business(
    engine, monkeypatch, record_property, adopted_only
):
    scopes = prepare(engine)
    subjects = set().union(*scopes.values())
    overlay = close_storage._private_overlay
    original_contains, original_getitem = ChainMap.__contains__, ChainMap.__getitem__
    lookups = Counter()

    def contains(mapping, key):
        lookups["contains"] += 1
        return original_contains(mapping, key)

    def getitem(mapping, key):
        lookups["getitem"] += 1
        return original_getitem(mapping, key)

    class UnrelatedCache(dict):
        def __iter__(self):
            raise AssertionError("existing cache must not be walked or copied")

        def keys(self):
            raise AssertionError("existing cache must not be walked or copied")

    def read(flat, unrelated):
        old_parts = UnrelatedCache({("unrelated", i): object() for i in range(unrelated)})
        # Real nested staging shape used by reference/adoption consumers;
        # all writes must still publish to the caller's first private map.
        published = {}
        parts = ChainMap(published, ChainMap({}, old_parts))
        positions = ChainMap({}, ChainMap({}, {}))

        def operation():
            with QueryReads.snapshot(engine) as reads:
                rows = reads.authoritative_close_rows(periods=scopes)
                headers = tuple(reads.close_header(row) for row in rows)
                method = (
                    close_storage.read_adopted_results_many if adopted_only
                    else close_storage.read_accounting_many
                )
                with monkeypatch.context() as patch:
                    patch.setattr(
                        close_storage, "_private_overlay", overlay if flat else
                        lambda fresh, prior: fresh if prior is None else ChainMap(fresh, prior),
                    )
                    patch.setattr(ChainMap, "__contains__", contains)
                    patch.setattr(ChainMap, "__getitem__", getitem)
                    lookups.clear()
                    return method(
                        reads.connection, headers, subjects, subjects_by_period=scopes,
                        _verified_parts=parts, _positions_cache=positions,
                    )

        work, result = measure_work(engine, operation)
        assert published
        assert len(old_parts) == unrelated
        return work["counters"], result, dict(lookups)

    legacy_work, expected, legacy_lookups = read(False, 0)
    flat_work, actual, flat_lookups = read(True, 0)
    grown_work, grown, grown_lookups = read(True, 1024)
    assert actual == expected == grown
    assert [tuple(item["subject_id"] for item in part.adopted_results) for part in actual] == [
        ("included",), ("february",), (),
    ]
    # Full accounting describes the requested union; adopted-only describes
    # each month's scope. Exact returned leaves are checked above in both lanes.
    assert [part.subjects for part in actual] == [
        frozenset(scopes[part.period] if adopted_only else subjects) for part in actual
    ]
    if not adopted_only:
        assert [tuple(voucher["total"] for voucher in part.vouchers) for part in actual] == [
            (100,), (100,), (),
        ]
        assert all(voucher["adopted_calculation_id"] == part.adopted_results[0]["calculation_id"]
                   for part in actual for voucher in part.vouchers)
    assert flat_lookups == grown_lookups
    assert sum(flat_lookups.values()) < sum(legacy_lookups.values())
    assert flat_work["returned_rows"] > 0
    assert flat_work["stdlib_json_input_bytes"] > 0
    for metric in ("sql_calls", "sqlite_vm_steps", "returned_rows", "returned_value_bytes",
                   "stdlib_json_loads", "stdlib_json_input_bytes"):
        assert flat_work[metric] == legacy_work[metric] == grown_work[metric]
    record_property("private_overlay_lookups", {
        "legacy": legacy_lookups, "flat": flat_lookups, "unrelated_1024": grown_lookups,
    })


@pytest.mark.parametrize("unrelated", [0, 1024])
def test_private_overlay_preserves_leaf_order_references_and_private_writes(unrelated):
    class UnwalkedMap(dict):
        def __iter__(self):
            raise AssertionError("flatten must not walk leaf keys")

        def keys(self):
            raise AssertionError("flatten must not walk leaf keys")

        def items(self):
            raise AssertionError("flatten must not walk leaf entries")

    class CustomChainMap(ChainMap):
        def __getitem__(self, key):
            return "custom" if key == "special" else super().__getitem__(key)

    fresh, empty = {}, {}
    left = UnwalkedMap({"shared": "left"})
    right = UnwalkedMap({"shared": "right", "tail": "right"})
    large = UnwalkedMap((index, object()) for index in range(unrelated))
    custom = CustomChainMap({"special": "underlying"})
    prior = ChainMap(left, ChainMap(empty, right, left), large, custom)
    result = close_storage._private_overlay(fresh, prior)
    expected = [fresh, left, empty, right, left, large, custom]
    assert len(result.maps) == len(expected)
    assert all(actual is original for actual, original in zip(result.maps, expected, strict=True))
    assert result["shared"] == "left"
    assert result["tail"] == "right"
    assert result["special"] == "custom"
    result["shared"] = "private"
    assert fresh == {"shared": "private"}
    assert left["shared"] == "left"
    right["tail"] = "updated"
    assert result["tail"] == "updated"
    assert close_storage._private_overlay(fresh, None) is fresh


@pytest.mark.parametrize("calls", [12, 48, 120])
@pytest.mark.parametrize("adopted_only", [False, True])
def test_private_overlay_releases_leaves_and_owned_proof_caches_without_collection(
    engine, calls, adopted_only
):
    class TrackedMap(dict):
        pass

    def transient_overlay(payload):
        leaf = TrackedMap(payload)
        reference = weakref.ref(leaf)
        # Keep empty maps and repeated leaves in the real staging shape.
        result = close_storage._private_overlay({}, ChainMap({}, ChainMap(leaf, {}, leaf)))
        assert result["payload"] is payload["payload"]
        return reference

    scopes = prepare(engine)
    subjects = set().union(*scopes.values())
    with QueryReads.snapshot(engine) as reads:
        reads._verified_close_storage_parts = TrackedMap()
        reads._close_accounting_positions = TrackedMap()
        caches = (
            weakref.ref(reads._verified_close_storage_parts),
            weakref.ref(reads._close_accounting_positions),
        )
        rows = reads.authoritative_close_rows(periods=scopes)
        method = reads.close_adopted_results_many if adopted_only else reads.close_accounting_many
        expected = method(rows, subjects=subjects, subjects_by_period=scopes)
        assert [len(part.adopted_results) for part in expected] == [1, 1, 0]
        if not adopted_only:
            assert [tuple(voucher["total"] for voucher in part.vouchers) for part in expected] == [
                (100,), (100,), (),
            ]
        for _ in range(calls):
            reference = transient_overlay({"payload": expected})
            # No gc.collect or GC setting changes: dropping the last real
            # owner must suffice, even while the business snapshot stays live.
            assert reference() is None
        assert method(rows, subjects=subjects, subjects_by_period=scopes) == expected
        assert all(reference() is not None for reference in caches)
    # reads still exists here; snapshot reset must release its old proof maps.
    assert all(reference() is None for reference in caches)
