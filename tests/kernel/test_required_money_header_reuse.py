"""Only actual current selector headers replace a duplicate physical transfer."""

import pytest
import test_cash as cash
from stage9_metrics import measure_work
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage
from test_journal_original_source_scope import _different_original_months

from ai_accounting.kernel import query_reads
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads, verify_current_voucher_publications

book = cash.book


def amounts(engine, period="2026-01"):
    with Dashboard(engine)._snapshot(period) as snap:
        return snap.month_journal.account_amounts()


def test_actual_selected_headers_reduce_transfer_with_same_required_result_work(
    engine, monkeypatch, record_property
):
    subjects = [f"charge-{index}" for index in range(24)]
    for index, subject in enumerate(subjects):
        save(engine, subject=subject, amount=index + 1, request=subject)
    publish(engine, subjects)
    current_work, current = measure_work(engine, lambda: amounts(engine))
    independent = query_reads.verify_current_voucher_publications

    def unowned_lookup(connection, identifiers):
        # The ID entry point now shares successful owned proofs too. Compare
        # with the real unowned boundary, which must read them independently.
        token = query_reads._active_fact_reads.set(None)
        try:
            return independent(connection, identifiers)
        finally:
            query_reads._active_fact_reads.reset(token)

    with monkeypatch.context() as patch:
        patch.setattr(query_reads, "_selected_current_voucher_publications", lambda *a, **k: None)
        patch.setattr(query_reads, "verify_current_voucher_publications", unowned_lookup)
        old_work, old = measure_work(engine, lambda: amounts(engine))
    assert current == old == {"5602": [300, 0], "2202": [0, 300]}
    for key in ("returned_rows", "returned_value_bytes", "sqlite_vm_steps"):
        assert current_work["counters"][key] < old_work["counters"][key]
        record_property(f"current_{key}", current_work["counters"][key])
        record_property(f"unowned_id_lookup_{key}", old_work["counters"][key])
    for key in ("calculation_result_rows_loaded", "calculation_result_json_decodes"):
        assert current_work["counters"][key] == old_work["counters"][key]


@pytest.mark.parametrize("closed", [False, True])
def test_original_and_no_impact_current_publications_equal_independent_id_reader(engine, closed):
    save(engine)
    publish(engine)
    save(engine, revision=1, request="review")
    publish(engine, request="publish-review")
    if closed:
        close(engine)
    with Dashboard(engine)._snapshot("2026-01") as snap:
        rows = list(snap.connection.execute(*snap.month_journal.sql()))
        expected = verify_current_voucher_publications(snap.connection, {row["id"] for row in rows})
        selected = query_reads._selected_current_voucher_publications(
            snap.reads, rows, period=snap.month, cutoff=snap.month
        )
        assert selected is None if closed else selected == expected
        assert snap.month_journal.account_amounts() == {"5602": [100, 0], "2202": [0, 100]}


@pytest.mark.parametrize("boundary", [
    "new_reader", "v1", "dict_rows", "different_month", "disabled", "unowned",
])
def test_unproved_header_scopes_keep_independent_id_fallback(engine, monkeypatch, boundary):
    save(engine)
    publish(engine)
    with Dashboard(engine)._snapshot("2026-01") as snap:
        rows = list(snap.connection.execute(*snap.month_journal.sql()))
        if boundary == "new_reader":
            snap.reads._report_snapshot_cache.clear()
        elif boundary == "v1":
            monkeypatch.setattr(snap.reads.store.registry, "content_version", 1, raising=False)
        elif boundary == "dict_rows":
            rows = [dict(row) for row in rows]
        elif boundary == "disabled":
            monkeypatch.setattr(snap.reads, "_snapshot_active", False)
        reads = QueryReads(engine, snap.connection) if boundary == "unowned" else snap.reads
        period = snap.month + 1 if boundary == "different_month" else snap.month
        assert query_reads._selected_current_voucher_publications(
            reads, rows, period=period, cutoff=snap.month
        ) is None
        assert verify_current_voucher_publications(snap.connection, {row["id"] for row in rows})


@pytest.mark.parametrize("corruption", ["kind", "fact_seal", "current_pointer", "publication"])
def test_actual_month_rejects_bad_source_and_current_head_before_proof_publication(
    engine, corruption
):
    save(engine, subject="first", request="first")
    save(engine, subject="second", request="second", amount=200)
    publish(engine, ["first", "second"])
    amounts(engine)
    with engine.store.connection(read_only=True) as connection:
        heads = dict(connection.execute(
            "SELECT subject_id,calculation_id FROM calculation_current"
        ))
        fact = connection.execute(
            "SELECT fact_id FROM calculation WHERE id=?", (heads["first"],)
        ).fetchone()[0]
    if corruption == "kind":
        damage(engine, "calculation", "UPDATE calculation SET kind='test_source' WHERE id=?",
               (heads["first"],))
    elif corruption == "fact_seal":
        damage(engine, "fact_seal", "DELETE FROM fact_seal WHERE fact_id=?",
               (fact,), foreign_keys=False)
    elif corruption == "current_pointer":
        damage(engine, "calculation_current",
               "DELETE FROM calculation_current WHERE subject_id='second'")
        damage(engine, "calculation_current",
               "UPDATE calculation_current SET calculation_id=? WHERE subject_id='first'",
               (heads["second"],))
    else:
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE calculation_id=?",
               (heads["first"],), foreign_keys=False)
    with Dashboard(engine)._snapshot("2026-01") as snap:
        with pytest.raises(KernelError) as failure:
            snap.month_journal.account_amounts()
        assert failure.value.code == "content_integrity_failed"
        assert snap.month_journal.verified_rows() is None


def test_closed_consecutive_corrections_keep_exact_originals_and_adopted_review(engine):
    _different_original_months(engine, review=True)
    assert amounts(engine, "2026-03") == {"5602": [410, 340], "2202": [340, 410]}


def test_same_month_activity_work_ignores_unrelated_prior_objects(book):
    engine, save_cash, publish_cash, _ = book
    cash.fund(save_cash, amount=1000)
    cash.expense(save_cash, amount=100)
    publish_cash("funding", "expense")
    cash.payment(save_cash, amount=100, source_id="expense", subject="paid")
    publish_cash("paid")

    def classified():
        with Dashboard(engine)._snapshot("2026-09") as snap:
            snap.month_journal.account_amounts()
            return snap.activity_classification

    before_work, before = measure_work(engine, classified)
    subjects = [f"unrelated-prior-{index}" for index in range(24)]
    for index, subject in enumerate(subjects):
        save_cash("expense", subject, {
            "period": "2026-08", "counterparty_id": subject,
            "amount_fen": index + 1, "expense_class": "administration",
            "creditor_kind": "supplier",
        })
    publish_cash(*subjects)
    after_work, after = measure_work(engine, classified)
    assert after == before
    assert after[1]["expense_supplier", "cash_payment"] == 1
    for counter in (
        "returned_rows", "returned_value_bytes", "sqlite_vm_steps",
        "calculation_result_rows_loaded", "calculation_result_json_decodes",
    ):
        assert after_work["counters"][counter] == before_work["counters"][counter]


def test_historical_context_with_current_registry_keeps_independent_lookup(engine):
    save(engine)
    publish(engine)
    with Dashboard(engine)._snapshot("2026-01") as snap:
        rows = list(snap.connection.execute(*snap.month_journal.sql()))
        with historical_content(1):
            assert query_reads._selected_current_voucher_publications(
                snap.reads, rows, period=snap.month, cutoff=snap.month
            ) is None
            assert verify_current_voucher_publications(
                snap.connection, {row["id"] for row in rows}
            )


def test_filtered_amount_reader_keeps_public_id_lookup(engine, monkeypatch):
    save(engine)
    publish(engine)
    observed = []
    original = query_reads.verify_current_voucher_publications

    def lookup(connection, identifiers):
        observed.append(set(identifiers))
        return original(connection, identifiers)

    monkeypatch.setattr(query_reads, "verify_current_voucher_publications", lookup)
    def amounts():
        with Dashboard(engine)._snapshot("2026-01") as snap:
            result = snap.month_journal.select(kinds={"test_charge"}).account_amounts()
            assert snap.reads._verified_open_voucher_scopes[snap.month] >= snap.month
            assert set(snap.reads._verified_current_voucher_publications) == observed[-1]
            assert all(signature is not None for signature, _ in
                       snap.reads._verified_current_voucher_publications.values())
            return result

    actual_work, actual = measure_work(engine, amounts)
    assert actual == {"5602": [100, 0], "2202": [0, 100]}
    # Filtering still enters the public ID boundary. The complete owned scope
    # already read and proved the actual forward heads and opposite sources;
    # its successful exact relationship records avoid a duplicate ID transfer.
    assert observed and all(identifiers == observed[0] for identifiers in observed)
    assert len(observed[0]) == 1
    lookups = [statement for statement in actual_work["sql"]
               if "requested_id" in statement["statement"]]
    assert not lookups
    scope = [statement for statement in actual_work["sql"]
             if statement["statement"].startswith("SELECT p.*,a.calculation_id current_id")]
    opposite = [statement for statement in actual_work["sql"]
                if "c.id IS NULL OR a.calculation_id IS NULL OR p.id IS NULL" in
                statement["statement"]]
    assert len(scope) == len(opposite) == 1
    assert scope[0]["calls"] == opposite[0]["calls"] == 1
    assert scope[0]["returned_rows"] == 1 and opposite[0].get("returned_rows", 0) == 0
    exact_ids = observed[0]
    observed.clear()

    def unowned_lookup(connection, identifiers):
        token = query_reads._active_fact_reads.set(None)
        try:
            return lookup(connection, identifiers)
        finally:
            query_reads._active_fact_reads.reset(token)

    monkeypatch.setattr(query_reads, "verify_current_voucher_publications", unowned_lookup)
    independent_work, independent = measure_work(engine, amounts)
    assert independent == actual
    assert observed and all(identifiers == exact_ids for identifiers in observed)
    physical = [statement for statement in independent_work["sql"]
                if "requested_id" in statement["statement"]]
    assert len(physical) == 1 and physical[0]["calls"] == 1
    assert physical[0]["returned_rows"] == len(exact_ids)


def test_missing_no_impact_review_rejects_new_request_after_success(engine):
    save(engine)
    publish(engine)
    save(engine, revision=1, request="review")
    _, reviewed = publish(engine, request="publish-review")
    amounts(engine)
    damage(engine, "calculation_publication",
           "DELETE FROM calculation_publication WHERE calculation_id=?",
           (reviewed["results"][0]["calculation_id"],), foreign_keys=False)
    with Dashboard(engine)._snapshot("2026-01") as snap:
        with pytest.raises(KernelError) as failure:
            snap.month_journal.account_amounts()
        assert failure.value.code == "content_integrity_failed"
        assert snap.month_journal.verified_rows() is None
