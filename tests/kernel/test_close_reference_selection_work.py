"""Exact close-reference selection keeps its damage checks as history grows."""

import json
import sqlite3
from collections import Counter
from types import SimpleNamespace

import pytest
from test_engine import close, publish, save
from test_engine import engine as engine_fixture
from test_integrity_content import damage

from ai_accounting.kernel import close_storage
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_reads import Journal
from ai_accounting.kernel.query_reads import QueryReads, _selected_voucher_path_references
from ai_accounting.kernel.read_indexes import (
    CLOSE_VOUCHERS,
    selected_voucher_references,
    verify_close_references,
)
from ai_accounting.kernel.types import YearMonth

engine = engine_fixture
MONTH = YearMonth("2026-01").ordinal


def _directory():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE close_reference(close_period INTEGER NOT NULL,path TEXT NOT NULL,"
        "position TEXT NOT NULL,reference_type TEXT NOT NULL,reference_id TEXT NOT NULL,"
        "related_id TEXT,PRIMARY KEY(close_period,path,position,reference_type));"
        "CREATE INDEX close_reference_lookup ON "
        "close_reference(reference_type,reference_id,close_period);"
    )
    return connection


def _work(connection, read):
    ticks = [0]
    connection.set_progress_handler(lambda: ticks.__setitem__(0, ticks[0] + 100), 100)
    try:
        rows = [tuple(row) for row in read()]
    finally:
        connection.set_progress_handler(None, 0)
    return Counter(rows), ticks[0]


def test_voucher_reference_lookup_preserves_cutoff_duplicates_and_index_work():
    connection = _directory()
    connection.executemany(
        "INSERT INTO close_reference VALUES(?,?,?,?,?,?)",
        [
            (10, CLOSE_VOUCHERS, str(index), "voucher", f"other-{index}", None)
            for index in range(1000)
        ]
        + [
            (10, CLOSE_VOUCHERS, "1000", "voucher", "chosen", None),
            (11, CLOSE_VOUCHERS, "0", "voucher", "chosen", None),
            (12, CLOSE_VOUCHERS, "0", "voucher", "chosen", None),
            (10, CLOSE_VOUCHERS, "1001", "fact", "chosen", None),
        ],
    )
    ids = ["chosen", "chosen"]
    original_sql = (
        "SELECT r.* FROM json_each(?) ids JOIN close_reference r "
        "ON r.reference_id=ids.value WHERE r.reference_type='voucher' "
        "AND r.close_period<=?"
    )
    original, old_vm = _work(
        connection, lambda: connection.execute(original_sql, (json.dumps(ids), 11))
    )
    selected = []

    def read_selected():
        selected.extend(selected_voucher_references(connection, ids, through_period=11))
        return selected

    selected_work, new_vm = _work(connection, read_selected)
    assert selected_work == original
    assert len(selected) == 4
    assert {row["close_period"] for row in selected} == {10, 11}
    assert {row["reference_type"] for row in selected} == {"voucher"}
    assert new_vm * 10 < old_vm


def test_period_reference_lookup_preserves_wrong_type_and_unique_heads_with_less_work():
    connection = _directory()
    rows = [
        (period, CLOSE_VOUCHERS, str(index), "voucher", f"item-{period}-{index}", None)
        for period in (10, 11)
        for index in range(1000)
    ]
    rows += [
        (10, CLOSE_VOUCHERS, "1000", "fact", "item-10-0", None),
        (11, CLOSE_VOUCHERS, "1000", "unexpected", "item-11-1", None),
        (10, "another.path", "0", "fact", "item-10-0", None),
    ]
    connection.executemany("INSERT INTO close_reference VALUES(?,?,?,?,?,?)", rows)
    pairs = [[f"item-{period}-{index}", period] for period in (10, 11) for index in range(40)]
    pairs.append(["item-10-0", 10])
    # The adoption verifier receives unique live heads keyed by voucher ID.
    # Duplicate-ID multiset behavior belongs to selected_voucher_references above.
    frozen = dict(pairs)
    assert len(frozen) == len(pairs) - 1
    original_sql = (
        "SELECT r.* FROM json_each(?) ids JOIN close_reference r "
        "ON r.reference_id=json_extract(ids.value,'$[0]') "
        "AND r.close_period=json_extract(ids.value,'$[1]') WHERE r.path=?"
    )
    original, old_vm = _work(
        connection,
        lambda: [
            row for row in connection.execute(
                original_sql, (json.dumps(list(frozen.items())), CLOSE_VOUCHERS)
            ) if row["reference_type"] != "voucher"
        ],
    )
    selected = []

    def read_selected():
        selected.extend(_selected_voucher_path_references(connection, frozen))
        return selected

    selected_work, new_vm = _work(connection, read_selected)
    assert selected_work == original
    assert len(selected) == 2
    assert {(row["reference_id"], row["close_period"]) for row in selected} == {
        ("item-10-0", 10), ("item-11-1", 11)
    }
    assert {row["reference_type"] for row in selected} == {"fact", "unexpected"}
    with pytest.raises(KernelError) as failure:
        verify_close_references(connection, selected)
    assert failure.value.details["reason"] == "invalid_reference_type"

    assert new_vm * 3 < old_vm
    large_frozen = {
        f"item-{period}-{index}": period for period in (10, 11) for index in range(250)
    }
    large_rows, large_vm = _work(
        connection, lambda: _selected_voucher_path_references(connection, large_frozen)
    )
    assert sum(large_rows.values()) == 2  # Both wrong-type leaves remain visible.
    assert large_vm < new_vm * 8  # Growing candidates must not rescan per candidate pair.


@pytest.mark.parametrize("count", [1, 100, 500])
def test_exact_reference_work_does_not_grow_with_unrelated_closed_history(count):
    connection = _directory()
    assert _selected_voucher_path_references(connection, {}) == []
    assert _selected_voucher_path_references(connection, {"missing": 10}) == []
    chosen = [
        (10, CLOSE_VOUCHERS, str(index), "voucher", f"chosen-{index}", None)
        for index in range(count)
    ]
    # Any stored type, including an empty or unknown type, must still be read
    # so the original validator can reject it. Another path is not a hit.
    chosen += [
        (10, CLOSE_VOUCHERS, str(count), "", "chosen-0", None),
        (10, CLOSE_VOUCHERS, str(count + 1), "not-a-type", "chosen-0", None),
        (10, "another.path", "0", "voucher", "chosen-0", None),
    ]
    connection.executemany("INSERT INTO close_reference VALUES(?,?,?,?,?,?)", chosen)
    pairs = [[f"chosen-{index}", 10] for index in range(count)] + [["chosen-0", 10]]
    frozen = dict(pairs)
    assert len(frozen) == count
    baseline_rows, baseline_vm = _work(
        connection, lambda: _selected_voucher_path_references(connection, frozen)
    )
    assert sum(baseline_rows.values()) == 2
    previous = 0
    for months in (12, 48, 120):
        connection.executemany(
            "INSERT INTO close_reference VALUES(?,?,?,?,?,?)",
            (
                (10 + month, CLOSE_VOUCHERS, f"unrelated-{index}", "voucher",
                 f"unrelated-{month}-{index}", None)
                for month in range(previous, months)
                for index in range(400)
            ),
        )
        actual, vm = _work(
            connection, lambda: _selected_voucher_path_references(connection, frozen)
        )
        assert actual == baseline_rows
        # Logarithmic index depth may change; unrelated directory rows must not
        # turn this exact lookup into a scan, even for one selected reference.
        assert vm <= baseline_vm + 1000
        previous = months
    hits = _selected_voucher_path_references(connection, frozen)
    assert {row["reference_type"] for row in hits} == {"", "not-a-type"}
    with pytest.raises(KernelError) as failure:
        verify_close_references(connection, hits)
    assert failure.value.details["reason"] == "invalid_reference_type"


def test_journal_reuses_snapshot_leaves_and_checks_again_next_snapshot(monkeypatch, engine):
    save(engine)
    publish(engine)
    close(engine)
    decoded_families = []
    load_family = close_storage._family
    selected_headers = []
    load_headers = close_storage.voucher_reference_headers

    def counted_family(connection, header, family):
        decoded_families.append((header.period, family))
        return load_family(connection, header, family)

    monkeypatch.setattr(close_storage, "_family", counted_family)

    def counted_headers(connection, batches, *, parts):
        batches = list(batches)
        result = load_headers(connection, batches, parts=parts)
        selected_headers.extend(
            (header.period, reference) for header, references in batches
            for reference in references
        )
        return result

    monkeypatch.setattr(close_storage, "voucher_reference_headers", counted_headers)

    def observe_adoptions(reads):
        seen = []
        verify = reads.verify_selected_voucher_adoptions

        def counted(rows, *, through_period):
            rows = tuple(rows)
            result = verify(rows, through_period=through_period)
            # Record successful adoption proofs, including their cutoff.
            seen.extend((row["id"], row["close_period"], through_period) for row in rows)
            return result

        monkeypatch.setattr(reads, "verify_selected_voucher_adoptions", counted)
        return seen

    def journal_rows(reads):
        snapshot = SimpleNamespace(
            connection=reads.connection, reads=reads, month=MONTH, period="2026-01"
        )
        journal = Journal(snapshot, month=MONTH)
        query, parameters = journal.sql()
        rows = reads.connection.execute(query, parameters).fetchall()
        assert rows and all(row["close_period"] == MONTH for row in rows)
        return journal, rows

    with QueryReads.snapshot(engine) as reads:
        journal, rows = journal_rows(reads)
        reads.verify_selected_voucher_adoptions(rows, through_period=MONTH)
        assert (MONTH, "accounting") in decoded_families
        assert len(selected_headers) == len(rows)
        expected_heads = {(MONTH, row["id"]) for row in rows}
        assert set(reads._verified_frozen_voucher_headers) == expected_heads
        assert len(reads._verified_selected_voucher_adoptions) == len(rows)
        reads.metadata({row["basis_calculation_id"] for row in rows})
        reads.voucher_lines({row["id"] for row in rows})
        decoded_families.clear()
        selected_headers.clear()
        seen = observe_adoptions(reads)
        hydrated = journal.hydrate(rows)
        assert len(hydrated) == len(rows)
        assert len(seen) == len(rows)
        assert set(seen) == {(row["id"], MONTH, MONTH) for row in rows}
        assert not decoded_families
        assert not selected_headers
        assert set(reads._verified_frozen_voucher_headers) == expected_heads
        same_snapshot = [dict(row) for row in hydrated]

    with QueryReads.snapshot(engine) as reads:
        journal, rows = journal_rows(reads)
        reads.metadata({row["basis_calculation_id"] for row in rows})
        reads.voucher_lines({row["id"] for row in rows})
        decoded_families.clear()
        selected_headers.clear()
        assert not reads._verified_selected_voucher_adoptions
        assert not reads._verified_frozen_voucher_headers
        seen = observe_adoptions(reads)
        fresh = [dict(row) for row in journal.hydrate(rows)]
        assert fresh == same_snapshot
        assert len(seen) == len(rows)
        assert set(seen) == {(row["id"], MONTH, MONTH) for row in rows}
        assert (MONTH, "accounting") in decoded_families
        assert len(selected_headers) == len(rows)
        assert set(reads._verified_frozen_voucher_headers) == expected_heads
        assert len(reads._verified_selected_voucher_adoptions) == len(rows)


def test_journal_bad_leaf_rejected_without_success_cache(engine):
    save(engine)
    publish(engine)
    close(engine)
    damage(
        engine,
        "close_reference",
        "UPDATE close_reference SET position='999' "
        "WHERE close_period=? AND path=? AND reference_type='voucher'",
        (MONTH, CLOSE_VOUCHERS),
    )
    with QueryReads.snapshot(engine) as reads:
        snapshot = SimpleNamespace(
            connection=reads.connection, reads=reads, month=MONTH, period="2026-01"
        )
        journal = Journal(snapshot, month=MONTH)
        query, parameters = journal.sql()
        rows = reads.connection.execute(query, parameters).fetchall()
        assert rows
        with pytest.raises(KernelError) as failure:
            journal.hydrate(rows)
        assert failure.value.code == "content_integrity_failed"
        assert not reads._verified_close_references
        assert not reads._verified_selected_voucher_adoptions
        assert not reads._verified_frozen_voucher_headers
        with pytest.raises(KernelError):
            journal.hydrate(rows)
        assert not reads._verified_close_references
        assert not reads._verified_selected_voucher_adoptions
        assert not reads._verified_frozen_voucher_headers
