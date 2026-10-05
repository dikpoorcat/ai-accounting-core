"""Narrow buckets by month without narrowing full publication/voucher authority."""

import json

import pytest
from stage9_metrics import measure_work
from test_close_accounting_filter import _close_empty_months, _closed_charge
from test_engine import close, evidence, publish, save
from test_engine import engine as engine_fixture
from test_integrity_content import damage

from ai_accounting.kernel import close_storage, close_storage_v1
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

engine = engine_fixture


def series(engine, months=3):
    scopes = {}
    for month in range(1, months + 1):
        period = f"2026-{month:02}"
        subjects = {f"charge-{month}-{number}" for number in range(4)}
        for subject in sorted(subjects):
            save(engine, subject=subject, period=period, request=f"save-{subject}")
        publish(engine, sorted(subjects), request=f"publish-{period}")
        _close_empty_months(engine, [period])
        scopes[YearMonth(period).ordinal] = subjects
    return scopes


def selected(engine, period, subjects, monkeypatch, *, narrow=True, **options):
    with monkeypatch.context() as patch:
        if not narrow:
            original = QueryReads.close_accounting_many

            def full(self, rows, *, subjects, subjects_by_period=None):
                return original(self, rows, subjects=subjects)

            patch.setattr(QueryReads, "close_accounting_many", full)
        with QueryReads.snapshot(engine) as reads:
            return BusinessQueries(engine, reads=reads)._selected_accounting(
                reads.connection, subjects, period, **options
            )


def batch(engine, scopes, *, narrow=True, owned=True):
    subjects = set().union(*scopes.values())
    if owned:
        with QueryReads.snapshot(engine) as reads:
            rows = reads.authoritative_close_rows(periods=scopes)
            return reads.close_accounting_many(
                rows, subjects=subjects,
                **({"subjects_by_period": scopes} if narrow else {}),
            )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        rows = reads.authoritative_close_rows(periods=scopes)
        return reads.close_accounting_many(rows, subjects=subjects, subjects_by_period=scopes)


@pytest.mark.parametrize("months", [3, 6])
def test_business_selection_and_batch_prove_omitted_subjects_with_frozen_filter(
    engine, monkeypatch, record_property, months
):
    scopes = series(engine, months=months)
    subjects = set().union(*scopes.values())
    probes = []
    original = close_storage._may_contain_with_positions

    def counted(*args):
        probes.append(args[1])
        return original(*args)

    monkeypatch.setattr(close_storage, "_may_contain_with_positions", counted)
    full_work, full = measure_work(engine, lambda: batch(engine, scopes, narrow=False))
    full_probes = len(probes)
    probes.clear()
    narrow_work, narrow = measure_work(engine, lambda: batch(engine, scopes))
    assert full_probes == len(scopes) * len(subjects)
    assert len(probes) == full_probes
    assert [(part.adopted_results, part.vouchers) for part in narrow] == [
        (part.adopted_results, part.vouchers) for part in full
    ]
    assert [part.subjects for part in narrow] == [frozenset(subjects)] * len(scopes)
    for counter in ("returned_rows", "returned_value_bytes", "calculation_result_rows_loaded"):
        assert narrow_work["counters"].get(counter, 0) <= full_work["counters"].get(counter, 0)
    record_property("full_work", json.dumps(full_work["counters"], sort_keys=True))
    record_property("narrow_work", json.dumps(narrow_work["counters"], sort_keys=True))
    record_property("membership_probes", f"{full_probes}->{len(probes)}")
    period = f"2026-{months:02}"
    assert selected(engine, period, subjects, monkeypatch) == selected(
        engine, period, subjects, monkeypatch, narrow=False
    )


@pytest.mark.parametrize("scope", [set(), {"absent"}])
def test_wrong_full_scope_cannot_hide_publication_even_after_a_smaller_cache_hit(engine, scope):
    january = _closed_charge(engine)
    # The mutable directory is missing the very publication that originally
    # placed this subject in the month's candidate scope.
    damage(engine, "calculation_publication",
           "DELETE FROM calculation_publication WHERE subject_id='included'",
           foreign_keys=False)
    subjects = {"included", "absent"}
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=[january])
        # A successful absence proof for a smaller authority universe is real,
        # but cannot be reused when the complete requested subjects expand.
        assert reads.close_accounting_many(
            rows, subjects={"absent"}, subjects_by_period={january: scope & {"absent"}},
        )[0].adopted_results == ()
        previous = dict(reads._close_accounting_slices)
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                reads.close_accounting_many(
                    rows, subjects=subjects, subjects_by_period={january: scope},
                )
            assert failure.value.code == "content_integrity_failed"
            assert reads._close_accounting_slices == previous


def test_independent_voucher_authority_is_not_filtered_by_month_scope(engine):
    january = _closed_charge(engine)
    # A corrupted additional physical voucher owned by a subject with no
    # publication in this close must still be discovered by full authority.
    save(engine, subject="later", period="2026-02", request="later")
    publish(engine, ["later"], request="publish-later")
    damage(engine, "voucher_version",
           "UPDATE voucher_version SET period=? WHERE calculation_id=("
           "SELECT calculation_id FROM calculation_current WHERE subject_id='later')",
           (january,))
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=[january])
        with pytest.raises(KernelError) as failure:
            reads.close_accounting_many(
                rows, subjects={"included", "later"},
                subjects_by_period={january: {"included"}},
            )
        assert failure.value.code == "content_integrity_failed"
        assert failure.value.details["reason"] == "storage_voucher_source_mismatch"
        assert not reads._close_accounting_slices
        assert not reads._verified_close_storage_parts


def test_equal_full_scope_shares_existing_complete_cache_without_losing_narrow_authority(engine):
    january = _closed_charge(engine)
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=[january])
        actual = reads.close_accounting_many(
            rows, subjects={"included"}, subjects_by_period={january: {"included"}},
        )[0]
        assert reads.close_accounting(rows[0], subjects={"included"}) is actual
        assert reads.close_accounting_many(rows, subjects={"included"})[0] is actual
        previous = dict(reads._close_accounting_slices)
        recovered = reads.close_accounting_many(
            rows, subjects={"included"}, subjects_by_period={january: set()},
        )[0]
        assert recovered == actual
        assert all(reads._close_accounting_slices[key] == value
                   for key, value in previous.items())


@pytest.mark.parametrize(
    "change", ["missing_directory", "missing_block", "bad_block", "publication"]
)
def test_failed_narrow_batch_publishes_no_prefix(engine, change):
    scopes = series(engine, months=2)
    january = YearMonth("2026-01").ordinal
    if change == "publication":
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE posting_period=?", (january,),
               foreign_keys=False)
    else:
        table = (
            "close_storage_directory" if change == "missing_directory" else "close_storage_block"
        )
        sql = (
            f"UPDATE {table} SET content='[]' WHERE period=? AND field='adopted_results'"
            if change == "bad_block" else
            f"DELETE FROM {table} WHERE period=? AND field='adopted_results'"
        )
        damage(engine, table, sql, (january,))
    with QueryReads.snapshot(engine) as reads:
        rows = list(reversed(reads.authoritative_close_rows(periods=scopes)))
        subjects = set().union(*scopes.values())
        previous_headers = dict(reads._close_headers)
        for _ in range(2):
            with pytest.raises(KernelError):
                reads.close_accounting_many(rows, subjects=subjects, subjects_by_period=scopes)
            assert reads._close_headers == previous_headers
            assert not reads._close_accounting_slices
            assert not reads._verified_close_storage_parts
            assert not reads._close_accounting_positions


def test_publication_and_physical_voucher_periods_both_locate_scope(engine, monkeypatch):
    january = _closed_charge(engine)
    _close_empty_months(engine, ["2026-02"])
    damage(engine, "calculation_publication",
           "UPDATE calculation_publication SET posting_period=? WHERE subject_id='included'",
           (january + 1,))
    calls = []
    original = QueryReads.close_accounting_many

    def capture(self, rows, *, subjects, subjects_by_period=None):
        calls.append(subjects_by_period)
        return original(self, rows, subjects=subjects, subjects_by_period=subjects_by_period)

    monkeypatch.setattr(QueryReads, "close_accounting_many", capture)
    for narrow in (True, False):
        with pytest.raises(KernelError):
            selected(engine, "2026-02", {"included"}, monkeypatch, narrow=narrow)
    assert calls[0] == {january: {"included"}, january + 1: {"included"}}


def test_open_replacement_source_month_move_and_no_line_state_match_full_scope(engine, monkeypatch):
    proof = evidence(engine)
    save(engine, subject="charge", request="first")
    publish(engine, request="first-publication")
    save(engine, subject="other", period="2026-02", request="other")
    _, other = publish(engine, ["other"], request="other-publication")
    engine.amend_fact(
        "test_charge", "charge", {"period": "2026-02", "amount": 125, "suppress_posting": True},
        evidence=(proof,), expected_revision=1, request_id="move-to-no-line",
        recording_error_confirmed=True,
    )
    _, moved = publish(engine, request="moved-publication")
    _close_empty_months(engine, ["2026-01", "2026-02"])
    subjects = {"charge", "other"}
    result = selected(engine, "2026-02", subjects, monkeypatch)
    assert result == selected(engine, "2026-02", subjects, monkeypatch, narrow=False)
    assert [item["calculation_id"] for item in result["through_period"]["state_results"]] == [
        moved["results"][0]["calculation_id"]
    ]
    assert [item["calculation_id"] for item in result["through_period"]["voucher_events"]] == [
        other["results"][0]["calculation_id"]
    ]


def test_old_voucher_new_no_impact_adoption_and_cross_month_correction_match(engine, monkeypatch):
    save(engine, request="first")
    _, original = publish(engine, request="first-publication")
    save(engine, revision=1, request="review")
    review_preview, reviewed = publish(engine, request="review-publication")
    assert review_preview["results"][0]["impact"] == "review_no_impact"
    close(engine)
    save(engine, subject="unrelated", period="2026-02", request="unrelated")
    publish(engine, ["unrelated"], request="unrelated-publication")
    _close_empty_months(engine, ["2026-02"])
    save(engine, amount=150, revision=2, request="correct")
    publish(engine, request="correction-publication", posting_period="2026-03")
    _close_empty_months(engine, ["2026-03"])
    subjects = {"charge", "unrelated"}
    for posting_period in (None, "2026-01", "2026-03"):
        result = selected(engine, "2026-03", subjects, monkeypatch, posting_period=posting_period)
        assert result == selected(
            engine, "2026-03", subjects, monkeypatch, narrow=False, posting_period=posting_period
        )
    result = selected(engine, "2026-01", subjects, monkeypatch)
    event = result["through_period"]["voucher_events"][0]
    assert event["voucher_calculation_id"] == original["results"][0]["calculation_id"]
    assert event["calculation_id"] == reviewed["results"][0]["calculation_id"]


def test_nonowned_scope_and_fixed_v1_keep_their_proof_boundaries(engine, monkeypatch):
    scopes = series(engine, months=2)
    assert batch(engine, scopes, owned=False) == batch(engine, scopes)
    calls = []
    original = close_storage_v1.read_accounting

    def observed(connection, header, subjects, **kwargs):
        calls.append(frozenset(subjects))
        return original(connection, header, subjects, **kwargs)

    monkeypatch.setattr(close_storage_v1, "read_accounting", observed)
    with historical_content(1):
        actual = batch(engine, scopes)
    assert calls == [frozenset(set().union(*scopes.values()))] * 2
    assert [(part.adopted_results, part.vouchers) for part in actual] == [
        (part.adopted_results, part.vouchers) for part in batch(engine, scopes)
    ]


def test_withdrawn_terminal_and_empty_close_match_full_scope(engine, monkeypatch):
    proof = evidence(engine)
    save(engine, subject="retained", request="retained")
    publish(engine, ["retained"], request="retained-publication")
    save(engine, request="removed")
    publish(engine, request="removed-publication")
    preview = engine.preview_delete("charge", recording_error_evidence=proof)
    engine.delete(
        "charge", preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id="withdraw", recording_error_evidence=proof,
    )
    _close_empty_months(engine, ["2026-01", "2026-02"])
    result = selected(engine, "2026-02", {"retained", "charge"}, monkeypatch)
    assert result == selected(engine, "2026-02", {"retained", "charge"}, monkeypatch, narrow=False)
    assert len(result["through_period"]["voucher_events"]) == 1
    assert result["through_period"]["state_results"] == []
    january = YearMonth("2026-01").ordinal
    scopes = {january: {"retained", "charge"}, january + 1: set()}
    narrow = batch(engine, scopes)
    assert narrow[1].adopted_results == narrow[1].vouchers == ()
    assert [(p.adopted_results, p.vouchers) for p in narrow] == [
        (p.adopted_results, p.vouchers) for p in batch(engine, scopes, narrow=False)
    ]


@pytest.mark.parametrize("scopes", [{}, {123: {"included"}}])
def test_full_period_scopes_must_cover_exact_requested_closes(engine, scopes):
    january = _closed_charge(engine)
    with QueryReads.snapshot(engine) as reads, pytest.raises(ValueError):
        reads.close_accounting_many(
            reads.authoritative_close_rows(periods=[january]),
            subjects={"included"}, subjects_by_period=scopes,
        )
