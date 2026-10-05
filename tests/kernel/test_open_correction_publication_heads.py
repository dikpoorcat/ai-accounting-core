"""Ordinary readers derive reversal obligations independently of surviving versions."""

import json
from contextlib import contextmanager

import pytest
from stage9_metrics import measure_work
from test_integrity_content import damage
from test_open_correction_source_heads import prepare
from test_publication_periods import close, publish, save
from test_publication_periods import company as company  # noqa: F401
from test_reports import book as _report_book
from test_reports import profile

from ai_accounting.kernel import publication, publication_v1, query_reads, stored_json
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth


@pytest.fixture(params=[publication, publication_v1], ids=["current", "fixed_v1"])
def reader(request):
    return request.param


def _prepare(company, mode):
    prepare(company, mode)
    if mode in {"terminal_review", "clear_correction"}:
        save(company, 120 if mode == "terminal_review" else 0, 2)
        publish(company, "terminal-review-or-clear")


def _terminal(connection, subject="position"):
    rows = list(connection.execute(
        "SELECT p.* FROM calculation_current h "
        "JOIN calculation_publication p ON p.calculation_id=h.calculation_id "
        "WHERE h.subject_id=?", (subject,),
    ))
    assert len(rows) == 1
    return rows


def _check(company, reader):
    with company[0].store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        rows = _terminal(connection)
        for row in rows:
            reader.verify_record(row)
        return reader.verify_open_correction_publication_heads(connection, rows)


def _current_reverse(engine):
    with engine.store.connection(read_only=True) as connection:
        rows = list(connection.execute(
            "SELECT v.* FROM voucher_current h JOIN voucher_version v ON v.id=h.version_id "
            "WHERE v.reverses_id IS NOT NULL "
            "AND v.period>(SELECT coalesce(max(period),-1) FROM period_close)",
        ))
        assert len(rows) == 1
        return dict(rows[0])


def _remove_reverse(engine):
    reverse = _current_reverse(engine)
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE voucher_id=?",
           (reverse["voucher_id"],), foreign_keys=False)
    damage(engine, "voucher_line", "DELETE FROM voucher_line WHERE version_id=?",
           (reverse["id"],), foreign_keys=False)
    damage(engine, "voucher_version", "DELETE FROM voucher_version WHERE id=?",
           (reverse["id"],), foreign_keys=False)
    return reverse


@pytest.mark.parametrize("mode", [
    "review_baseline", "zero_baseline", "cleared_baseline", "zero_correction",
    "same_month_replace", "move_review", "second_closed_correction",
    "terminal_review", "clear_correction",
])
def test_exact_correction_segments_keep_clear_review_replace_and_move(company, reader, mode):
    _prepare(company, mode)
    expected = (
        set() if mode in {"zero_baseline", "cleared_baseline"}
        else {_current_reverse(company[0])["id"]}
    )
    assert _check(company, reader) == expected
    assert _check(company, reader) == expected


@pytest.mark.parametrize("mode", [
    "zero_correction", "same_month_replace", "move_review", "second_closed_correction",
])
def test_complete_reverse_deletion_cannot_remove_ordinary_reader_obligation(
    company, reader, mode,
):
    _prepare(company, mode)
    assert _check(company, reader) == {_current_reverse(company[0])["id"]}
    _remove_reverse(company[0])
    for _ in range(2):
        with pytest.raises(KernelError) as failure:
            _check(company, reader)
        assert failure.value.details["reason"] == "correction_voucher_source_missing"


@contextmanager
def _read(engine, owned):
    if owned:
        with QueryReads.snapshot(engine) as reads:
            yield reads.connection
    else:
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            yield connection


@pytest.mark.parametrize("owned", [False, True], ids=["unowned", "owned"])
def test_shared_current_publication_guard_rejects_missing_reversal_in_each_snapshot(company, owned):
    _prepare(company, "terminal_review")
    with _read(company[0], owned) as connection:
        query_reads.verify_current_publication_voucher_heads(connection, _terminal(connection))
    _remove_reverse(company[0])
    for _ in range(2):
        with _read(company[0], owned) as connection:
            with pytest.raises(KernelError) as failure:
                query_reads.verify_current_publication_voucher_heads(
                    connection, _terminal(connection),
                )
            assert failure.value.details["reason"] == "correction_voucher_source_missing"


@pytest.mark.parametrize("variant", [
    "duplicate", "noncanonical", "shape", "overflow", "self_consistent_zero",
])
def test_necessary_baseline_is_strictly_decoded_with_each_readers_own_encoding(
    company, reader, variant,
):
    _prepare(company, "terminal_review")
    engine = company[0]
    with engine.store.connection(read_only=True) as connection:
        ident = _terminal(connection)[0]["baseline_calculation_id"]
        row = connection.execute("SELECT outcome FROM calculation WHERE id=?", (ident,)).fetchone()
    decoded = json.loads(row[0])
    if variant == "duplicate":
        raw, checksum = '{"lines":[],"lines":[]}', reader.digest({"lines": []})
    elif variant == "noncanonical":
        raw, checksum = json.dumps(decoded, indent=2), reader.digest(decoded)
    else:
        if variant == "self_consistent_zero":
            decoded["lines"] = []
        elif variant == "shape":
            decoded["lines"] = {"not": "a list"}
        else:
            decoded["lines"][0]["debit"] = 2**63
            decoded["lines"][1]["credit"] = 2**63
        raw, checksum = reader.canonical(decoded), reader.digest(decoded)
    damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
           (raw, checksum, ident))
    if variant == "noncanonical":
        assert _check(company, reader) == {_current_reverse(engine)["id"]}
    else:
        with pytest.raises(KernelError) as failure:
            _check(company, reader)
        assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("target", ["digest", "seal", "fact", "identity"])
def test_necessary_baseline_digest_seal_and_source_identity_cannot_disappear(company, target):
    _prepare(company, "same_month_replace")
    engine = company[0]
    with engine.store.connection(read_only=True) as connection:
        ident = _terminal(connection)[0]["baseline_calculation_id"]
        fact_id = connection.execute(
            "SELECT fact_id FROM calculation WHERE id=?", (ident,),
        ).fetchone()[0]
    if target == "digest":
        damage(engine, "calculation", "UPDATE calculation SET digest=? WHERE id=?",
               (b"x" * 32, ident))
    elif target == "seal":
        damage(engine, "calculation_seal", "DELETE FROM calculation_seal WHERE calculation_id=?",
               (ident,), foreign_keys=False)
    elif target == "fact":
        damage(engine, "fact_revision", "DELETE FROM fact_revision WHERE id=?",
               (fact_id,), foreign_keys=False)
    else:
        damage(engine, "calculation", "UPDATE calculation SET period=period+1 WHERE id=?",
               (ident,))
    with pytest.raises(KernelError) as failure:
        _check(company, publication)
    assert failure.value.code == "content_integrity_failed"


def test_fixed_v1_correction_publication_guard_never_calls_current_code(company, monkeypatch):
    _prepare(company, "move_review")
    expected = {_current_reverse(company[0])["id"]}

    def forbidden(*args, **kwargs):
        pytest.fail("fixed publication proof invoked mutable current code")

    for name in ("canonical", "digest", "sum_fen", "loads_unique",
                 "Read", "YearMonth", "verify_open_correction_publication_heads"):
        monkeypatch.setattr(publication, name, forbidden)
    monkeypatch.setattr(stored_json, "loads_unique", forbidden)
    monkeypatch.setattr(query_reads, "verify_current_publication_voucher_heads", forbidden)
    assert _check(company, publication_v1) == expected
    with company[0].store.connection(read_only=True) as connection:
        assert publication_v1.verify_open_voucher_heads(
            connection, YearMonth("2026-04").ordinal,
        ) is None
    _remove_reverse(company[0])
    with pytest.raises(KernelError) as failure:
        _check(company, publication_v1)
    assert failure.value.details["reason"] == "correction_voucher_source_missing"


def _accounting(engine):
    with QueryReads.snapshot(engine) as reads:
        return BusinessQueries(engine, reads=reads)._selected_accounting(
            reads.connection, None, "2026-03",
        )


def test_real_cash_correction_combined_deletion_rejects_all_four_consumers(tmp_path):
    engine, save_fact, confirm, close_month = _report_book.__wrapped__(tmp_path)
    profile(save_fact, confirm)
    data = {
        "period": "2026-01", "actual_date": "2026-01-03", "owner_id": "owner",
        "funding_kind": "capital", "amount_fen": 100, "cash_account_id": "cash",
    }
    save_fact("cash_funding", "capital", data)
    confirm("capital")
    close_month("2026-01")
    with engine.store.connection(read_only=True) as connection:
        proof = connection.execute("SELECT digest FROM evidence ORDER BY rowid LIMIT 1")
        proof = proof.fetchone()[0].hex()
    engine.amend_fact(
        "cash_funding", "capital", {**data, "amount_fen": 120}, evidence=(proof,),
        expected_revision=1, recording_error_confirmed=True, request_id="correct-capital",
    )
    confirm("capital", posting_period="2026-03")
    dashboard = Dashboard(engine)
    assert [row["role"] for row in _accounting(engine)["period_events"]] == [
        "reversal", "replacement",
    ]
    funds = dashboard.funds("2026-03", preparation="deferred")["data"]
    assert (funds["total_fen"], funds["net_change_fen"], funds["movement_count"]) == (120, 20, 2)
    report = dashboard.quarterly_report(2026, 1, preparation="deferred")
    cash_rows = report["statements"][2]["rows"]
    assert [row["values"]["current_fen"] for row in cash_rows if row["line"] in (15, 20, 22)] == [
        120, 120, 120,
    ]
    assert dashboard.brief("2026-03", preparation="deferred")["data"] is not None
    _remove_reverse(engine)
    for operation in (
        lambda: _accounting(engine),
        lambda: dashboard.funds("2026-03", preparation="deferred"),
        lambda: dashboard.quarterly_report(2026, 1, preparation="deferred"),
        lambda: dashboard.brief("2026-03", preparation="deferred"),
    ):
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                operation()
            assert failure.value.code == "content_integrity_failed"
            assert failure.value.details["reason"] == "correction_voucher_source_missing"


def test_correction_reads_only_exact_baseline_when_unrelated_frozen_history_grows(tmp_path):
    measurements = []
    for count in (1, 25):
        directory = tmp_path / str(count)
        directory.mkdir()
        book = company.__wrapped__(directory)
        engine = book[0]
        save(book, 100)
        publish(book, "initial")
        for index in range(count):
            subject = f"unrelated-{index}"
            save(book, 1, subject=subject)
            publish(book, "publish-" + subject, subject=subject)
        close(book, "2026-01")
        save(book, 120, 1)
        publish(book, "correction", "2026-03")

        def read(book=book):
            return _check(book, publication)

        work, actual = measure_work(engine, read)
        assert actual == {_current_reverse(engine)["id"]}
        counts = work["counters"]
        assert counts["calculation_result_rows_loaded"] == 1
        assert counts["calculation_result_json_decodes"] == 1
        assert counts.get("typed_fact_json_decodes", 0) == 0
        measurements.append(counts)
    small, large = measurements
    print(json.dumps({"unrelated_frozen_subjects": [1, 25], "work": measurements}))
    for key in ("returned_rows", "returned_value_bytes", "calculation_result_bytes_loaded",
                "sql_calls", "calculation_result_json_decodes"):
        assert large[key] == small[key]
    assert large.get("sqlite_vm_steps", 0) <= small.get("sqlite_vm_steps", 0) + 600


def test_multiple_corrections_share_real_batch_queries_without_sharing_body_proof(company):
    engine = company[0]
    for subject, amount in (("position", 100), ("other-position", 200)):
        save(company, amount, subject=subject)
        publish(company, "initial-" + subject, subject=subject)
    close(company, "2026-01")
    for subject, amount in (("position", 120), ("other-position", 220)):
        save(company, amount, 1, subject=subject)
        publish(company, "correct-" + subject, "2026-03", subject=subject)

    def read(subjects):
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            rows = list(connection.execute(
                "SELECT p.* FROM json_each(?) ids CROSS JOIN calculation_current h "
                "ON h.subject_id=ids.value JOIN calculation_publication p "
                "ON p.calculation_id=h.calculation_id",
                (publication.canonical(subjects),),
            ))
            for row in rows:
                publication.verify_record(row)
            return publication.verify_open_correction_publication_heads(connection, rows)

    one, selected_one = measure_work(engine, lambda: read(["position"]))
    batch, selected_batch = measure_work(engine, lambda: read(["position", "other-position"]))
    assert len(selected_one) == 1 and len(selected_batch) == 2
    assert selected_one < selected_batch
    assert one["counters"]["sql_calls"] == batch["counters"]["sql_calls"]
    assert one["counters"]["calculation_result_rows_loaded"] == 1
    assert batch["counters"]["calculation_result_rows_loaded"] == 2
    assert batch["counters"]["calculation_result_json_decodes"] == 2
    print(json.dumps({"one": one["counters"], "batch": batch["counters"]}))


def test_noncorrection_terminal_needs_no_additional_sql_or_body_reads(company):
    save(company, 100)
    publish(company, "initial")
    engine = company[0]
    with engine.store.connection(read_only=True) as connection:
        rows = _terminal(connection)
        publication.verify_record(rows[0])

    def read():
        with engine.store.connection(read_only=True) as connection:
            return publication.verify_open_correction_publication_heads(connection, rows)

    work, actual = measure_work(engine, read)
    assert actual == set()
    assert work["counters"].get("sql_calls", 0) == 0
    assert work["counters"].get("calculation_result_rows_loaded", 0) == 0
