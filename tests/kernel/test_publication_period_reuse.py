"""Existing exact publication digests never replace physical period coverage."""

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from stage9_metrics import measure_work
from test_engine import engine as engine  # noqa: F401
from test_engine import publish, save

from ai_accounting.kernel import publication
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.period_balances import _period_seals
from ai_accounting.kernel.query_reads import QueryReads, verify_current_voucher_publications
from ai_accounting.kernel.types import YearMonth, canonical, digest


class _PublicationStore:
    """A small real SQLite source, with the same owned read-only lifetime."""

    def __init__(self, path):
        self.path = path
        self.registry = SimpleNamespace(content_version=2)

    @contextmanager
    def connection(self, *, read_only=False):
        connection = sqlite3.connect(
            self.path.as_uri() + ("?mode=ro" if read_only else ""), uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()


def source(tmp_path, count=4, *, second_period=False, unrelated=0):
    store = _PublicationStore(tmp_path / "publication.sqlite")
    with store.connection() as connection:
        connection.execute(
            "CREATE TABLE calculation_publication("
            "id TEXT PRIMARY KEY,sequence INTEGER UNIQUE,subject_id TEXT,"
            "previous_publication_id TEXT,calculation_id TEXT,mode TEXT,"
            "posting_period INTEGER,baseline_calculation_id TEXT,voucher_id TEXT) STRICT"
        )
        records = []
        for index in range(count + unrelated):
            values = {
                "sequence": index + 1,
                "subject_id": f"业务😀-{index}",
                "previous_publication_id": None,
                "calculation_id": f"calculation-{index}",
                "mode": "initial",
                "posting_period": (
                    3 if index >= count else int(second_period and index == count - 1)
                ),
                "baseline_calculation_id": None,
                "voucher_id": f"voucher-{index}",
            }
            records.append({"id": "p_" + digest(values).hex(), **values})
        connection.executemany(
            "INSERT INTO calculation_publication VALUES(?,?,?,?,?,?,?,?,?)",
            [(row["id"], *(row[field] for field in publication.CONTENT_FIELDS))
             for row in reversed(records)],
        )
        connection.commit()
    return SimpleNamespace(store=store), records


def observe_hashes(monkeypatch):
    original = publication.hashlib.sha256
    calls = []

    def observe(value, *args, **kwargs):
        if value.startswith(b'{"baseline_calculation_id":'):
            calls.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(publication.hashlib, "sha256", observe)
    return calls


def test_1005_checked_and_23_unchecked_keep_all_1028_headers_with_less_transfer(
    tmp_path, monkeypatch, record_property
):
    engine, records = source(tmp_path, 1028)
    hashes = observe_hashes(monkeypatch)

    def read():
        with QueryReads.snapshot(engine) as reads:
            checked = list(reads.connection.execute(
                "SELECT * FROM calculation_publication ORDER BY sequence LIMIT 1005"
            ))
            reads.verify_publication_records(checked)
            hashes.clear()
            result = reads.verify_publication_periods({0})
            return result, len(hashes)

    current_work, (current, current_hashes) = measure_work(engine, read)
    original = publication._verified_period_headers
    with monkeypatch.context() as patch:
        patch.setattr(publication, "_verified_period_headers",
                      lambda connection, periods, verified: original(connection, periods, {}))
        strict_work, (strict, strict_hashes) = measure_work(engine, read)
    assert current == strict == [
        {key: row[key] for key in ("id", "posting_period", "sequence")} for row in records
    ]
    assert current_hashes == 23
    assert strict_hashes == 1028
    assert current_work["counters"]["returned_rows"] == (
        strict_work["counters"]["returned_rows"] + 23
    )
    assert current_work["counters"]["returned_value_bytes"] < (
        strict_work["counters"]["returned_value_bytes"]
    )
    for name in ("sql_calls", "returned_rows", "returned_value_bytes", "sqlite_vm_steps"):
        record_property(f"reuse_{name}", current_work["counters"][name])
        record_property(f"strict_{name}", strict_work["counters"][name])
    with engine.store.connection(read_only=True) as connection:
        assert _period_seals(connection, [], {0}, verified_publications=current) == (
            _period_seals(connection, [], {0})
        )


def test_unrelated_verified_id_count_cannot_grow_requested_sql_parameters_or_work(
    tmp_path, monkeypatch, record_property
):
    engine, records = source(tmp_path, 1028, unrelated=5000)
    original = publication._verified_period_headers
    observed = []

    class ObserveParameters:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters):
            observed.append((statement, tuple(parameters)))
            return self.connection.execute(statement, parameters)

    class NoWholeCacheWalk(dict):
        def __iter__(self):
            raise AssertionError("the requested period must not enumerate unrelated proof IDs")

        def keys(self):
            raise AssertionError("the requested period must not enumerate unrelated proof IDs")

        def items(self):
            raise AssertionError("the requested period must not enumerate unrelated proof IDs")

        def values(self):
            raise AssertionError("the requested period must not enumerate unrelated proof IDs")

    def capture(connection, periods, verified):
        return original(ObserveParameters(connection), periods, verified)

    monkeypatch.setattr(publication, "_verified_period_headers", capture)

    def read(unrelated):
        def operation():
            with QueryReads.snapshot(engine) as reads:
                reads.verify_publication_records(records[:1005])
                if unrelated:
                    reads.verify_publication_records(records[1028:])
                reads._verified_publication_ids = NoWholeCacheWalk(reads._verified_publication_ids)
                observed.clear()
                return reads.verify_publication_periods({0})
        work, headers = measure_work(engine, operation)
        requested = [
            item for item in work["sql"]
            if item["statement"].startswith("SELECT id,posting_period,sequence")
        ]
        counters = {
            name: sum(item.get(name, 0) for item in requested)
            for name in ("calls", "returned_rows", "returned_value_bytes", "sqlite_vm_steps")
        }
        return headers, counters, list(observed)

    headers, work, parameters = read(False)
    many_headers, many_work, many_parameters = read(True)
    assert headers == many_headers and len(headers) == 1028
    assert work == many_work
    assert parameters == many_parameters
    assert len(parameters) == 2
    assert parameters[0][1] == ("[0]",)
    assert parameters[1][1] == (canonical(sorted(row["id"] for row in records[1005:1028])),)
    for name, value in work.items():
        record_property(f"requested_{name}_with_5000_unrelated_ids", value)
    record_property("requested_parameter_bytes", sum(
        len(value.encode("utf-8")) for _, values in parameters for value in values
    ))


@pytest.mark.parametrize("field", publication.CONTENT_FIELDS)
def test_unchecked_record_damage_keeps_full_canonical_validation(tmp_path, field):
    engine, records = source(tmp_path)
    value = records[-1][field]
    replacement = value + 1 if type(value) is int else "damaged"
    with engine.store.connection() as connection:
        connection.execute(
            f"UPDATE calculation_publication SET {field}=? WHERE id=?",
            (replacement, records[-1]["id"]),
        )
        connection.commit()
    with QueryReads.snapshot(engine) as reads:
        reads.verify_publication_records(records[:1])
        previous = dict(reads._verified_publication_ids)
        with pytest.raises(KernelError) as failure:
            reads.verify_publication_periods({0, 1})
        assert failure.value.details["reason"] == "publication_digest"
        assert reads._verified_publications == {}
        assert reads._verified_publication_ids == previous


@pytest.mark.parametrize("boundary", ["unowned", "write", "disabled", "registry_v1", "context_v1"])
def test_noncurrent_or_unowned_reads_keep_full_digest_path(tmp_path, monkeypatch, boundary):
    engine, records = source(tmp_path)
    hashes = observe_hashes(monkeypatch)
    if boundary in {"unowned", "write"}:
        with engine.store.connection(read_only=boundary == "unowned") as connection:
            if boundary == "write":
                connection.execute("BEGIN IMMEDIATE")
            reads = QueryReads(engine, connection)
            reads.verify_publication_records(records)
            hashes.clear()
            statements = []
            connection.set_trace_callback(statements.append)
            assert len(reads.verify_publication_periods({0})) == len(records)
            assert len(hashes) == len(records)
            assert len(statements) == 1 and "json_object(" in statements[0]
            assert reads._verified_publications == reads._verified_publication_ids == {}
        return
    with QueryReads.snapshot(engine) as reads:
        reads.verify_publication_records(records)
        hashes.clear()
        if boundary == "disabled":
            monkeypatch.setattr(reads, "_snapshot_active", False)
        elif boundary == "registry_v1":
            monkeypatch.setattr(engine.store.registry, "content_version", 1)
        statements = []
        reads.connection.set_trace_callback(statements.append)
        with historical_content(1 if boundary == "context_v1" else 2):
            assert len(reads.verify_publication_periods({0})) == len(records)
        assert len(hashes) == len(records)
        assert len(statements) == 1 and "json_object(" in statements[0]
        assert reads._verified_publications == {}


def test_warm_periods_new_period_and_next_snapshot_keep_exact_scope(tmp_path, monkeypatch):
    engine, records = source(tmp_path, second_period=True)
    hashes = observe_hashes(monkeypatch)
    with QueryReads.snapshot(engine) as reads:
        reads.verify_publication_records(records[:1])
        hashes.clear()
        first = reads.verify_publication_periods({0})
        assert len(first) == 3 and len(hashes) == 2
        hashes.clear()
        assert reads.verify_publication_periods({0}) == first
        assert hashes == []
        full = reads.verify_publication_periods({0, 1, 2})
        assert len(full) == 4 and len(hashes) == 1
        assert reads._verified_publications[2] == ()
    assert reads._verified_publications == reads._verified_publication_ids == {}
    hashes.clear()
    with QueryReads.snapshot(engine) as fresh:
        statements = []
        fresh.connection.set_trace_callback(statements.append)
        assert fresh.verify_publication_periods({0, 1, 2}) == full
        assert len(hashes) == 4
        assert len(statements) == 1 and "json_object(" in statements[0]


def test_period_conflict_rejects_before_publishing_success(tmp_path):
    engine, records = source(tmp_path)
    with QueryReads.snapshot(engine) as reads:
        reads.verify_publication_records(records[:1])
        reads._verified_publication_ids[records[0]["id"]] = 1
        with pytest.raises(KernelError) as failure:
            reads.verify_publication_periods({0})
        assert failure.value.details["reason"] == "publication_period_mismatch"
        assert reads._verified_publications == {}
        assert len(reads._verified_publication_ids) == 1


def test_late_failure_does_not_cache_other_new_period_or_good_new_ids(tmp_path):
    engine, records = source(tmp_path, second_period=True)
    with engine.store.connection() as connection:
        connection.execute("UPDATE calculation_publication SET mode='damaged' WHERE id=?",
                           (records[-1]["id"],))
        connection.commit()
    with QueryReads.snapshot(engine) as reads:
        reads.verify_publication_records(records[:1])
        previous = dict(reads._verified_publication_ids)
        for _ in range(2):
            with pytest.raises(KernelError):
                reads.verify_publication_periods({0, 1})
            assert reads._verified_publications == {}
            assert reads._verified_publication_ids == previous


def test_real_current_voucher_proof_can_feed_period_seal_and_public_entry_stays_strict(
    engine, monkeypatch
):
    save(engine)
    publish(engine)
    hashes = observe_hashes(monkeypatch)
    period = YearMonth("2026-01").ordinal
    with QueryReads.snapshot(engine) as reads:
        vouchers = {
            row[0] for row in reads.connection.execute("SELECT version_id FROM voucher_current")
        }
        verify_current_voucher_publications(reads.connection, vouchers)
        hashes.clear()
        headers = reads.verify_publication_periods({period})
        assert len(headers) == 1 and hashes == []
        assert _period_seals(reads.connection, [], {period}, verified_publications=headers) == (
            _period_seals(reads.connection, [], {period})
        )
        hashes.clear()
        statements = []
        reads.connection.set_trace_callback(statements.append)
        assert list(publication.verified_period_headers(reads.connection, {period})) == headers
        assert len(hashes) == 1
        assert len(statements) == 1 and "json_object(" in statements[0]
