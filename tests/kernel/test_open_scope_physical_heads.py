"""Owned open scopes carry actual physical heads, never caller success flags."""

import pytest
from stage9_metrics import measure_work
from test_engine import engine as engine  # noqa: F401
from test_engine import publish, save
from test_integrity_content import damage
from test_open_correction_source_heads import prepare
from test_publication_periods import company as company  # noqa: F401
from test_publication_periods import publish as position_publish
from test_publication_periods import save as position_save

from ai_accounting.kernel import query_reads
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import _Snapshot
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

JAN = YearMonth("2026-01").ordinal


def money(engine, period="2026-01"):
    with QueryReads.snapshot(engine) as reads:
        snap = _Snapshot(engine, reads.connection, period, reads=reads)
        return snap.month_journal.account_amounts()


def scope(engine, period=JAN):
    with QueryReads.snapshot(engine) as reads:
        reads.verify_open_voucher_scope(period)
        assert not reads._verified_source_contents
        return {
            key: (signature, {cid: dict(row) for cid, row in receipts.items()})
            for key, (signature, receipts) in reads._verified_current_voucher_publications.items()
        }


def test_normal_scope_reduces_actual_rows_bytes_and_vm_without_body_proof(
    engine, monkeypatch, record_property,
):
    subjects = [f"charge-{index}" for index in range(24)]
    for index, subject in enumerate(subjects):
        save(engine, subject=subject, amount=index + 1, request=subject)
    publish(engine, subjects)
    actual_work, actual = measure_work(engine, lambda: scope(engine))
    expected = {}
    original = query_reads._verify_current_voucher_publication_rows

    def observed(connection, rows):
        returned = original(connection, rows)
        expected.update((key, dict(row)) for key, row in returned.items())
        return returned

    with monkeypatch.context() as patch:
        patch.setattr(query_reads, "_owns_current_selector_snapshot", lambda *a: False)
        patch.setattr(query_reads, "_verify_current_voucher_publication_rows", observed)
        fallback_work, _ = measure_work(engine, lambda: scope(engine))
    receipts = {cid: row for _, rows in actual.values() for cid, row in rows.items()}
    assert receipts == expected and len(actual) == len(subjects)
    for key in ("returned_rows", "returned_value_bytes", "sqlite_vm_steps"):
        value = actual_work["counters"].get(key, 0)
        baseline = fallback_work["counters"].get(key, 0)
        assert value < baseline
        record_property("owned_" + key, value)
        record_property("independent_" + key, baseline)
    for work in (actual_work, fallback_work):
        assert work["counters"]["calculation_result_rows_loaded"] == 0
        assert work["counters"]["calculation_result_json_decodes"] == 0
    sql = [row["statement"] for row in actual_work["sql"]]
    assert not any(s.startswith("SELECT p.id,p.calculation_id,p.voucher_id") for s in sql)
    assert not any(s.startswith("SELECT ids.value requested_id,v.id") for s in sql)


@pytest.mark.parametrize("mode", ["initial", "review", "replace", "move", "clear"])
def test_posted_money_equals_independent_scope_for_legal_publications(company, monkeypatch, mode):
    engine = company[0]
    position_save(company, 100)
    position_publish(company, "initial")
    period, expected = "2026-01", 100
    if mode != "initial":
        expected = 0 if mode == "clear" else 100 if mode == "review" else 150
        period = "2026-02" if mode == "move" else period
        position_save(company, expected, 1, period=period)
        position_publish(company, "second")
    actual = money(engine, period)
    with monkeypatch.context() as patch:
        patch.setattr(query_reads, "_owns_current_selector_snapshot", lambda *a: False)
        independent = money(engine, period)
    assert actual == independent == (
        {"5602": [expected, 0], "2202": [0, expected]} if expected else {}
    )


@pytest.mark.parametrize("target", [
    "head", "version", "wrong_version", "version_subject", "source", "fact",
    "publication", "posting_period",
])
def test_corrupt_physical_or_source_heads_never_cache_batch_success(engine, target):
    save(engine)
    _, initial = publish(engine)
    initial_id = initial["results"][0]["calculation_id"]
    save(engine, revision=1, amount=150, request="replace")
    publish(engine, request="replace-publish")
    scope(engine)
    with engine.store.connection(read_only=True) as connection:
        head = dict(connection.execute(
            "SELECT p.*,c.fact_id FROM calculation_current h "
            "JOIN calculation_publication p ON p.calculation_id=h.calculation_id "
            "JOIN calculation c ON c.id=h.calculation_id"
        ).fetchone())
    if target == "head":
        damage(engine, "voucher_current", "DELETE FROM voucher_current")
    elif target == "version":
        damage(engine, "voucher_version", "DELETE FROM voucher_version WHERE id="
               "(SELECT version_id FROM voucher_current)", foreign_keys=False)
    elif target == "wrong_version":
        damage(engine, "voucher_current", "UPDATE voucher_current SET version_id="
               "(SELECT id FROM voucher_version WHERE calculation_id=?)", (initial_id,))
    elif target == "version_subject":
        damage(engine, "calculation", "UPDATE calculation SET subject_id='wrong' WHERE id=?",
               (head["calculation_id"],), foreign_keys=False)
    elif target == "source":
        damage(engine, "calculation", "DELETE FROM calculation WHERE id=?",
               (head["calculation_id"],), foreign_keys=False)
    elif target == "fact":
        damage(engine, "fact_revision", "DELETE FROM fact_revision WHERE id=?",
               (head["fact_id"],), foreign_keys=False)
    elif target == "publication":
        damage(engine, "calculation_publication", "DELETE FROM calculation_publication WHERE id=?",
               (head["id"],), foreign_keys=False)
    else:
        damage(engine, "calculation_publication", "UPDATE calculation_publication "
               "SET posting_period=posting_period+1 WHERE id=?", (head["id"],))
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                reads.verify_open_voucher_scope(JAN)
            assert failure.value.code == "content_integrity_failed"
            assert not reads._verified_open_voucher_scopes
            assert not reads._verified_current_voucher_publications
            assert not reads._verified_publication_ids


@pytest.mark.parametrize("mode", ["same_month_replace", "move_review", "zero_correction"])
def test_correction_reverse_stays_independently_read(company, mode):
    prepare(company, mode)
    engine = company[0]
    work, receipts = measure_work(engine, lambda: scope(engine, JAN + 3))
    assert receipts
    assert any(signature[3] is not None for signature, _ in receipts.values())
    assert any(row["statement"].startswith("SELECT ids.value requested_id,v.id")
               for row in work["sql"])
    with engine.store.connection(read_only=True) as connection:
        reverse = connection.execute(
            "SELECT v.id FROM voucher_current h JOIN voucher_version v ON v.id=h.version_id "
            "WHERE v.reverses_id IS NOT NULL"
        ).fetchone()[0]
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE version_id=?", (reverse,))
    damage(engine, "voucher_line", "DELETE FROM voucher_line WHERE version_id=?",
           (reverse,), foreign_keys=False)
    damage(engine, "voucher_version", "DELETE FROM voucher_version WHERE id=?",
           (reverse,), foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            reads.verify_open_voucher_scope(JAN + 3)
        assert not reads._verified_current_voucher_publications
        assert not reads._verified_publication_ids


def test_private_scope_rows_expire_with_owned_token_and_public_api_has_no_flag(
    engine, monkeypatch,
):
    save(engine)
    publish(engine)
    captured = []
    original = query_reads._verify_publication_voucher_head_batch

    def observed(connection, rows, reads, scope_heads=None):
        if scope_heads is not None:
            captured.append((scope_heads, rows))
        return original(connection, rows, reads, scope_heads)

    monkeypatch.setattr(query_reads, "_verify_publication_voucher_head_batch", observed)
    with QueryReads.snapshot(engine) as reads:
        reads.verify_open_voucher_scope(JAN)
        carried, rows = captured[0]
        assert carried.require(reads.connection, rows, reads) == rows
        with pytest.raises(TypeError):
            query_reads.verify_current_publication_voucher_heads(
                reads.connection, rows, scope_heads=carried,
            )
    with QueryReads.snapshot(engine) as later:
        with pytest.raises(KernelError):
            carried.require(later.connection, rows, later)


def test_fixed_and_unowned_heads_keep_independent_sql(engine):
    save(engine)
    publish(engine)

    def read(fixed=False):
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            rows = list(connection.execute("SELECT p.* FROM calculation_publication p"))
            if fixed:
                with historical_content(1):
                    query_reads.verify_current_publication_voucher_heads(connection, rows)
            else:
                query_reads.verify_current_publication_voucher_heads(connection, rows)

    for fixed in (False, True):
        work, _ = measure_work(engine, lambda fixed=fixed: read(fixed))
        statements = [row["statement"] for row in work["sql"]]
        assert any(s.startswith("SELECT p.id,p.calculation_id,p.voucher_id") for s in statements)
        assert any(s.startswith("SELECT ids.value requested_id,v.id") for s in statements)


def test_withdrawn_and_unconsumed_future_do_not_create_physical_obligations(company):
    engine = company[0]
    position_save(company, 100)
    position_publish(company, "initial")
    preview = engine.preview_delete("position")
    engine.delete("position", preview_digest=preview["digest"], epochs=preview["epochs"],
                  request_id="remove")
    position_save(company, 200, period="2026-02", subject="future")
    position_publish(company, "future", subject="future")
    damage(engine, "calculation_publication", "DELETE FROM calculation_publication "
           "WHERE subject_id='future'", foreign_keys=False)
    assert money(engine) == {}
    with QueryReads.snapshot(engine) as reads:
        reads.verify_open_voucher_scope(JAN)
        assert not reads._verified_current_voucher_publications
        assert not reads._verified_source_contents
        with pytest.raises(KernelError):
            reads.verify_open_voucher_scope(JAN + 1)


def test_missing_publication_failure_repeats_actual_scope_work(engine, record_property):
    save(engine)
    publish(engine)
    scope(engine)
    damage(engine, "calculation_publication", "DELETE FROM calculation_publication",
           foreign_keys=False)

    def failed_reads():
        with QueryReads.snapshot(engine) as reads:
            for _ in range(2):
                with pytest.raises(KernelError) as failure:
                    reads.verify_open_voucher_scope(JAN)
                assert failure.value.code == "content_integrity_failed"
                assert not reads._verified_open_voucher_scopes
                assert not reads._verified_current_voucher_publications
                assert not reads._verified_publication_ids

    work, _ = measure_work(engine, failed_reads)
    scopes = [row for row in work["sql"]
              if row["statement"].startswith("SELECT p.*,a.calculation_id current_id")]
    reverse = [row for row in work["sql"]
               if row["statement"].startswith("SELECT v.id FROM voucher_version v INDEXED")]
    assert len(scopes) == len(reverse) == 1
    assert scopes[0]["calls"] == reverse[0]["calls"] == 2
    assert reverse[0]["returned_rows"] == 2
    assert work["counters"]["calculation_result_rows_loaded"] == 0
    assert work["counters"]["calculation_result_json_decodes"] == 0
    for key in ("returned_rows", "returned_value_bytes", "sqlite_vm_steps"):
        record_property("failed_" + key, work["counters"].get(key, 0))
