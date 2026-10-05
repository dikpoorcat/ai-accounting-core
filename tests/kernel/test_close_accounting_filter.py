"""The committed accounting filter may skip blocks, never authority checks."""

import hashlib
import json
import sqlite3
from collections import Counter
from types import SimpleNamespace

import pytest
from test_business_queries import state_review_engine as state_review_engine_fixture
from test_engine import close, evidence, publish, save
from test_engine import engine as engine_fixture
from test_integrity_content import damage

from ai_accounting.kernel import close_storage, close_storage_v1
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.key_membership_filter import decode_keys_filter, may_contain
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth, canonical

engine = engine_fixture
state_review_engine = state_review_engine_fixture


def _closed_charge(engine):
    save(engine, subject="included", request="included")
    publish(engine, ["included"])
    close(engine)
    return YearMonth("2026-01").ordinal


def _close_empty_months(engine, months):
    for month in months:
        periods = Periods(engine)
        proof = evidence(engine)
        for category in MATERIAL_CATEGORIES:
            periods.inventory(
                month, category, evidence=[], expected=0, no_business=True,
                confirmation_evidence=proof, request_id=f"inventory-{month}-{category}",
            )
        preview = periods.preview_close(month, owner_confirmation=proof)
        periods.close(
            month, owner_confirmation=proof, preview_digest=preview["digest"],
            epochs=preview["epochs"], request_id=f"close-{month}",
        )


def _reseal_accounting_subroot(engine, period, change):
    """Damage one synthetic private root coherently, leaving logical close intact."""
    with engine.store.connection(read_only=True) as connection:
        subroot = json.loads(connection.execute(
            "SELECT content FROM close_storage_subroot WHERE period=? AND family='accounting'",
            (period,),
        ).fetchone()[0])
        root = json.loads(connection.execute(
            "SELECT manifest FROM period_close WHERE period=?", (period,)
        ).fetchone()[0])
    change(subroot)

    def sha(value):
        return hashlib.sha256(canonical(value).encode("utf-8")).digest()

    subroot_digest = sha(subroot)
    damage(
        engine, "close_storage_subroot",
        "UPDATE close_storage_subroot SET content=?,digest=? "
        "WHERE period=? AND family='accounting'",
        (canonical(subroot), subroot_digest, period),
    )
    root["subroots"]["accounting"] = subroot_digest.hex()
    damage(
        engine, "period_close", "UPDATE period_close SET manifest=? WHERE period=?",
        (canonical(root), period),
    )
    damage(
        engine, "close_storage_root",
        "UPDATE close_storage_root SET storage_digest=? WHERE period=?",
        (sha(root), period),
    )


def test_accounting_filter_exact_absence_false_positive_and_v1_reader(engine, monkeypatch):
    period = _closed_charge(engine)
    with engine.store.connection(read_only=True) as connection:
        subroot = json.loads(connection.execute(
            "SELECT content FROM close_storage_subroot WHERE period=? AND family='accounting'",
            (period,),
        ).fetchone()[0])
    decoded = decode_keys_filter(subroot["subject_filter"])
    assert decoded.key_count == 1
    absent = next(f"other-{index}" for index in range(10000)
                  if not may_contain(decoded, f"other-{index}"))
    false_positive = next(f"other-{index}" for index in range(10000)
                          if may_contain(decoded, f"other-{index}"))
    assert may_contain(decoded, "included")
    counted = []
    original = close_storage._buckets_rows

    def track(connection, header, subroot, field, buckets):
        if field in close_storage.ACCOUNTING_FIELDS:
            counted.append((field, tuple(buckets)))
        return original(connection, header, subroot, field, buckets)

    monkeypatch.setattr(close_storage, "_buckets_rows", track)
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[period])[0]
        assert reads.close_accounting(row, subjects={absent}).adopted_results == ()
        assert all(not buckets for _, buckets in counted)
        counted.clear()
        assert reads.close_accounting(row, subjects={false_positive}).adopted_results == ()
        assert counted
        actual = reads.close_accounting(row, subjects={"included"})
        assert [item["subject_id"] for item in actual.adopted_results] == ["included"]
    with historical_content(1), engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
        header = close_storage_v1.verified_header(connection, row)
        assert close_storage_v1.read_accounting(
            connection, header, {absent}
        ).adopted_results == ()
        assert [item["subject_id"] for item in close_storage_v1.read_accounting(
            connection, header, {"included"}
        ).adopted_results] == ["included"]
        assert close_storage_v1.decode_close(connection, row)["adopted_results"]
    with historical_content(1), QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[period])[0]
        assert [item["subject_id"] for item in reads.close_accounting(
            row, subjects={"included"}
        ).adopted_results] == ["included"]
        assert not reads._close_accounting_positions


def test_snapshot_reuses_pure_filter_positions_across_months_and_rechecks_sources(
    engine, monkeypatch
):
    period = _closed_charge(engine)
    for month in ("2026-02", "2026-03"):
        periods = Periods(engine)
        proof = evidence(engine)
        for category in MATERIAL_CATEGORIES:
            periods.inventory(
                month, category, evidence=[], expected=0, no_business=True,
                confirmation_evidence=proof, request_id=f"inventory-{month}-{category}",
            )
        preview = periods.preview_close(month, owner_confirmation=proof)
        periods.close(
            month, owner_confirmation=proof, preview_digest=preview["digest"],
            epochs=preview["epochs"], request_id=f"close-{month}",
        )
    periods = [period, YearMonth("2026-02").ordinal, YearMonth("2026-03").ordinal]
    subjects = {"included", "unrelated"}
    with engine.store.connection(read_only=True) as connection:
        bit_counts = {
            json.loads(row[0])["subject_filter"]["bit_count"]
            for row in connection.execute(
                "SELECT content FROM close_storage_subroot WHERE family='accounting'"
            )
        }
    expected = Counter((subject, bits) for subject in subjects for bits in bit_counts)
    assert len(expected) < len(periods) * len(subjects)
    observed = Counter()
    original = close_storage._positions

    def counted(key, bit_count):
        observed[key, bit_count] += 1
        return original(key, bit_count)

    monkeypatch.setattr(close_storage, "_positions", counted)
    for _ in range(2):
        observed.clear()
        with QueryReads.snapshot(engine) as reads:
            rows = reads.authoritative_close_rows(periods=periods)
            assert [row["period"] for row in rows] == periods
            statements = []
            reads.connection.set_trace_callback(statements.append)
            for row in rows:
                result = reads.close_accounting(row, subjects=subjects)
                assert [item["subject_id"] for item in result.adopted_results] == (
                    ["included"] if row["period"] == period else []
                )
                assert reads.close_accounting(row, subjects=subjects) is result
            reads.connection.set_trace_callback(None)
            assert observed == expected
            assert set(reads._close_accounting_positions) == set(expected)
            assert sum(
                "SELECT p.subject_id,p.id,p.calculation_id FROM json_each" in sql
                for sql in statements
            ) == len(rows)
            assert sum("SELECT v.id FROM json_each" in sql for sql in statements) == len(rows)
        assert not reads._close_accounting_positions


def test_scoped_accounting_batch_matches_serial_and_business_selection(engine, monkeypatch):
    first = _closed_charge(engine)
    _close_empty_months(engine, ("2026-02", "2026-03"))
    periods = [first, first + 1, first + 2]
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=periods)
        batched = reads.close_accounting_many(rows, subjects={"included", "unrelated"})
        assert reads.close_accounting_many(rows, subjects={"included", "unrelated"}) == batched
        assert all(
            reads.close_accounting(row, subjects={"included", "unrelated"}) is part
            for row, part in zip(rows, batched, strict=True)
        )
        assert [len(part.adopted_results) for part in batched] == [1, 0, 0]
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=periods)
        assert tuple(
            reads.close_accounting(row, subjects={"included", "unrelated"}) for row in rows
        ) == batched

    with QueryReads.snapshot(engine) as reads:
        expected = BusinessQueries(engine, reads=reads)._selected_accounting(
            reads.connection, {"included"}, "2026-03", include_lines=False
        )
    original = QueryReads.close_accounting_many
    monkeypatch.setattr(
        QueryReads, "close_accounting_many",
        lambda self, rows, *, subjects, subjects_by_period=None: tuple(
            self.close_accounting(row, subjects=subjects) for row in rows
        ),
    )
    with QueryReads.snapshot(engine) as reads:
        assert BusinessQueries(engine, reads=reads)._selected_accounting(
            reads.connection, {"included"}, "2026-03", include_lines=False
        ) == expected
    monkeypatch.setattr(QueryReads, "close_accounting_many", original)


def test_scoped_accounting_batch_keeps_closed_no_impact_adoption_and_old_voucher(engine):
    save(engine, request="initial-fact")
    _, original = publish(engine, request="initial-publication")
    save(engine, revision=1, request="first-review-fact")
    preview, _ = publish(engine, request="first-review-publication")
    assert preview["results"][0]["impact"] == "review_no_impact"
    save(engine, revision=2, request="later-review-fact")
    later_preview, reviewed = publish(engine, request="later-review-publication")
    assert later_preview["results"][0]["impact"] == "review_no_impact"
    first = YearMonth("2026-01").ordinal
    close(engine)
    with engine.store.connection(read_only=True) as connection:
        original_version = connection.execute(
            "SELECT id FROM voucher_version WHERE calculation_id=?",
            (original["results"][0]["calculation_id"],),
        ).fetchone()[0]
        frozen = connection.execute(
            "SELECT manifest FROM period_close WHERE period=?", (first,)
        ).fetchone()[0]
        original_lines = [
            tuple(row) for row in connection.execute(
                "SELECT line_no,account,debit,credit,cashflow FROM voucher_line "
                "WHERE version_id=? ORDER BY line_no",
                (original_version,),
            )
        ]

    _close_empty_months(engine, ("2026-02", "2026-03"))
    periods = [first, first + 1, first + 2]
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=periods)
        batched = reads.close_accounting_many(rows, subjects={"charge"})
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=periods)
        serial = tuple(reads.close_accounting(row, subjects={"charge"}) for row in rows)
    assert batched == serial
    assert [len(part.adopted_results) for part in batched] == [1, 0, 0]
    assert [len(part.vouchers) for part in batched] == [1, 0, 0]
    adopted = batched[0].adopted_results[0]
    voucher = batched[0].vouchers[0]
    assert adopted["calculation_id"] == reviewed["results"][0]["calculation_id"]
    assert voucher["adopted_calculation_id"] == adopted["calculation_id"]
    assert voucher["id"] == original_version
    with QueryReads.snapshot(engine) as reads:
        selected = BusinessQueries(engine, reads=reads)._selected_accounting(
            reads.connection, {"charge"}, "2026-03"
        )
    event = selected["through_period"]["voucher_events"][0]
    assert event["voucher_calculation_id"] == original["results"][0]["calculation_id"]
    assert event["calculation_id"] == adopted["calculation_id"]
    assert sum(line["debit"] for line in event["lines"]) == 100
    assert sum(line["credit"] for line in event["lines"]) == 100
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT manifest FROM period_close WHERE period=?", (first,)
        ).fetchone()[0] == frozen
        assert [
            tuple(row) for row in connection.execute(
                "SELECT line_no,account,debit,credit,cashflow FROM voucher_line "
                "WHERE version_id=? ORDER BY line_no",
                (original_version,),
            )
        ] == original_lines


@pytest.mark.parametrize("damage_kind", ["publication", "voucher", "filter", "member"])
def test_scoped_accounting_batch_failure_does_not_publish_prefix(engine, damage_kind):
    first = _closed_charge(engine)
    _close_empty_months(engine, ("2026-02",))
    if damage_kind == "publication":
        damage(
            engine, "calculation_publication",
            "DELETE FROM calculation_publication WHERE subject_id='included'",
            foreign_keys=False,
        )
    elif damage_kind == "voucher":
        damage(
            engine, "voucher",
            "INSERT INTO voucher(id,number) SELECT 'extra-voucher',max(number)+1 FROM voucher",
        )
        damage(
            engine, "voucher_version",
            "INSERT INTO voucher_version(id,voucher_id,calculation_id,period,reverses_id,total) "
            "SELECT 'extra-version','extra-voucher',calculation_id,period,NULL,total "
            "FROM voucher_version LIMIT 1",
        )
        damage(
            engine, "voucher_current",
            "INSERT INTO voucher_current(voucher_id,version_id) "
            "VALUES('extra-voucher','extra-version')",
        )
    elif damage_kind == "filter":
        _reseal_accounting_subroot(
            engine, first + 1, lambda subroot: subroot.pop("subject_filter")
        )
    else:
        damage(
            engine, "close_storage_block",
            "UPDATE close_storage_block SET content='[]' "
            "WHERE period=? AND field='adopted_results'",
            (first,),
        )
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=[first, first + 1])
        if damage_kind == "member":
            rows.reverse()
        prior_headers = dict(reads._close_headers)
        with pytest.raises(KernelError):
            reads.close_accounting_many(rows, subjects={"included"})
        assert reads._close_headers == prior_headers
        assert not reads._close_accounting_slices
        assert not reads._verified_close_storage_parts
        assert not reads._close_accounting_positions


def test_accounting_authority_batch_work_grows_with_selected_months():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE calculation_publication(
            id TEXT, subject_id TEXT, calculation_id TEXT, posting_period INTEGER,
            sequence INTEGER, previous_publication_id TEXT);
        CREATE INDEX publication_subject ON calculation_publication(subject_id);
        CREATE INDEX publication_previous ON calculation_publication(previous_publication_id);
        CREATE TABLE calculation(id TEXT, subject_id TEXT);
        CREATE INDEX calculation_subject ON calculation(subject_id);
        CREATE TABLE voucher_version(id TEXT, calculation_id TEXT, period INTEGER);
        CREATE INDEX voucher_calculation ON voucher_version(calculation_id);
        CREATE TABLE voucher_current(version_id TEXT);
    """)
    subjects = frozenset(f"subject-{index}" for index in range(12))
    for month in range(120):
        for subject in subjects:
            calc = f"{month}-{subject}"
            connection.execute(
                "INSERT INTO calculation_publication VALUES(?,?,?,?,?,NULL)",
                (f"pub-{calc}", subject, calc, month, month + 1),
            )
            connection.execute("INSERT INTO calculation VALUES(?,?)", (calc, subject))
            connection.execute(
                "INSERT INTO voucher_version VALUES(?,?,?)", (f"voucher-{calc}", calc, month)
            )
            connection.execute(
                "INSERT INTO voucher_current VALUES(?)", (f"voucher-{calc}",)
            )
    work = {}
    for months in (12, 48, 120):
        headers = tuple(
            SimpleNamespace(
                period=month, storage_digest=b"root",
                root={"small": {"publication_sequence": month + 1}},
            )
            for month in range(months)
        )
        progress = [0]

        def counted(progress=progress):
            progress[0] += 1
            return 0

        connection.set_progress_handler(counted, 100)
        try:
            authority = close_storage._accounting_authority(connection, headers, subjects)
        finally:
            connection.set_progress_handler(None, 0)
        assert all(len(authority.publications[month]) == len(subjects) for month in range(months))
        assert all(len(authority.vouchers[month]) == len(subjects) for month in range(months))
        work[months] = progress[0]
    assert work[12] < work[48] < work[120]
    assert work[120] < work[12] * 16


@pytest.mark.parametrize("change,reason", [
    ("missing", "storage_accounting_filter_invalid"),
    ("wrong_but_well_formed", "storage_accounting_filter_mismatch"),
])
def test_accounting_filter_damage_rejected_by_page_and_full_verify(engine, change, reason):
    from ai_accounting.kernel.key_membership_filter import build_keys_filter

    period = _closed_charge(engine)
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[period])[0]
        assert reads.close_accounting(row, subjects={"included"}).adopted_results
    _reseal_accounting_subroot(
        engine, period,
        (lambda subroot: subroot.pop("subject_filter")) if change == "missing"
        else (lambda subroot: subroot.__setitem__(
            "subject_filter", build_keys_filter(["unrelated"])
        )),
    )
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[period])[0]
        with pytest.raises(KernelError) as error:
            reads.close_accounting(row, subjects={"included"})
        assert error.value.details["reason"] == (
            reason if change == "missing" else "storage_adoption_publication_mismatch"
        )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as error:
            verify_integrity(engine, connection)
        assert error.value.details["reason"] == reason


@pytest.mark.parametrize("damage_kind,reason", [
    ("publication", "storage_adoption_publication_mismatch"),
    ("extra_voucher", "storage_voucher_source_mismatch"),
])
def test_accounting_filter_keeps_full_publication_and_voucher_checks(engine, damage_kind, reason):
    period = _closed_charge(engine)
    if damage_kind == "publication":
        damage(
            engine, "calculation_publication",
            "DELETE FROM calculation_publication WHERE subject_id='included'",
            foreign_keys=False,
        )
    else:
        damage(
            engine, "voucher",
            "INSERT INTO voucher(id,number) SELECT 'extra-voucher',max(number)+1 FROM voucher",
        )
        damage(
            engine, "voucher_version",
            "INSERT INTO voucher_version(id,voucher_id,calculation_id,period,reverses_id,total) "
            "SELECT 'extra-version','extra-voucher',calculation_id,period,NULL,total "
            "FROM voucher_version LIMIT 1",
        )
        damage(
            engine, "voucher_current",
            "INSERT INTO voucher_current(voucher_id,version_id) "
            "VALUES('extra-voucher','extra-version')",
        )
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[period])[0]
        with pytest.raises(KernelError) as error:
            reads.close_accounting(row, subjects={"included"})
        assert error.value.details["reason"] == reason


def test_state_only_adoption_cannot_disappear_behind_negative_filter(state_review_engine):
    from ai_accounting.kernel.key_membership_filter import build_keys_filter

    engine = state_review_engine
    engine.save_fact(
        "test_charge", "state-only",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=(evidence(engine),), expected_revision=0, request_id="state-only",
    )
    publish(engine, ["state-only"])
    period = YearMonth("2026-01").ordinal
    close(engine)
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[period])[0]
        selected = reads.close_accounting(row, subjects={"state-only"})
        assert len(selected.adopted_results) == 1
        assert selected.vouchers == ()
    _reseal_accounting_subroot(
        engine, period,
        lambda subroot: subroot.__setitem__("subject_filter", build_keys_filter([])),
    )
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[period])[0]
        with pytest.raises(KernelError) as error:
            reads.close_accounting(row, subjects={"state-only"})
        assert error.value.details["reason"] == "storage_adoption_publication_mismatch"
