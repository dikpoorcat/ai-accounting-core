"""Pending members retain exact vouchers when a review keeps their old owner."""

from contextlib import contextmanager

import pytest
from test_banking import book as book  # noqa: F401
from test_banking import close_month, inventories
from test_dashboard_open_groups import expenses, snapshot
from test_integrity_content import damage

from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _Snapshot
from ai_accounting.kernel.dashboard_open_groups import open_group_members, open_group_page
from ai_accounting.kernel.query_reads import QueryReads, _owns_current_selector_snapshot


def _review(book):
    engine, _, publish, _ = book
    expenses(book, 1)
    with snapshot(engine, "2026-09") as snap:
        key = open_group_page(snap)["collection"]["items"][0]["group_key"]
        original = open_group_members(snap, key)["items"][0]
    evidence = engine.register_evidence(
        b"Explicit unchanged expense review", "text/plain", "review",
        request_id="unchanged-review-proof",
    )["digest"]
    engine.save_fact(
        "expense", "expense-000", {
            "period": "2026-09", "counterparty_id": "supplier", "amount_fen": 100,
            "expense_class": "administration", "creditor_kind": "supplier",
        }, evidence=(evidence,), expected_revision=1, request_id="unchanged-review",
    )
    reviewed = publish("expense-000")
    assert reviewed["results"][0]["impact"] == "review_no_impact"
    with engine.store.connection(read_only=True) as connection:
        source = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='expense-000'",
        ).fetchone()[0]
        owner = connection.execute(
            "SELECT calculation_id FROM voucher_version WHERE id=?",
            (original["voucher_version_id"],),
        ).fetchone()[0]
    assert source != owner
    return key, original, source


@pytest.mark.parametrize("closed,later", [(False, False), (True, False), (True, True)])
def test_reviewed_member_and_group_keep_original_exact_voucher(book, closed, later):
    engine, save, _, proof = book
    key, original, _ = _review(book)
    if closed:
        assert close_month(
            inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09",
        )["status"] == "closed"
    if later:
        save("expense", "expense-000", {
            "period": "2026-09", "counterparty_id": "supplier", "amount_fen": 200,
            "expense_class": "administration", "creditor_kind": "supplier",
        }, revision=2)
        preview = engine.preview(["expense-000"], posting_period="2026-10")
        engine.confirm(
            ["expense-000"], preview_digest=preview["digest"], epochs=preview["epochs"],
            posting_period="2026-10", request_id="later-reviewed-correction",
        )
    with snapshot(engine, "2026-09") as snap:
        member = open_group_members(snap, key)["items"][0]
        assert member["voucher_version_id"] == original["voucher_version_id"]
        assert member["voucher_number"] == original["voucher_number"]
        assert member["outstanding_fen"] == 100
        assert member["current_outstanding_fen"] == (200 if later else 100)
    result = Dashboard(engine).brief_group("2026-09", section="open_items", group_key=key)
    members = result["data"]["collections"]["members"]["items"]
    vouchers = result["data"]["collections"]["vouchers"]["items"]
    assert members[0]["voucher_version_id"] == vouchers[0]["voucher_version_id"] == original[
        "voucher_version_id"
    ]
    assert len(vouchers) == 1
    assert vouchers[0]["amount_fen"] == 100
    assert sum(line["debit_fen"] for line in vouchers[0]["lines"]) == 100
    assert sum(line["credit_fen"] for line in vouchers[0]["lines"]) == 100


@pytest.mark.parametrize("fault", ["missing", "duplicate", "adopted_body"])
def test_reviewed_member_rejects_missing_duplicate_or_damaged_exact_voucher(book, fault):
    engine, _, _, proof = book
    key, original, source = _review(book)
    close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    if fault == "missing":
        damage(engine, "voucher_version", "DELETE FROM voucher_version WHERE id=?",
               (original["voucher_version_id"],), foreign_keys=False)
    elif fault == "duplicate":
        damage(engine, "voucher_version",
               "INSERT INTO voucher_version SELECT 'unexpected-duplicate',voucher_id,"
               "calculation_id,period,reverses_id,total FROM voucher_version WHERE id=?",
               (original["voucher_version_id"],))
    else:
        damage(engine, "calculation",
               "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount_fen',101) "
               "WHERE id=?", (source,))
    with snapshot(engine, "2026-09") as snap:
        with pytest.raises(KernelError) as failure:
            open_group_members(snap, key)
        assert failure.value.code == "content_integrity_failed"


@contextmanager
def _fallback_snapshot(engine, reader):
    if reader == "fixed":
        with historical_content(1), snapshot(engine, "2026-09") as snap:
            yield snap
    else:
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            reads = QueryReads(engine, connection)
            snap = _Snapshot(engine, connection, "2026-09", reads)
            try:
                yield snap
            finally:
                snap.release()


@pytest.mark.parametrize("reader", ["fixed", "unowned"])
@pytest.mark.parametrize("damaged", [False, True])
def test_closed_reviewed_member_fallback_keeps_exact_proof_without_current_cache(
    book, reader, damaged,
):
    engine, _, _, proof = book
    key, original, source = _review(book)
    close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    with snapshot(engine, "2026-09") as snap:
        open_group_members(snap, key)
        cached = {key: value for key, value in snap.reads._report_snapshot_cache.items()
                  if isinstance(key, tuple) and key[0] == "authenticated_frozen_voucher_adoption"}
        assert cached
    if damaged:
        damage(engine, "calculation",
               "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount_fen',101) "
               "WHERE id=?", (source,))
    with _fallback_snapshot(engine, reader) as snap:
        assert not _owns_current_selector_snapshot(snap.reads, snap.connection)
        # Deliberately stale current-reader proof must neither authorize this
        # historical/unowned consumer nor be refreshed by it after success.
        snap.reads._report_snapshot_cache.update(cached)
        if damaged:
            with pytest.raises(KernelError) as failure:
                open_group_members(snap, key)
            assert failure.value.code == "content_integrity_failed"
        else:
            member = open_group_members(snap, key)["items"][0]
            assert member["voucher_version_id"] == original["voucher_version_id"]
            assert member["voucher_number"] == original["voucher_number"]
            assert member["outstanding_fen"] == member["current_outstanding_fen"] == 100
            assert isinstance(snap._open_group_data["historical"]["obligations"], list)
        actual = {key: value for key, value in snap.reads._report_snapshot_cache.items()
                  if isinstance(key, tuple) and key[0] == "authenticated_frozen_voucher_adoption"}
        assert actual == cached
        assert not snap.reads._exact_source_headers
