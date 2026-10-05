"""Page the month's already proved headers without repeating source selection."""

import pytest
from stage9_metrics import measure_work
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_reads import Journal


def _posted(engine, count=23):
    subjects = [f"item-{index:03}" for index in range(count)]
    for index, subject in enumerate(subjects):
        save(engine, subject=subject, amount=index + 1, request=f"save-{subject}")
    publish(engine, subjects, request="publish-items")
    return subjects


def _pages(snapshot):
    journal = snapshot.month_journal
    first, first_page = journal.page(0, 20)
    last, last_page = journal.page(first_page["next_cursor"], 20)
    exact_number = journal.page(0, 1, voucher_number=last[-1]["number"])
    exact_id = journal.page(0, 1, voucher_version_id=last[-1]["id"])
    absent = journal.page(0, 1, voucher_version_id="not-a-voucher")
    after_end = journal.page(last[-1]["number"], 20)
    return (first, first_page, last, last_page, exact_number, exact_id, absent, after_end)


@pytest.mark.parametrize("closed", [False, True])
def test_verified_page_matches_all_sql_pages_and_exact_focus(engine, monkeypatch, closed):
    _posted(engine)
    # An unchanged amount can adopt new evidence while keeping the old voucher.
    save(engine, subject="item-022", amount=23, revision=1, request="review-item")
    publish(engine, ["item-022"], request="publish-review")
    if closed:
        close(engine)
    dashboard = Dashboard(engine)

    def read():
        with dashboard._snapshot("2026-01") as snapshot:
            snapshot.month_journal.account_amounts()
            return _pages(snapshot)

    current_work, current = measure_work(engine, read)
    with monkeypatch.context() as patch:
        patch.setattr(Journal, "_verified_page_rows", lambda *_args, **_kwargs: None)
        original_work, original = measure_work(engine, read)
    assert current == original
    first, first_page, last, last_page, exact_number, exact_id, absent, after_end = current
    assert len(first) == 20 and len(last) == 3
    assert first_page["total_count"] == last_page["total_count"] == 23
    assert first_page["has_more"] and not last_page["has_more"]
    assert [row["number"] for row in [*first, *last]] == list(range(1, 24))
    assert exact_number[0] == exact_id[0] == [last[-1]]
    assert absent[0] == after_end[0] == []
    for key in ("sqlite_vm_steps", "returned_rows", "returned_value_bytes"):
        assert current_work["counters"][key] < original_work["counters"][key]
    # Source trust/decoding still covers the complete month in both paths.
    assert current_work["counters"]["calculation_result_json_decodes"] == (
        original_work["counters"]["calculation_result_json_decodes"]
    )


def test_no_proof_or_different_scope_keeps_independent_selector(engine, monkeypatch):
    _posted(engine, count=3)
    with Dashboard(engine)._snapshot("2026-01") as snapshot:
        journal = snapshot.month_journal
        assert journal._verified_page_rows(0, 20) is None
        expected = journal.page(0, 20)
        journal.account_amounts()
        assert journal.page(0, 20) == expected
        with Dashboard(engine)._snapshot("2026-01"):
            # A different active read scope cannot borrow this proof, even
            # while the outer connection remains in its read transaction.
            assert journal._verified_page_rows(0, 20) is None
        for scoped in (
            journal.select(kinds={"test_charge"}),
            journal.select(accounts={"2202"}),
            journal.select(subjects={"item-000"}),
            snapshot.journal,
        ):
            assert scoped._verified_page_rows(0, 20) is None
            rows, page = scoped.page(0, 20)
            assert rows and page["total_count"] == len(rows)
        monkeypatch.setattr(snapshot.reads.store.registry, "content_version", 1, raising=False)
        assert journal._verified_page_rows(0, 20) is None
        snapshot.reads._snapshot_active = False
        assert journal._verified_page_rows(0, 20) is None


@pytest.mark.parametrize("options", [{"after_number": 1 << 63}, {"voucher_number": 1 << 63}])
def test_out_of_range_page_key_keeps_original_sqlite_rejection(engine, options):
    _posted(engine, count=1)
    with Dashboard(engine)._snapshot("2026-01") as snapshot:
        journal = snapshot.month_journal
        journal.account_amounts()
        after = options.get("after_number", 0)
        exact = options.get("voucher_number")
        assert journal._verified_page_rows(after, 20, voucher_number=exact) is None
        with pytest.raises(OverflowError):
            journal.page(after, 20, voucher_number=exact)


def test_repeated_pages_reuse_only_selected_month_without_new_decoding(engine):
    _posted(engine, count=23)
    dashboard = Dashboard(engine)

    def measured():
        with dashboard._snapshot("2026-01") as snapshot:
            snapshot.month_journal.account_amounts()
            # Page hydration is part of the measurement; prove that unused
            # source/history is not loaded just to reuse already proved heads.
            work, result = measure_work(engine, lambda: _pages(snapshot))
            return work["counters"], result

    before, expected = measured()
    for period in ("2025-01", "2025-12", "2026-02", "2027-01"):
        subjects = [f"other-{period}-{index}" for index in range(10)]
        for subject in subjects:
            save(engine, subject=subject, amount=999, period=period, request=f"save-{subject}")
        publish(engine, subjects, request=f"post-{period}")
    for revision in range(1, 4):
        save(engine, subject="item-000", amount=1, revision=revision, request=f"review-{revision}")
        publish(engine, ["item-000"], request=f"post-review-{revision}")
    after, actual = measured()
    # Review legitimately changes exact adopted metadata; page identities,
    # amounts, complete counts and cursor progression remain the same.
    assert [[row["number"] for row in group] for group in (actual[0], actual[2])] == (
        [[row["number"] for row in group] for group in (expected[0], expected[2])]
    )
    assert actual[1] == expected[1] and actual[3] == expected[3]
    for key in (
        "returned_rows", "returned_value_bytes", "calculation_result_rows_loaded",
        "calculation_result_json_decodes",
    ):
        assert after.get(key, 0) <= before.get(key, 0)
